# -*- coding: utf-8 -*-
#
# Copyright (C) 2015-2019 Bitergia
#
# This program is free software; you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation; either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE. See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program; if not, write to the Free Software
# Foundation, Inc., 59 Temple Place - Suite 330, Boston, MA 02111-1307, USA.
#
# Authors:
#   Alvaro del Castillo San Felix <acs@bitergia.com>
#

import datetime
from dateutil import parser, tz
import inspect
import json
import logging

import requests
import urllib3


from grimoirelab_toolkit.datetime import datetime_utcnow


BACKOFF_FACTOR = 0.2
MAX_RETRIES = 21
MAX_RETRIES_ON_REDIRECT = 5
MAX_RETRIES_ON_READ = 8
MAX_RETRIES_ON_CONNECT = 21
STATUS_FORCE_LIST = [408, 409, 502, 503, 504]
logger = logging.getLogger(__name__)


def get_repository_filter(perceval_backend, perceval_backend_name,
                          term=False):
    """ Get the filter needed for get the items in a repository """
    from .github import GITHUB

    filter_ = {}

    if not perceval_backend:
        return filter_

    field = 'origin'
    value = perceval_backend.origin

    if perceval_backend_name in ["meetup", "nntp", "stackexchange", "jira"]:
        # Until tag is supported in all raw and enriched indexes
        # we should use origin. But stackexchange and meetup won't work with origin
        # because the tag must be included in the filter.
        # For nntp we have a common group server as origin, so we need to use also the tag.
        # And in jira we can filter by product, and the origin is the same jira server.
        field = 'tag'
        value = perceval_backend.tag

    if perceval_backend:
        if not term:
            filter_ = {"name": field,
                       "value": value}
        else:
            filter_ = '''
                {"term":
                    { "%s" : "%s"  }
                }
            ''' % (field, value)
            # Filters are always a dict
            filter_ = json.loads(filter_)

    if value in ['', GITHUB + '/', 'https://meetup.com/']:
        # Support for getting all items from a multiorigin index
        # In GitHub we receive GITHUB + '/', the site url without org and repo
        # In Meetup we receive https://meetup.com/ as the tag
        filter_ = {}

    return filter_


def get_time_diff_days(start, end):
    ''' Number of days between two dates in UTC format  '''

    if start is None or end is None:
        return None

    if type(start) is not datetime.datetime:
        start = parser.parse(start).replace(tzinfo=None)
    if type(end) is not datetime.datetime:
        end = parser.parse(end).replace(tzinfo=None)

    seconds_day = float(60 * 60 * 24)
    diff_days = (end - start).total_seconds() / seconds_day
    diff_days = float('%.2f' % diff_days)

    return diff_days


# https://github.com/grimoirelab/perceval/blob/master/perceval/utils.py#L149
def unixtime_to_datetime(ut):
    """Convert a unixtime timestamp to a datetime object.
    The function converts a timestamp in Unix format to a
    datetime object. UTC timezone will also be set.
    :param ut: Unix timestamp to convert
    :returns: a datetime object
    :raises InvalidDateError: when the given timestamp cannot be
        converted into a valid date
    """

    dt = datetime.datetime.utcfromtimestamp(ut)
    dt = dt.replace(tzinfo=tz.tzutc())
    return dt


def grimoire_con(insecure=True, conn_retries=MAX_RETRIES_ON_CONNECT, total=MAX_RETRIES):
    conn = requests.Session()
    # {backoff factor} * (2 ^ ({number of total retries} - 1))
    # conn_retries = 21  # 209715.2 = 2.4d
    # total covers issues like 'ProtocolError('Connection aborted.')
    # Retry when there are errors in HTTP connections
    retries = urllib3.util.Retry(total=total, connect=conn_retries, read=MAX_RETRIES_ON_READ,
                                 redirect=MAX_RETRIES_ON_REDIRECT, backoff_factor=BACKOFF_FACTOR,
                                 method_whitelist=False, status_forcelist=STATUS_FORCE_LIST)
    adapter = requests.adapters.HTTPAdapter(max_retries=retries)
    conn.mount('http://', adapter)
    conn.mount('https://', adapter)

    if insecure:
        urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
        conn.verify = False

    return conn


def get_last_enrich(backend_cmd, enrich_backend):
    last_enrich = None

    if backend_cmd:
        backend = backend_cmd.backend
        # Only supported in data retrieved from a perceval backend
        # Always filter by repository to support multi repository indexes
        backend_name = enrich_backend.get_connector_name()
        filter_ = get_repository_filter(backend, backend_name)

        # Check if backend supports from_date
        signature = inspect.signature(backend.fetch)

        from_date = None
        if 'from_date' in signature.parameters:
            try:
                # Support perceval pre and post BackendCommand refactoring
                from_date = backend_cmd.from_date
            except AttributeError:
                from_date = backend_cmd.parsed_args.from_date

        offset = None
        if 'offset' in signature.parameters:
            try:
                offset = backend_cmd.offset
            except AttributeError:
                offset = backend_cmd.parsed_args.offset

        if from_date:
            if from_date.replace(tzinfo=None) != parser.parse("1970-01-01"):
                last_enrich = from_date
            # if the index is empty, set the last enrich to None
            elif not enrich_backend.from_date:
                last_enrich = None
            else:
                # if the index is not empty, the last enrich is the minimum between
                # the last filtered item and the last item in the enriched index
                last_enrich_filtered = enrich_backend.get_last_update_from_es([filter_])
                last_enrich = get_min_last_enrich(enrich_backend.from_date, last_enrich_filtered)

        elif offset is not None:
            if offset != 0:
                last_enrich = offset
            else:
                last_enrich = enrich_backend.get_last_offset_from_es([filter_])

        else:
            if not enrich_backend.from_date:
                last_enrich = None
            else:
                last_enrich_filtered = enrich_backend.get_last_update_from_es([filter_])
                last_enrich = get_min_last_enrich(enrich_backend.from_date, last_enrich_filtered)
    else:
        last_enrich = enrich_backend.get_last_update_from_es()

    return last_enrich


def get_min_last_enrich(last_enrich, last_enrich_filtered):
    min_enrich = last_enrich
    if last_enrich_filtered:
        min_enrich = min(last_enrich, last_enrich_filtered.replace(tzinfo=None))

    return min_enrich


def get_diff_current_date(days=0, hours=0, minutes=0):
    before_date = datetime_utcnow() - datetime.timedelta(days=days, hours=hours, minutes=minutes)

    return before_date
