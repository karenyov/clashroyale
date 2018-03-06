'''
MIT License

Copyright (c) 2017 kyb3r

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
'''

import asyncio
import json
from datetime import datetime
from urllib.parse import urlencode

import aiohttp
import requests

from .errors import NotFoundError, NotResponding, ServerError, Unauthorized
from .models import (AuthStats, Clan, ClanInfo, Constants, Player, PlayerInfo,
                     Tournament, rlist)
from .utils import API, SqliteDict, clansearch, crtag, keys, typecasted

fromts = datetime.fromtimestamp


class Client:
    '''A client that requests data from cr-api.com. This class can
    either be async or non async.

    Parameters
    ----------
    token: str
        The api authorization token to be used for requests.
    is_async: Optional[bool]
        Toggle for asynchronous/synchronous usage of the client.
        Defaults to False.
    session: Optional[Session]
        The http (client)session to be used for requests. Can either be a 
        requests.Session or aiohttp.ClientSession.
    timeout: Optional[int]
        A timeout for requests to the API, defaults to 10 seconds.
    cache_fp: Optional[str]
        File path for the sqlite3 database to use for caching requests, 
        if this parameter is provided, the client will use its caching system.
    cache_expires: Optional[int]
        The number of seconds to wait before the client will request 
        from the api for a specific route, this defaults to 10 seconds.
    table_name: Optional[str]
        The table name to use for the cache database. Defaults to 'cache'
    camel_case: Optional(bool)
        Whether or not to access model data keys in snake_case or camelCase, 
        this defaults to False (use snake_case)
    '''

    def __init__(self, token, session=None, is_async=False, **options):
        self.token = token
        self.is_async = is_async
        self.timeout = options.get('timeout', 10)
        self.session = session or (aiohttp.ClientSession() if is_async else requests.Session())
        self.camel_case = options.get('camel_case', False)
        self.headers = {
            'Authorization': f'Bearer {token}',
            'user-agent': 'python-clashroyale-client (kyb3r)'
            }
        self.cache_fp = options.get('cache_fp')
        self.using_cache = bool(self.cache_fp)
        self.cache_reset = options.get('cache_expires', 300)
        if self.using_cache:
            table = options.get('table_name', 'cache')
            self.cache = SqliteDict(self.cache_fp, table)

    def _resolve_cache(self, url, **params):
        bucket = url + (('?' + urlencode(params)) if params else '')
        cached_data = self.cache.get(bucket)
        if not cached_data:
            return None
        last_updated = fromts(cached_data['c_timestamp'])
        if (datetime.utcnow() - last_updated).total_seconds() < self.cache_reset:
            ret = (cached_data['data'], True, last_updated)
            if self.is_async:
                return self._wrap_coro(ret) # return a coroutine
                                            # so self.request can be awaitable
            return ret
        return None

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        self.session.close()

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc_value, traceback):
        self.session.close()

    def __repr__(self):
        return f'<ClashRoyaleClient async={self.is_async}>'

    def close(self):
        self.session.close()

    def _raise_for_status(self, resp, text):
        try:
            data = json.loads(text)
        except:
            data = text
        code = getattr(resp, 'status', None) or getattr(resp, 'status_code')
        if 300 > code >= 200: # Request was successful
            if self.using_cache:
                cached_data = {
                    'c_timestamp': datetime.utcnow().timestamp(),
                    'data': data
                }
                self.cache[str(resp.url)] = cached_data
            return data, False, datetime.utcnow() # value, cached, last_updated
        if code == 401: # Unauthorized request - Invalid token
            raise Unauthorized(resp, data)
        if code == 404: # Tag not found
            raise NotFoundError(resp, data)
        if code >= 500: # Something wrong with the api servers :(
            raise ServerError(resp, data)

    async def _arequest(self, url, **params):
        try:
            async with self.session.get(url, timeout=self.timeout, headers=self.headers, params=params) as resp:
                return self._raise_for_status(resp, await resp.text())
        except asyncio.TimeoutError:
            raise NotResponding()

    async def _wrap_coro(self, arg):
        return arg
    
    def request(self, url, refresh=False, **params):
        if self.using_cache and refresh is False: # Refresh=True forces a request instead of using cache
            cache = self._resolve_cache(url, **params)
            if cache is not None:
                return cache
        if self.is_async: # Return a coroutine
            return self._arequest(url, **params)
        try:
            resp = self.session.get(url, timeout=self.timeout, headers=self.headers, params=params)
            return self._raise_for_status(resp, resp.text)
        except requests.Timeout:
            raise NotResponding()

    def _convert_model(self, data, cached, ts, model):
        if isinstance(data, str):
            return data # version endpoint, not feasable to add refresh functionality.
        if isinstance(data, list): # Extra functionality
            if all(isinstance(x, str) for x in data): # Endpoints endpoint
                return rlist(self, data, cached, ts) # Extra functionality
            return [model(self, d, cached=cached, ts=ts) for d in data]
        else:
            return model(self, data, cached=cached, ts=ts)

    async def _aget_model(self, url, model=None, **params):
        try:
            data, cached, ts = await self.request(url, **params)
        except Exception as e:
            if self.using_cache:
                cache = self._resolve_cache(url, **params)
                if cache is not None:
                    data, cached, ts = cache
            if 'data' not in locals():
                raise e

        return self._convert_model(data, cached, ts, model)

    def _get_model(self, url, model=None, **params):
        if self.is_async: # Return a coroutine
            return self._aget_model(url, model, **params)
        # Otherwise, do everything synchronously.
        try:
            data, cached, ts = self.request(url, **params)
        except Exception as e:
            if self.using_cache:
                cache = self._resolve_cache(url, **params)
                if cache is not None:
                    data, cached, ts = cache
            if 'data' not in locals():
                raise e

        return self._convert_model(data, cached, ts, model)

    def get_version(self):
        return self._get_model(API.VERSION)

    def get_endpoints(self):
        return self._get_model(API.ENDPOINTS)

    @typecasted # Convert to a proper tag
    def get_tournament(self, tag: crtag, **params: keys):
        url = API.TOURNAMENT + '/' + tag
        return self._get_model(url, Tournament, **params)

    @typecasted
    def get_player(self, *tags: crtag, **params: keys):
        url = API.PLAYER + '/' + ','.join(tags)
        return self._get_model(url, Player, **params)

    get_players = get_player

    @typecasted
    def get_clan(self, *tags: crtag, **params: keys):
        url = API.CLAN + '/' + ','.join(tags)
        return self._get_model(url, Clan, **params)

    get_clans = get_clan

    @typecasted # Validate clan search parameters.
    def search_clans(self, **params: clansearch):
        return self._get_model(API.SEARCH, ClanInfo, **params)

    @typecasted # checks if the keys=&exclude= parameters are passed only
    def get_constants(self, **params: keys):
        return self._get_model(API.CONSTANTS, Constants, **params)

    @typecasted
    def get_top_clans(self, country_key='', **params: keys):
        url = API.TOP + '/clans/' + country_key
        return self._get_model(url, ClanInfo, **params)

    @typecasted
    def get_top_players(self, country_key='', **params: keys):
        url = API.TOP + '/players/' + country_key
        return self._get_model(url, PlayerInfo, **params)

    @typecasted
    def get_popular_clans(self, **params: keys):
        url = API.POPULAR + '/clans'
        return self._get_model(url, Clan, **params)

    @typecasted
    def get_popular_players(self, **params: keys):
        url = API.POPULAR + '/players'
        return self._get_model(url, PlayerInfo, **params)

    @typecasted
    def get_popular_tournaments(self, **params: keys):
        url = API.POPULAR + '/tournaments'
        return self._get_model(url, Tournament, **params)

    @typecasted
    def get_auth_stats(self, **params: keys):
        url = API.AUTH + '/stats'
        return self._get_model(url, AuthStats, **params)
