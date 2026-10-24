#!/usr/bin/env python3
# pylint: disable=no-member,method-hidden

import os
import sys
import asyncio
import copy
import shutil
import pathlib
from datetime import datetime, timedelta
from pathlib import Path
from aiohttp import web
from aiohttp.web import GracefulExit
from aiohttp.log import access_logger
import ssl
import socketio
import logging
import json
import re
import time
from urllib.parse import parse_qs, urlencode, urlparse, urlunparse
from watchfiles import awatch

import bg_tasks
import format_catalog
from ytdl import DownloadQueueNotifier, DownloadQueue, Download
from download_options import DownloadOptions
from subscriptions import SubscriptionManager, SubscriptionNotifier, SubscriptionInfo, browse_playlist, coerce_optional_bool
from yt_dlp.version import __version__ as yt_dlp_version

# Authentication imports
from auth import SessionManager, UserStore

log = logging.getLogger('main')

_NIGHTLY_TIME_RE = re.compile(r'^([01]\d|2[0-3]):[0-5]\d$')
_RESTART_FOR_UPDATE = False

def _request_graceful_exit() -> None:
    raise GracefulExit()

def seconds_until_next_daily_time(time_hhmm: str, now: datetime | None = None) -> float:
    now = now or datetime.now()
    hour, minute = map(int, time_hhmm.split(':'))
    target = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if target <= now:
        target += timedelta(days=1)
    return (target - now).total_seconds()

_LOG_LEVEL_NONE = logging.CRITICAL + 10

def parseLogLevel(logLevel):
    if not isinstance(logLevel, str):
        return None
    name = logLevel.strip().upper()
    if name == 'NONE':
        return _LOG_LEVEL_NONE
    level = logging.getLevelNamesMapping().get(name)
    return level if isinstance(level, int) and name != 'NOTSET' else None

if not logging.getLogger().hasHandlers():
    logging.basicConfig(level=parseLogLevel(os.environ.get('LOGLEVEL', 'INFO')) or logging.INFO)

class Config:
    _DEFAULTS = {
        'DOWNLOAD_DIR': '.',
        'AUDIO_DOWNLOAD_DIR': '%%DOWNLOAD_DIR',
        'TEMP_DIR': '%%DOWNLOAD_DIR',
        'DOWNLOAD_DIRS_INDEXABLE': 'false',
        'CUSTOM_DIRS': 'true',
        'CREATE_CUSTOM_DIRS': 'true',
        'CUSTOM_DIRS_EXCLUDE_REGEX': r'(^|/)[.@].*$',
        'DEFAULT_FOLDER': '',
        'DELETE_FILE_ON_TRASHCAN': 'false',
        'STATE_DIR': '.',
        'URL_PREFIX': '',
        'PUBLIC_HOST_URL': 'download/',
        'PUBLIC_HOST_AUDIO_URL': 'audio_download/',
        'OUTPUT_TEMPLATE': '%(title)s.%(ext)s',
        'OUTPUT_TEMPLATE_CHAPTER': '%(title)s - %(section_number)02d - %(section_title)s.%(ext)s',
        'OUTPUT_TEMPLATE_PLAYLIST': '%(playlist_title)s/%(title)s.%(ext)s',
        'OUTPUT_TEMPLATE_CHANNEL': '%(channel)s/%(title)s.%(ext)s',
        'DEFAULT_OPTION_PLAYLIST_ITEM_LIMIT' : '0',
        'SUBSCRIPTION_DEFAULT_CHECK_INTERVAL': '60',
        'SUBSCRIPTION_SCAN_PLAYLIST_END': '50',
        'SUBSCRIPTION_MAX_SEEN_IDS': '50000',
        'CLEAR_COMPLETED_AFTER': '0',
        'YTDL_OPTIONS': '{}',
        'YTDL_OPTIONS_FILE': '',
        'YTDL_OPTIONS_PRESETS': '{}',
        'YTDL_OPTIONS_PRESETS_FILE': '',
        'ALLOW_YTDL_OPTIONS_OVERRIDES': 'false',
        'ALLOW_PRIVATE_ADDRESSES': 'false',
        'CORS_ALLOWED_ORIGINS': '',
        'ROBOTS_TXT': '',
        'HOST': '0.0.0.0',
        'PORT': '8081',
        'HTTPS': 'false',
        'CERTFILE': '',
        'KEYFILE': '',
        'BASE_DIR': '',
        'DEFAULT_THEME': 'auto',
        'MAX_CONCURRENT_DOWNLOADS': '3',
        'LOGLEVEL': 'INFO',
        'ENABLE_ACCESSLOG': 'false',
        'YTDL_NIGHTLY_UPDATE_TIME': '',
        # Auth Config
        'ENABLE_AUTH': 'false',
        'AUTH_USERS_FILE': 'users.json',
        'AUTH_ADMIN_USERNAME': '',
        'AUTH_ADMIN_PASSWORD': '',
        'AUTH_SESSION_SECRET': '',
        'AUTH_SESSION_TTL': '2592000',
    }

    _BOOLEAN = ('DOWNLOAD_DIRS_INDEXABLE', 'CUSTOM_DIRS', 'CREATE_CUSTOM_DIRS', 'HTTPS', 'ENABLE_ACCESSLOG', 'ALLOW_YTDL_OPTIONS_OVERRIDES', 'ALLOW_PRIVATE_ADDRESSES', 'ENABLE_AUTH')

    def __init__(self):
        for k, v in self._DEFAULTS.items():
            setattr(self, k, os.environ.get(k, v))

        for k, v in self.__dict__.items():
            if isinstance(v, str) and v.startswith('%%'):
                setattr(self, k, getattr(self, v[2:]))
            if k in self._BOOLEAN:
                if v not in ('true', 'false', 'True', 'False', 'on', 'off', '1', '0'):
                    log.error(f'Environment variable "{k}" is set to a non-boolean value "{v}"')
                    sys.exit(1)
                setattr(self, k, v in ('true', 'True', 'on', '1'))

        raw = self.DELETE_FILE_ON_TRASHCAN.strip()
        if raw in ('true', 'True', 'on', '1'):
            self.DELETE_FILE_ON_TRASHCAN = 'true'
        elif raw in ('false', 'False', 'off', '0'):
            self.DELETE_FILE_ON_TRASHCAN = 'false'
        elif raw.lower() == 'ask':
            self.DELETE_FILE_ON_TRASHCAN = 'ask'
        else:
            log.error(f'Environment variable "DELETE_FILE_ON_TRASHCAN" is set to an invalid value "{raw}", expected one of "true", "false", "ask"')
            sys.exit(1)

        if self.HOST.strip() == '*':
            self.HOST = ''

        if not self.URL_PREFIX.endswith('/'):
            self.URL_PREFIX += '/'

        for attr in ('DOWNLOAD_DIR', 'AUDIO_DOWNLOAD_DIR', 'TEMP_DIR', 'STATE_DIR'):
            val = getattr(self, attr)
            if isinstance(val, str) and len(val) > 1 and val.endswith('/'):
                setattr(self, attr, val.rstrip('/') or '/')

        if not self.PUBLIC_HOST_AUDIO_URL and self.PUBLIC_HOST_URL:
            self.PUBLIC_HOST_AUDIO_URL = self._DEFAULTS['PUBLIC_HOST_AUDIO_URL']

        for attr in ('PUBLIC_HOST_URL', 'PUBLIC_HOST_AUDIO_URL'):
            val = getattr(self, attr)
            if val and not val.endswith('/'):
                setattr(self, attr, val + '/')

        self.DEFAULT_FOLDER = self.DEFAULT_FOLDER.strip().strip('/')
        if self.DEFAULT_FOLDER and not self.CUSTOM_DIRS:
            log.warning('Ignoring DEFAULT_FOLDER "%s" because CUSTOM_DIRS is not enabled', self.DEFAULT_FOLDER)
            self.DEFAULT_FOLDER = ''

        if self.YTDL_OPTIONS_FILE and self.YTDL_OPTIONS_FILE.startswith('.'):
            self.YTDL_OPTIONS_FILE = str(Path(self.YTDL_OPTIONS_FILE).resolve())
        if self.YTDL_OPTIONS_PRESETS_FILE and self.YTDL_OPTIONS_PRESETS_FILE.startswith('.'):
            self.YTDL_OPTIONS_PRESETS_FILE = str(Path(self.YTDL_OPTIONS_PRESETS_FILE).resolve())
            
        if self.AUTH_USERS_FILE and not os.path.isabs(self.AUTH_USERS_FILE):
            self.AUTH_USERS_FILE = os.path.join(self.STATE_DIR, self.AUTH_USERS_FILE)

        if self.YTDL_NIGHTLY_UPDATE_TIME and not _NIGHTLY_TIME_RE.match(self.YTDL_NIGHTLY_UPDATE_TIME):
            log.error('Environment variable "YTDL_NIGHTLY_UPDATE_TIME" must be HH:MM (24-hour), got "%s"', self.YTDL_NIGHTLY_UPDATE_TIME)
            sys.exit(1)

        for attr in ('LOGLEVEL', 'DEFAULT_THEME'):
            if not getattr(self, attr).strip():
                setattr(self, attr, self._DEFAULTS[attr])

        if parseLogLevel(self.LOGLEVEL) is None:
            log.error('Environment variable "LOGLEVEL" must be one of DEBUG, INFO, WARNING, ERROR, CRITICAL or NONE, got "%s"', self.LOGLEVEL)
            sys.exit(1)

        self.DEFAULT_THEME = self.DEFAULT_THEME.strip().lower()
        if self.DEFAULT_THEME not in ('light', 'dark', 'auto'):
            log.error('Environment variable "DEFAULT_THEME" must be one of light, dark or auto, got "%s"', self.DEFAULT_THEME)
            sys.exit(1)

        try:
            self.CUSTOM_DIRS_EXCLUDE_RE = re.compile(self.CUSTOM_DIRS_EXCLUDE_REGEX) if self.CUSTOM_DIRS_EXCLUDE_REGEX else None
        except re.error as exc:
            log.error('Environment variable "CUSTOM_DIRS_EXCLUDE_REGEX" is not a valid regular expression ("%s"): %s', self.CUSTOM_DIRS_EXCLUDE_REGEX, exc)
            sys.exit(1)
            
        try:
            self.AUTH_SESSION_TTL = int(self.AUTH_SESSION_TTL)
        except ValueError:
            log.error('Environment variable "AUTH_SESSION_TTL" must be an integer value in seconds')
            sys.exit(1)
        if self.AUTH_SESSION_TTL <= 0:
            log.error('Environment variable "AUTH_SESSION_TTL" must be greater than zero')
            sys.exit(1)

        self._validate_int('MAX_CONCURRENT_DOWNLOADS', minimum=1)
        self._validate_int('PORT', minimum=1, maximum=65535)
        self._validate_int('CLEAR_COMPLETED_AFTER', minimum=0)
        self._validate_int('DEFAULT_OPTION_PLAYLIST_ITEM_LIMIT', minimum=0)
        self._validate_int('SUBSCRIPTION_DEFAULT_CHECK_INTERVAL', minimum=1)
        self._validate_int('SUBSCRIPTION_SCAN_PLAYLIST_END', minimum=1)
        self._validate_int('SUBSCRIPTION_MAX_SEEN_IDS', minimum=1)

        self._runtime_overrides = {}

        success,_ = self.load_ytdl_options()
        if not success:
            sys.exit(1)
        success,_ = self.load_ytdl_option_presets()
        if not success:
            sys.exit(1)

    def _validate_int(self, key, *, minimum=None, maximum=None):
        raw = getattr(self, key)
        try:
            value = int(raw)
        except (TypeError, ValueError):
            log.error('Environment variable "%s" must be an integer, got "%s"', key, raw)
            sys.exit(1)
        if minimum is not None and value < minimum:
            log.error('Environment variable "%s" must be >= %d, got "%s"', key, minimum, raw)
            sys.exit(1)
        if maximum is not None and value > maximum:
            log.error('Environment variable "%s" must be <= %d, got "%s"', key, maximum, raw)
            sys.exit(1)
        setattr(self, key, value)

    def set_runtime_override(self, key, value):
        self._runtime_overrides[key] = value
        self.YTDL_OPTIONS[key] = value

    def remove_runtime_override(self, key):
        self._runtime_overrides.pop(key, None)
        self.YTDL_OPTIONS.pop(key, None)

    def _apply_runtime_overrides(self):
        self.YTDL_OPTIONS.update(self._runtime_overrides)

    _FRONTEND_KEYS = (
        'CUSTOM_DIRS',
        'CREATE_CUSTOM_DIRS',
        'DEFAULT_FOLDER',
        'OUTPUT_TEMPLATE_CHAPTER',
        'PUBLIC_HOST_URL',
        'PUBLIC_HOST_AUDIO_URL',
        'DEFAULT_OPTION_PLAYLIST_ITEM_LIMIT',
        'SUBSCRIPTION_DEFAULT_CHECK_INTERVAL',
        'ALLOW_YTDL_OPTIONS_OVERRIDES',
        'DELETE_FILE_ON_TRASHCAN',
    )

    def frontend_safe(self) -> dict:
        return {k: getattr(self, k) for k in self._FRONTEND_KEYS}

    def _env_json(self, key: str):
        raw = os.environ.get(key, '').strip() or self._DEFAULTS[key]
        return json.loads(raw)

    def load_ytdl_options(self) -> tuple[bool, str]:
        try:
            self.YTDL_OPTIONS = self._env_json('YTDL_OPTIONS')
            assert isinstance(self.YTDL_OPTIONS, dict)
        except (json.decoder.JSONDecodeError, AssertionError):
            msg = 'Environment variable YTDL_OPTIONS is invalid'
            log.error(msg)
            return (False, msg)

        if not self.YTDL_OPTIONS_FILE:
            self._apply_runtime_overrides()
            return (True, '')

        log.info(f'Loading yt-dlp custom options from "{self.YTDL_OPTIONS_FILE}"')
        if not os.path.exists(self.YTDL_OPTIONS_FILE):
            msg = f'File "{self.YTDL_OPTIONS_FILE}" not found'
            log.error(msg)
            return (False, msg)
        try:
            with open(self.YTDL_OPTIONS_FILE) as json_data:
                opts = json.load(json_data)
            assert isinstance(opts, dict)
        except (json.decoder.JSONDecodeError, AssertionError):
            msg = 'YTDL_OPTIONS_FILE contents is invalid'
            log.error(msg)
            return (False, msg)

        self.YTDL_OPTIONS.update(opts)
        self._apply_runtime_overrides()
        return (True, '')

    def load_ytdl_option_presets(self) -> tuple[bool, str]:
        try:
            self.YTDL_OPTIONS_PRESETS = self._env_json('YTDL_OPTIONS_PRESETS')
            assert isinstance(self.YTDL_OPTIONS_PRESETS, dict)
            assert all(isinstance(name, str) and isinstance(options, dict) for name, options in self.YTDL_OPTIONS_PRESETS.items())
        except (json.decoder.JSONDecodeError, AssertionError):
            msg = 'Environment variable YTDL_OPTIONS_PRESETS is invalid'
            log.error(msg)
            return (False, msg)

        if not self.YTDL_OPTIONS_PRESETS_FILE:
            return (True, '')

        log.info(f'Loading yt-dlp option presets from "{self.YTDL_OPTIONS_PRESETS_FILE}"')
        if not os.path.exists(self.YTDL_OPTIONS_PRESETS_FILE):
            msg = f'File "{self.YTDL_OPTIONS_PRESETS_FILE}" not found'
            log.error(msg)
            return (False, msg)
        try:
            with open(self.YTDL_OPTIONS_PRESETS_FILE) as json_data:
                opts = json.load(json_data)
            assert isinstance(opts, dict)
            assert all(isinstance(name, str) and isinstance(options, dict) for name, options in opts.items())
        except (json.decoder.JSONDecodeError, AssertionError):
            msg = 'YTDL_OPTIONS_PRESETS_FILE contents is invalid'
            log.error(msg)
            return (False, msg)

        self.YTDL_OPTIONS_PRESETS.update(opts)
        return (True, '')

config = Config()
logging.getLogger().setLevel(parseLogLevel(str(config.LOGLEVEL)) or logging.INFO)

# Auth initialization
user_store = None
session_manager = None
if config.ENABLE_AUTH:
    try:
        user_store = UserStore(config.AUTH_USERS_FILE)
        user_store.ensure_bootstrap_admin(config.AUTH_ADMIN_USERNAME, config.AUTH_ADMIN_PASSWORD)
        session_secret = config.AUTH_SESSION_SECRET
        if not session_secret:
            session_secret = os.urandom(32).hex()
            log.warning("AUTH_SESSION_SECRET is not set; sessions will be invalidated on restart.")
        session_manager = SessionManager(session_secret, config.AUTH_SESSION_TTL)
    except Exception as exc:
        log.error("Failed to initialize authentication subsystem: %s", exc)
        sys.exit(1)

def _route_with_prefix(path: str) -> str:
    return config.URL_PREFIX + path

_AUTH_PROTECTED_EXACT_PATHS = {
    _route_with_prefix('add'),
    _route_with_prefix('delete'),
    _route_with_prefix('start'),
    _route_with_prefix('history'),
    _route_with_prefix('cancel-add'),
    _route_with_prefix('retry'),
    _route_with_prefix('subscribe'),
    _route_with_prefix('subscriptions/update'),
    _route_with_prefix('subscriptions/delete'),
    _route_with_prefix('subscriptions/check'),
    _route_with_prefix('auth/users'),
}
_AUTH_PROTECTED_PREFIX_PATHS = (
    _route_with_prefix('download/'),
    _route_with_prefix('audio_download/'),
    _route_with_prefix('socket.io'),
    _route_with_prefix('auth/users/'),
)

def public_user_from_session_user(session_user):
    return {
        'username': session_user['username'],
        'role': session_user['role'],
    }

@web.middleware
async def auth_middleware(request, handler):
    request['auth_user'] = None
    if not config.ENABLE_AUTH:
        return await handler(request)

    session_user = session_manager.user_from_request(request) if session_manager is not None else None
    user = None
    if session_user is not None and user_store is not None:
        persisted_user = user_store.get_session_user(session_user['username'])
        if persisted_user is not None and persisted_user.get('session_version') == session_user.get('session_version'):
            user = public_user_from_session_user(persisted_user)
            
    if user is not None:
        request['auth_user'] = user

    path = request.path
    is_protected = (
        path in _AUTH_PROTECTED_EXACT_PATHS
        or any(path.startswith(prefix) for prefix in _AUTH_PROTECTED_PREFIX_PATHS)
    )

    if is_protected and user is None:
        raise web.HTTPUnauthorized()

    return await handler(request)

def require_authenticated_user(request):
    user = request.get('auth_user')
    if config.ENABLE_AUTH and user is None:
        raise web.HTTPUnauthorized()
    return user

def require_admin(request):
    user = require_authenticated_user(request)
    if user is None or user.get('role') != 'admin':
        raise web.HTTPForbidden()
    return user

class ObjectSerializer(json.JSONEncoder):
    def default(self, obj):
        to_public = getattr(obj, 'to_public_dict', None)
        if callable(to_public):
            return to_public()
        if hasattr(obj, '__dict__'):
            return obj.__dict__
        elif hasattr(obj, '__iter__') and not isinstance(obj, (str, bytes)):
            try:
                return list(obj)
            except Exception:
                pass
        return json.JSONEncoder.default(self, obj)

serializer = ObjectSerializer()

_STATE_DIR_REAL = os.path.realpath(config.STATE_DIR)

def _is_within_state_dir(real_target: str) -> bool:
    return real_target == _STATE_DIR_REAL or real_target.startswith(_STATE_DIR_REAL + os.sep)

@web.middleware
async def state_dir_guard(request, handler):
    for prefix, base in (
        (config.URL_PREFIX + 'download/', config.DOWNLOAD_DIR),
        (config.URL_PREFIX + 'audio_download/', config.AUDIO_DOWNLOAD_DIR),
    ):
        if request.path.startswith(prefix):
            rel = request.path[len(prefix):]
            target = os.path.realpath(os.path.join(base, rel))
            if _is_within_state_dir(target):
                raise web.HTTPNotFound()
            break
    return await handler(request)

def _origin_is_trusted(origin, sec_fetch_site, hosts) -> bool:
    if origin and ('*' in _cors_origins or origin in _cors_origins):
        return True
    if sec_fetch_site:
        return sec_fetch_site in ('same-origin', 'none')
    if not origin:
        return True
    try:
        netloc = urlparse(origin).netloc.lower()
    except ValueError:
        return False
    return bool(netloc) and any(h and h.split(',')[0].strip().lower() == netloc for h in hosts)

_SAFE_METHODS = ('GET', 'HEAD', 'OPTIONS')

def _log_refusal(what, origin, sec_fetch_site, hosts):
    if sec_fetch_site:
        log.warning('Refused cross-origin %s from %s. If this is a bookmarklet or extension you use, add its origin to CORS_ALLOWED_ORIGINS.', what, origin)
        return
    host, forwarded_host = hosts
    log.warning(
        'Refused cross-origin %s from %s: it was addressed to Host %s%s. If %s is where you '
        'open MeTube, a reverse proxy in front of it is rewriting the Host header; have it '
        'pass the original one (in nginx: proxy_set_header Host $http_host), or add that '
        'address to CORS_ALLOWED_ORIGINS. If it is a bookmarklet or extension you use, add '
        'its origin to CORS_ALLOWED_ORIGINS.',
        what, origin, host, f' (X-Forwarded-Host {forwarded_host})' if forwarded_host else '', origin)

@web.middleware
async def cross_origin_guard(request, handler):
    if request.method not in _SAFE_METHODS:
        origin = request.headers.get('Origin')
        hosts = (request.headers.get('Host'), request.headers.get('X-Forwarded-Host'))
        sec_fetch_site = request.headers.get('Sec-Fetch-Site')
        if not _origin_is_trusted(origin, sec_fetch_site, hosts):
            _log_refusal(f'{request.method} {request.path}', origin, sec_fetch_site, hosts)
            headers = {}
            if origin:
                headers = {
                    'Access-Control-Allow-Origin': origin,
                    'Access-Control-Allow-Credentials': 'true',
                    'Vary': 'Origin',
                }
            raise web.HTTPForbidden(
                reason='Cross-origin request not allowed', headers=headers,
                text=f'MeTube refused this request from {origin}. Add that origin to CORS_ALLOWED_ORIGINS to allow it.')
    return await handler(request)

def _socketio_origin_allowed(origin, environ):
    hosts = (environ.get('HTTP_HOST'), environ.get('HTTP_X_FORWARDED_HOST'))
    sec_fetch_site = environ.get('HTTP_SEC_FETCH_SITE')
    if _origin_is_trusted(origin, sec_fetch_site, hosts):
        return True
    if not environ.get('metube.origin_refusal_logged'):
        environ['metube.origin_refusal_logged'] = True
        _log_refusal('Socket.IO connection', origin, sec_fetch_site, hosts)
    return False

app = web.Application(middlewares=[cross_origin_guard, state_dir_guard] + ([auth_middleware] if config.ENABLE_AUTH else []))
_cors_origins = [o.strip() for o in config.CORS_ALLOWED_ORIGINS.split(',') if o.strip()] if config.CORS_ALLOWED_ORIGINS else []
if '*' in _cors_origins and len(_cors_origins) > 1:
    log.warning(
        "CORS_ALLOWED_ORIGINS mixes '*' with named origins %s. '*' wins, and credentialed "
        "cross-origin requests stay disabled for every origin in the list. Remove '*' if you "
        "need a bookmarklet to reach an authenticated instance.",
        [o for o in _cors_origins if o != '*'])
sio = socketio.AsyncServer(cors_allowed_origins=_socketio_origin_allowed)
sio.attach(app, socketio_path=config.URL_PREFIX + 'socket.io')
routes = web.RouteTableDef()

class Notifier(DownloadQueueNotifier):
    def __init__(self, room=None):
        self.room = room

    async def _emit(self, event, payload):
        if self.room is not None:
            await sio.emit(event, payload, to=self.room)
        else:
            await sio.emit(event, payload)

    async def added(self, dl):
        log.info(f"Notifier: Download added - {dl.title}")
        await self._emit('added', serializer.encode(dl))

    async def updated(self, dl):
        log.debug(f"Notifier: Download updated - {dl.title}")
        await self._emit('updated', serializer.encode(dl))

    async def completed(self, dl):
        log.info(f"Notifier: Download completed - {dl.title}")
        await self._emit('completed', serializer.encode(dl))

    async def canceled(self, id):
        log.info(f"Notifier: Download canceled - {id}")
        await self._emit('canceled', serializer.encode(id))

    async def cleared(self, id):
        log.info(f"Notifier: Download cleared - {id}")
        await self._emit('cleared', serializer.encode(id))

class MetubeSubscriptionNotifier(SubscriptionNotifier):
    def __init__(self, room=None):
        self.room = room

    async def _emit(self, event, payload):
        if self.room is not None:
            await sio.emit(event, payload, to=self.room)
        else:
            await sio.emit(event, payload)

    async def subscription_added(self, sub: SubscriptionInfo):
        log.info("Subscription added: %s", sub.name)
        await self._emit('subscription_added', serializer.encode(sub.to_public_dict()))

    async def subscription_updated(self, sub: SubscriptionInfo):
        await self._emit('subscription_updated', serializer.encode(sub.to_public_dict()))

    async def subscription_removed(self, sub_id: str):
        log.info("Subscription removed: %s", sub_id)
        await self._emit('subscription_removed', serializer.encode(sub_id))

    async def subscriptions_all(self, subs: list[SubscriptionInfo]):
        await self._emit('subscriptions_all', serializer.encode([s.to_public_dict() for s in subs]))

class UserEnvironmentRegistry:
    def __init__(self, base_config):
        self.base_config = base_config
        self._envs = {}
        self._lock = asyncio.Lock()

    def room(self, username):
        return f'user:{username}'

    def user_paths(self, username):
        download_dir = os.path.join(self.base_config.DOWNLOAD_DIR, username)
        audio_download_dir = download_dir if self.base_config.AUDIO_DOWNLOAD_DIR == self.base_config.DOWNLOAD_DIR else os.path.join(self.base_config.AUDIO_DOWNLOAD_DIR, username)
        temp_dir = download_dir if self.base_config.TEMP_DIR == self.base_config.DOWNLOAD_DIR else os.path.join(self.base_config.TEMP_DIR, username)
        state_dir = os.path.join(self.base_config.STATE_DIR, 'users', username)
        return {
            'download_dir': download_dir,
            'audio_download_dir': audio_download_dir,
            'temp_dir': temp_dir,
            'state_dir': state_dir,
        }

    def _build_user_config(self, username):
        paths = self.user_paths(username)
        user_config = copy.copy(self.base_config)
        user_config.DOWNLOAD_DIR = paths['download_dir']
        user_config.AUDIO_DOWNLOAD_DIR = paths['audio_download_dir']
        user_config.TEMP_DIR = paths['temp_dir']
        user_config.STATE_DIR = paths['state_dir']
        for directory in (user_config.DOWNLOAD_DIR, user_config.AUDIO_DOWNLOAD_DIR, user_config.TEMP_DIR, user_config.STATE_DIR):
            os.makedirs(directory, exist_ok=True)
        return user_config

    async def get(self, username):
        env = self._envs.get(username)
        if env is not None:
            return env
        async with self._lock:
            env = self._envs.get(username)
            if env is not None:
                return env
            user_config = self._build_user_config(username)
            room_name = self.room(username)
            queue = DownloadQueue(user_config, Notifier(room=room_name))
            submgr = SubscriptionManager(user_config, queue, MetubeSubscriptionNotifier(room=room_name))
            env = {'queue': queue, 'submgr': submgr, 'config': user_config}
            self._envs[username] = env
            await queue.initialize()
            submgr.start_background_loop()
            return env

    def get_if_present(self, username):
        return self._envs.get(username)

    async def remove(self, username):
        async with self._lock:
            env = self._envs.pop(username, None)
            if env:
                env['submgr'].close()
                env['queue'].close()


dqueue = DownloadQueue(config, Notifier()) if not config.ENABLE_AUTH else None
submgr = SubscriptionManager(config, dqueue, MetubeSubscriptionNotifier()) if not config.ENABLE_AUTH else None
user_envs = UserEnvironmentRegistry(config) if config.ENABLE_AUTH else None


async def env_for_request(request):
    if config.ENABLE_AUTH:
        user = require_authenticated_user(request)
        return await user_envs.get(user['username'])
    return {'queue': dqueue, 'submgr': submgr, 'config': config}


async def _download_queue_startup(app):
    if config.ENABLE_AUTH and user_store is not None:
        for user in user_store.list_users():
            await user_envs.get(user['username'])
    elif dqueue is not None:
        await dqueue.initialize()

async def _shutdown_download_manager(app):
    if config.ENABLE_AUTH and user_store is not None:
        for user in user_store.list_users():
            env = user_envs.get_if_present(user['username'])
            if env:
                env['queue'].close()
    elif dqueue is not None:
        dqueue.close()
    Download.shutdown_manager()

app.on_startup.append(_download_queue_startup)
app.on_cleanup.append(_shutdown_download_manager)

async def _shutdown_subscriptions(app):
    if config.ENABLE_AUTH and user_store is not None:
        for user in user_store.list_users():
            env = user_envs.get_if_present(user['username'])
            if env:
                env['submgr'].close()
    elif submgr is not None:
        submgr.close()

app.on_cleanup.append(_shutdown_subscriptions)

async def _subscription_loop_startup(app):
    if not config.ENABLE_AUTH and submgr is not None:
        submgr.start_background_loop()
app.on_startup.append(_subscription_loop_startup)

async def _schedule_nightly_update() -> None:
    global _RESTART_FOR_UPDATE
    time_hhmm = config.YTDL_NIGHTLY_UPDATE_TIME
    if not time_hhmm:
        return
    delay = seconds_until_next_daily_time(time_hhmm)
    log.info('Next yt-dlp nightly update in %.0f seconds (at %s local time)', delay, time_hhmm)
    await asyncio.sleep(delay)
    log.info('Scheduled yt-dlp nightly update: requesting restart')
    _RESTART_FOR_UPDATE = True
    asyncio.get_running_loop().call_soon(_request_graceful_exit)

async def _start_nightly_update_schedule(app):
    bg_tasks.create_task(_schedule_nightly_update(), name="nightly_update_schedule")

app.on_startup.append(_start_nightly_update_schedule)

def get_options_update_time(success=True, msg=''):
    result = {
        'success': success,
        'msg': msg,
        'update_time': None
    }
    if config.YTDL_OPTIONS_FILE and os.path.exists(config.YTDL_OPTIONS_FILE):
        try:
            result['update_time'] = os.path.getmtime(config.YTDL_OPTIONS_FILE)
        except (OSError, IOError) as e:
            log.warning(f"Could not get modification time for {config.YTDL_OPTIONS_FILE}: {e}")
            result['update_time'] = None
    return result

def _options_file_signature(path: str):
    try:
        st = os.stat(path)
    except OSError:
        return None
    return (st.st_dev, st.st_ino, st.st_mtime_ns, st.st_size)

async def _watch_options_file(on_change, *, debounce: int = 1600, stop_event=None) -> None:
    path = os.path.abspath(config.YTDL_OPTIONS_FILE)
    last = _options_file_signature(path)
    async for _changes in awatch(
        os.path.dirname(path), recursive=False, debounce=debounce, stop_event=stop_event,
    ):
        current = _options_file_signature(path)
        if current == last:
            continue
        last = current
        await on_change()

async def _reload_ytdl_options() -> None:
    success, msg = config.load_ytdl_options()
    await sio.emit('ytdl_options_changed', serializer.encode(get_options_update_time(success, msg)))

async def watch_files():
    log.info(f'Starting Watch File: {config.YTDL_OPTIONS_FILE}')
    bg_tasks.create_task(_watch_options_file(_reload_ytdl_options), name="watch_ytdl_options_file")

async def _watch_files_startup(app):
    await watch_files()

if config.YTDL_OPTIONS_FILE:
    app.on_startup.append(_watch_files_startup)

async def _read_json_request(request: web.Request) -> dict:
    try:
        post = await request.json()
    except json.JSONDecodeError as exc:
        raise web.HTTPBadRequest(reason='Invalid JSON request body') from exc
    if not isinstance(post, dict):
        raise web.HTTPBadRequest(reason='JSON request body must be an object')
    return post

def _bool_field(post: dict, key: str, default: bool) -> bool:
    value = post.get(key)
    if value is None or (isinstance(value, str) and not value.strip()):
        return default
    try:
        return coerce_optional_bool(value, default=default, field_name=key)
    except ValueError as exc:
        raise web.HTTPBadRequest(reason=str(exc)) from exc

def _str_field(post: dict, key: str, default):
    value = post.get(key)
    if value is None:
        return default
    if not isinstance(value, str):
        raise web.HTTPBadRequest(reason=f"'{key}' must be a string")
    return value

def _playlist_item_limit_field(post: dict, env_config=config) -> int:
    value = post.get('playlist_item_limit')
    if value is None:
        value = env_config.DEFAULT_OPTION_PLAYLIST_ITEM_LIMIT
    if isinstance(value, bool):
        raise web.HTTPBadRequest(reason='playlist_item_limit must be an integer')
    try:
        value = int(value)
    except (TypeError, ValueError) as exc:
        raise web.HTTPBadRequest(reason='playlist_item_limit must be an integer') from exc
    if value < 0:
        raise web.HTTPBadRequest(reason='playlist_item_limit must not be negative')
    return value

def _parse_ytdl_options_overrides(value, *, enabled: bool) -> dict:
    if value is None or value == '':
        return {}
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except json.JSONDecodeError as exc:
            raise web.HTTPBadRequest(reason='ytdl_options_overrides must be valid JSON') from exc
    if not isinstance(value, dict):
        raise web.HTTPBadRequest(reason='ytdl_options_overrides must be a JSON object')
    if value and not enabled:
        raise web.HTTPBadRequest(reason='ytdl_options_overrides are disabled')
    return value

_YOUTUBE_T_COMPACT_RE = re.compile(r'^(?:(\d+)h)?(?:(\d+)m)?(?:(\d+)(?:s)?)?$', re.IGNORECASE)

def _parse_youtube_t_compact(value: str) -> float | None:
    v = value.strip()
    if not v:
        return None
    if re.fullmatch(r'-?\d+(\.\d+)?', v):
        sec = float(v)
        return sec if sec >= 0 else None
    m = _YOUTUBE_T_COMPACT_RE.match(v)
    if m and any(m.groups()):
        hours = int(m.group(1) or 0)
        minutes = int(m.group(2) or 0)
        seconds = int(m.group(3) or 0)
        total = hours * 3600 + minutes * 60 + seconds
        return float(total) if total >= 0 else None
    return None

def _parse_clock_timestamp(s: str) -> float:
    part = s.strip()
    if not part:
        raise ValueError('empty timestamp')
    segments = part.split(':')
    if len(segments) > 3:
        raise ValueError('too many segments')
    try:
        nums = [float(x) for x in segments]
    except ValueError as exc:
        raise ValueError('invalid number') from exc
    if any(x < 0 for x in nums):
        raise ValueError('negative segment')
    if len(segments) == 1:
        return nums[0]
    if len(segments) == 2:
        return nums[0] * 60 + nums[1]
    return nums[0] * 3600 + nums[1] * 60 + nums[2]

def _parse_clip_timestamp_value(value) -> float:
    if isinstance(value, bool):
        raise web.HTTPBadRequest(reason='clip timestamp must be a number or string')
    if isinstance(value, (int, float)):
        if value < 0:
            raise web.HTTPBadRequest(reason='clip timestamp must be non-negative')
        return float(value)
    s = str(value).strip()
    if not s:
        raise web.HTTPBadRequest(reason='clip timestamp cannot be empty')
    if ':' in s:
        try:
            return _parse_clock_timestamp(s)
        except ValueError as exc:
            raise web.HTTPBadRequest(reason='invalid clip timestamp format') from exc
    compact = _parse_youtube_t_compact(s)
    if compact is not None:
        return compact
    raise web.HTTPBadRequest(reason='invalid clip timestamp format')

def _optional_clip_field(raw) -> float | None:
    if raw is None:
        return None
    if isinstance(raw, str) and not raw.strip():
        return None
    return _parse_clip_timestamp_value(raw)

def _clip_field_provided_in_post(raw) -> bool:
    if raw is None:
        return False
    if isinstance(raw, str) and not raw.strip():
        return False
    return True

def _extract_t_query_from_url(url: str) -> tuple[str, float | None]:
    try:
        parsed = urlparse(url)
        params = parse_qs(parsed.query)
    except Exception:
        return url, None
    host = (parsed.hostname or '').lower()
    if not (host in ('youtu.be', 'youtube.com') or host.endswith('.youtube.com')):
        return url, None
    t_values = params.get('t')
    if not t_values:
        return url, None
    start = _parse_youtube_t_compact(t_values[0])
    if start is None:
        return url, None
    filtered = {k: v for k, v in params.items() if k != 't'}
    new_query = urlencode(filtered, doseq=True)
    cleaned = urlunparse((
        parsed.scheme,
        parsed.netloc,
        parsed.path,
        parsed.params,
        new_query,
        parsed.fragment,
    ))
    return cleaned, float(start)

def _parse_ytdl_options_presets(post: dict) -> list[str]:
    raw = post.get('ytdl_options_presets')
    if raw is None:
        raw = post.get('ytdl_options_preset')
    if raw is None:
        return []
    if isinstance(raw, list):
        return [str(x).strip() for x in raw if str(x).strip()]
    if isinstance(raw, str):
        s = raw.strip()
        return [s] if s else []
    raise web.HTTPBadRequest(reason='ytdl_options_presets must be a JSON array of strings (or legacy ytdl_options_preset string)')

def _require_configured_presets(names: list[str]) -> None:
    for preset_name in names:
        if preset_name not in config.YTDL_OPTIONS_PRESETS:
            raise web.HTTPBadRequest(reason='ytdl_options_presets must only contain configured preset names')

def _migrate_legacy_request(post: dict) -> dict:
    if "download_type" in post:
        return post
    old_format = str(post.get("format") or "any").strip().lower()
    old_quality = str(post.get("quality") or "best").strip().lower()
    old_video_codec = str(post.get("video_codec") or "auto").strip().lower()
    if old_format in format_catalog.format_ids('audio') and old_format != 'auto':
        post["download_type"] = "audio"
        post["codec"] = "auto"
        post["format"] = old_format
    elif old_format == "thumbnail":
        post["download_type"] = "thumbnail"
        post["codec"] = "auto"
        post["format"] = "jpg"
        post["quality"] = "best"
    elif old_format == "captions":
        post["download_type"] = "captions"
        post["codec"] = "auto"
        post["format"] = str(post.get("subtitle_format") or "srt").strip().lower()
        post["quality"] = "best"
    else:
        post["download_type"] = "video"
        post["codec"] = old_video_codec
        if old_quality == "best_ios":
            post["format"] = "ios"
            post["quality"] = "best"
        elif old_quality == "audio":
            post["download_type"] = "audio"
            post["codec"] = "auto"
            post["format"] = "m4a"
            post["quality"] = "best"
        else:
            post["format"] = old_format
            post["quality"] = old_quality
    return post


def parse_download_options(post: dict, env_config=config) -> tuple[str, DownloadOptions]:
    post = _migrate_legacy_request(dict(post))
    url = post.get('url')
    download_type = post.get('download_type')
    codec = post.get('codec')
    format = post.get('format')
    quality = post.get('quality')
    if not url or not quality or not download_type:
        raise web.HTTPBadRequest(reason="missing 'url', 'download_type', or 'quality'")
    url = str(url).strip()
    folder = _str_field(post, 'folder', None)
    custom_name_prefix = _str_field(post, 'custom_name_prefix', '')
    playlist_item_limit = _playlist_item_limit_field(post, env_config=env_config)
    auto_start = _bool_field(post, 'auto_start', True)
    split_by_chapters = _bool_field(post, 'split_by_chapters', False)
    sponsorblock = _bool_field(post, 'sponsorblock', False)
    audio_tags = str(post.get('audio_tags') or format_catalog.choice_default('audio', 'audio_tags')).strip().lower()
    chapter_template = _str_field(post, 'chapter_template', env_config.OUTPUT_TEMPLATE_CHAPTER)
    subtitle_language = post.get('subtitle_language')
    subtitle_mode = post.get('subtitle_mode')
    ytdl_options_overrides = post.get('ytdl_options_overrides')
    video_password = post.get('video_password')

    if subtitle_language is None:
        subtitle_language = format_catalog.choice_default('captions', 'subtitle_language')
    if subtitle_mode is None:
        subtitle_mode = format_catalog.choice_default('captions', 'subtitle_mode')
    download_type = str(download_type).strip().lower()
    codec = str(codec or format_catalog.choice_default('video', 'codec')).strip().lower()
    format = str(format or '').strip().lower()
    quality = str(quality).strip().lower()
    subtitle_language = str(subtitle_language).strip()
    subtitle_mode = str(subtitle_mode).strip()
    ytdl_options_presets = _parse_ytdl_options_presets(post)
    ytdl_options_overrides = _parse_ytdl_options_overrides(
        ytdl_options_overrides,
        enabled=env_config.ALLOW_YTDL_OPTIONS_OVERRIDES,
    )
    if video_password == '':
        video_password = None
    if video_password is not None and not isinstance(video_password, str):
        raise web.HTTPBadRequest(reason='video_password must be a string')

    _require_configured_presets(ytdl_options_presets)

    try:
        format_catalog.check_subtitle_language(subtitle_language)
        format_catalog.check_subtitle_mode(subtitle_mode)
        format_catalog.check_audio_tags(audio_tags)
        codec, format, quality = format_catalog.resolve_selection(download_type, codec, format, quality)
    except format_catalog.InvalidOption as exc:
        raise web.HTTPBadRequest(reason=str(exc)) from exc

    clip_start_raw = post.get('clip_start')
    clip_end_raw = post.get('clip_end')
    clip_start: float | None
    clip_end: float | None
    if download_type in ('captions', 'thumbnail'):
        if _clip_field_provided_in_post(clip_start_raw) or _clip_field_provided_in_post(clip_end_raw):
            raise web.HTTPBadRequest(reason='clip_start and clip_end are only supported for video and audio downloads')
        clip_start = None
        clip_end = None
    else:
        cleaned_url, url_t = _extract_t_query_from_url(url)
        if url_t is not None:
            url = cleaned_url
        explicit_start = _optional_clip_field(clip_start_raw)
        explicit_end = _optional_clip_field(clip_end_raw)
        explicit_start_provided = _clip_field_provided_in_post(clip_start_raw)
        explicit_end_provided = _clip_field_provided_in_post(clip_end_raw)
        if explicit_start_provided:
            clip_start = explicit_start
        elif explicit_end_provided:
            clip_start = 0.0
        elif url_t is not None:
            clip_start = url_t
        else:
            clip_start = None
        clip_end = explicit_end
        if clip_end is not None and clip_start is None:
            clip_start = 0.0
        if clip_start is not None and clip_end is not None and clip_end <= clip_start:
            raise web.HTTPBadRequest(reason='clip_end must be greater than clip_start')

    return url, DownloadOptions(
        download_type=download_type, codec=codec, format=format, quality=quality,
        folder=folder, custom_name_prefix=custom_name_prefix, playlist_item_limit=playlist_item_limit,
        auto_start=auto_start, split_by_chapters=split_by_chapters, sponsorblock=sponsorblock,
        audio_tags=audio_tags, chapter_template=chapter_template, subtitle_language=subtitle_language,
        subtitle_mode=subtitle_mode, ytdl_options_presets=ytdl_options_presets,
        ytdl_options_overrides=ytdl_options_overrides, clip_start=clip_start, clip_end=clip_end,
        video_password=video_password,
    )

# Auth Endpoints

@routes.get(config.URL_PREFIX + 'auth/me')
async def auth_me(request):
    user = request.get('auth_user')
    return web.json_response({
        'enabled': config.ENABLE_AUTH,
        'authenticated': (not config.ENABLE_AUTH) or user is not None,
        'user': user,
    })

@routes.post(config.URL_PREFIX + 'auth/login')
async def auth_login(request):
    if not config.ENABLE_AUTH:
        return web.json_response({'status': 'ok', 'enabled': False})
    try:
        post = await request.json()
    except Exception as exc:
        raise web.HTTPBadRequest() from exc

    username = str(post.get('username', '')).strip()
    password = str(post.get('password', ''))
    session_user = user_store.verify_credentials_session(username, password) if user_store is not None else None
    if session_user is None:
        return web.json_response({'status': 'error', 'msg': 'Invalid username or password.'}, status=401)

    response_user = public_user_from_session_user(session_user)
    response = web.json_response({'status': 'ok', 'user': response_user})
    session_manager.set_cookie(response, session_user)
    return response

@routes.post(config.URL_PREFIX + 'auth/logout')
async def auth_logout(request):
    response = web.json_response({'status': 'ok'})
    if config.ENABLE_AUTH and session_manager is not None:
        session_manager.clear_cookie(response)
    return response

@routes.get(config.URL_PREFIX + 'auth/users')
async def auth_users(request):
    if not config.ENABLE_AUTH:
        raise web.HTTPNotFound()
    require_admin(request)
    return web.json_response({'users': user_store.list_users()})

@routes.post(config.URL_PREFIX + 'auth/users')
async def auth_users_create(request):
    if not config.ENABLE_AUTH:
        raise web.HTTPNotFound()
    require_admin(request)
    try:
        post = await request.json()
    except Exception as exc:
        raise web.HTTPBadRequest() from exc

    username = str(post.get('username', '')).strip()
    password = str(post.get('password', ''))
    role = str(post.get('role', 'user')).strip().lower()
    try:
        user = user_store.create_user(username, password, role)
    except ValueError as exc:
        return web.json_response({'status': 'error', 'msg': str(exc)}, status=400)
    return web.json_response({'status': 'ok', 'user': user}, status=201)

async def invalidate_user_sessions(username):
    if not config.ENABLE_AUTH or user_envs is None:
        return
    room = user_envs.room(username)
    payload = serializer.encode({'username': username})
    await sio.emit('session_invalidated', payload, to=room)

@routes.delete(config.URL_PREFIX + 'auth/users/{username}')
async def auth_users_delete(request):
    if not config.ENABLE_AUTH:
        raise web.HTTPNotFound()
    admin_user = require_admin(request)
    username = str(request.match_info.get('username', '')).strip()
    if not username:
        raise web.HTTPBadRequest(text='username is required')
    if username == admin_user.get('username'):
        return web.json_response({'status': 'error', 'msg': 'You cannot delete the currently logged-in account.'}, status=400)

    delete_downloads_raw = request.query.get('delete_downloads', 'false')
    try:
        delete_downloads = coerce_optional_bool(delete_downloads_raw, False, 'delete_downloads')
    except ValueError as exc:
        return web.json_response({'status': 'error', 'msg': str(exc)}, status=400)

    existing_env = user_envs.get_if_present(username) if user_envs is not None else None
    if existing_env is not None and ((not existing_env['queue'].queue.empty()) or (not existing_env['queue'].pending.empty())):
        return web.json_response(
            {'status': 'error', 'msg': f"User '{username}' has active or pending downloads. Cancel or finish them before deletion."},
            status=409,
        )

    try:
        deleted_user = user_store.delete_user(username)
    except (KeyError, ValueError) as exc:
        return web.json_response({'status': 'error', 'msg': str(exc)}, status=404 if isinstance(exc, KeyError) else 400)

    await invalidate_user_sessions(username)

    if user_envs is not None:
        paths = user_envs.user_paths(username)
        await user_envs.remove(username)
    else:
        paths = {'download_dir': '', 'audio_download_dir': '', 'temp_dir': '', 'state_dir': ''}

    if paths['state_dir']:
        shutil.rmtree(paths['state_dir'], ignore_errors=True)

    if delete_downloads:
        for directory in set((paths['download_dir'], paths['audio_download_dir'], paths['temp_dir'])):
            if directory:
                shutil.rmtree(directory, ignore_errors=True)

    return web.json_response({'status': 'ok', 'user': deleted_user, 'deleted_downloads': delete_downloads})

@routes.post(config.URL_PREFIX + 'auth/users/{username}/password')
async def auth_users_change_password(request):
    if not config.ENABLE_AUTH:
        raise web.HTTPNotFound()

    actor = require_authenticated_user(request)
    username = str(request.match_info.get('username', '')).strip()
    if not username:
        raise web.HTTPBadRequest(text='username is required')
    try:
        post = await request.json()
    except Exception as exc:
        raise web.HTTPBadRequest() from exc

    new_password = str(post.get('new_password', ''))
    current_password = str(post.get('current_password', ''))

    is_admin = actor.get('role') == 'admin'
    is_self = actor.get('username') == username
    if (not is_admin) and (not is_self):
        raise web.HTTPForbidden()

    if (not is_admin) and is_self:
        if not current_password:
            return web.json_response({'status': 'error', 'msg': 'Current password is required.'}, status=400)
        if not user_store.verify_user_password(username, current_password):
            return web.json_response({'status': 'error', 'msg': 'Current password is incorrect.'}, status=401)

    try:
        updated_user = user_store.set_password(username, new_password)
    except ValueError as exc:
        return web.json_response({'status': 'error', 'msg': str(exc)}, status=400)
    except KeyError as exc:
        return web.json_response({'status': 'error', 'msg': str(exc)}, status=404)

    await invalidate_user_sessions(username)

    response = web.json_response({'status': 'ok', 'user': updated_user, 'force_relogin': actor.get('username') == username})
    if actor.get('username') == username and session_manager is not None:
        session_manager.clear_cookie(response)
    return response

# Main Action Endpoints

@routes.post(config.URL_PREFIX + 'add')
async def add(request):
    log.info("Received request to add download")
    post = await _read_json_request(request)
    env = await env_for_request(request)
    try:
        url, options = parse_download_options(post, env_config=env['config'])
    except web.HTTPBadRequest as e:
        log.error("Bad request: %s", e.reason)
        raise
    log.info("Add download request: type=%s quality=%s format=%s", options.download_type, options.quality, options.format)
    status = await env['queue'].add(url, options)
    return web.Response(text=serializer.encode(status))

@routes.get(config.URL_PREFIX + 'formats')
async def formats(request):
    return web.Response(text=serializer.encode(format_catalog.public_catalog()), content_type='application/json')

@routes.post(config.URL_PREFIX + 'browse')
async def browse(request):
    post = await _read_json_request(request)
    env = await env_for_request(request)
    url = str(post.get('url') or '').strip()
    if not url:
        raise web.HTTPBadRequest(reason="missing 'url'")
    ytdl_options_presets = _parse_ytdl_options_presets(post)
    _require_configured_presets(ytdl_options_presets)
    ytdl_options_overrides = _parse_ytdl_options_overrides(
        post.get('ytdl_options_overrides'),
        enabled=env['config'].ALLOW_YTDL_OPTIONS_OVERRIDES,
    )
    playlist_item_limit = _playlist_item_limit_field(post, env_config=env['config'])
    status = await browse_playlist(env['config'], url, playlist_item_limit, ytdl_options_presets, ytdl_options_overrides)
    return web.Response(text=serializer.encode(status))

@routes.get(config.URL_PREFIX + 'presets')
async def presets(request):
    return web.Response(text=serializer.encode({'presets': sorted(config.YTDL_OPTIONS_PRESETS.keys())}), content_type='application/json')

@routes.post(config.URL_PREFIX + 'cancel-add')
async def cancel_add(request):
    env = await env_for_request(request)
    env['queue'].cancel_add()
    return web.Response(text=serializer.encode({'status': 'ok'}), content_type='application/json')

@routes.post(config.URL_PREFIX + 'retry')
async def retry(request):
    post = await _read_json_request(request)
    env = await env_for_request(request)
    status = await env['queue'].retry(_require_id(post))
    return web.Response(text=serializer.encode(status), content_type='application/json')

@routes.post(config.URL_PREFIX + 'subscribe')
async def subscribe(request):
    post = await _read_json_request(request)
    env = await env_for_request(request)
    url, options = parse_download_options(post, env_config=env['config'])
    cic = post.get('check_interval_minutes')
    if cic is None:
        cic = env['config'].SUBSCRIPTION_DEFAULT_CHECK_INTERVAL
    try:
        cic = int(cic)
    except (TypeError, ValueError) as exc:
        raise web.HTTPBadRequest(reason='check_interval_minutes must be an integer') from exc
    if cic < 1:
        raise web.HTTPBadRequest(reason='check_interval_minutes must be at least 1')
    skip_subscriber_only = _bool_field(post, 'skip_subscriber_only', False)

    clip_given = _clip_field_provided_in_post(post.get('clip_start')) or _clip_field_provided_in_post(post.get('clip_end'))
    if not clip_given:
        options = options.replace(clip_start=None, clip_end=None)

    result = await env['submgr'].add_subscription(
        url,
        check_interval_minutes=cic,
        download_type=options.download_type,
        codec=options.codec,
        format=options.format,
        quality=options.quality,
        folder=options.folder or '',
        custom_name_prefix=options.custom_name_prefix,
        auto_start=options.auto_start,
        playlist_item_limit=options.playlist_item_limit,
        split_by_chapters=options.split_by_chapters,
        chapter_template=options.chapter_template,
        subtitle_language=options.subtitle_language,
        subtitle_mode=options.subtitle_mode,
        ytdl_options_presets=list(options.ytdl_options_presets),
        ytdl_options_overrides=options.ytdl_options_overrides,
        sponsorblock=options.sponsorblock,
        audio_tags=options.audio_tags,
        title_regex=post.get('title_regex'),
        skip_subscriber_only=skip_subscriber_only,
        clip_start=options.clip_start,
        clip_end=options.clip_end,
    )
    return web.Response(text=serializer.encode(result))

@routes.get(config.URL_PREFIX + 'subscriptions')
async def subscriptions_list(request):
    env = await env_for_request(request)
    return web.Response(text=serializer.encode([s.to_public_dict() for s in env['submgr'].list_all()]))

@routes.post(config.URL_PREFIX + 'subscriptions/update')
async def subscriptions_update(request):
    post = await _read_json_request(request)
    env = await env_for_request(request)
    sub_id = post.get('id')
    if not sub_id:
        raise web.HTTPBadRequest(reason='missing subscription id')
    changes = {k: v for k, v in post.items() if k != 'id' and k in ('enabled', 'check_interval_minutes', 'name', 'folder', 'title_regex', 'skip_subscriber_only')}
    if not changes:
        raise web.HTTPBadRequest(reason='no valid fields to update')
    result = await env['submgr'].update_subscription(str(sub_id), changes)
    return web.Response(text=serializer.encode(result))

@routes.post(config.URL_PREFIX + 'subscriptions/delete')
async def subscriptions_delete(request):
    post = await _read_json_request(request)
    env = await env_for_request(request)
    ids = post.get('ids')
    if not ids or not isinstance(ids, list):
        raise web.HTTPBadRequest(reason='missing ids list')
    result = await env['submgr'].delete_subscriptions([str(i) for i in ids])
    return web.Response(text=serializer.encode(result))

@routes.post(config.URL_PREFIX + 'subscriptions/check')
async def subscriptions_check(request):
    post = await _read_json_request(request)
    env = await env_for_request(request)
    ids = post.get('ids')
    if ids is not None and not isinstance(ids, list):
        raise web.HTTPBadRequest(reason='ids must be a list')
    result = await env['submgr'].check_now([str(i) for i in ids] if ids else None)
    return web.Response(text=serializer.encode(result))

def _require_id(post: dict) -> str:
    id = post.get('id')
    if not isinstance(id, str) or not id:
        raise web.HTTPBadRequest(reason="'id' must be a non-empty string")
    return id

def _require_id_list(post: dict) -> list:
    ids = post.get('ids')
    if not isinstance(ids, list) or not ids or not all(isinstance(i, str) for i in ids):
        raise web.HTTPBadRequest(reason="'ids' must be a non-empty list of strings")
    return ids

@routes.post(config.URL_PREFIX + 'delete')
async def delete(request):
    post = await _read_json_request(request)
    env = await env_for_request(request)
    ids = _require_id_list(post)
    where = post.get('where')
    if where not in ['queue', 'done']:
        log.error("Bad request: incorrect 'where' value")
        raise web.HTTPBadRequest()
    delete_files = post.get('delete_files', False)
    if not isinstance(delete_files, bool):
        raise web.HTTPBadRequest(reason="'delete_files' must be a boolean")
    status = await (env['queue'].cancel(ids) if where == 'queue' else env['queue'].clear(ids, delete_files=delete_files))
    return web.Response(text=serializer.encode(status))

@routes.post(config.URL_PREFIX + 'start')
async def start(request):
    post = await _read_json_request(request)
    env = await env_for_request(request)
    ids = _require_id_list(post)
    status = await env['queue'].start_pending(ids)
    return web.Response(text=serializer.encode(status))

COOKIES_PATH = os.path.join(config.STATE_DIR, 'cookies.txt')

def warn_if_cookiefile_shadowed():
    configured = config.YTDL_OPTIONS.get('cookiefile')
    if isinstance(configured, str) and configured and configured != COOKIES_PATH:
        log.warning('Uploaded cookies at %s take precedence over the cookiefile configured in YTDL_OPTIONS (%s).', COOKIES_PATH, configured)

@routes.post(config.URL_PREFIX + 'upload-cookies')
async def upload_cookies(request):
    reader = await request.multipart()
    field = await reader.next()
    if field is None or field.name != 'cookies':
        return web.Response(status=400, text=serializer.encode({'status': 'error', 'msg': 'No cookies file provided'}))
    max_size = 1_000_000
    size = 0
    content = bytearray()
    while True:
        chunk = await field.read_chunk()
        if not chunk:
            break
        size += len(chunk)
        if size > max_size:
            return web.Response(status=400, text=serializer.encode({'status': 'error', 'msg': 'Cookie file too large (max 1MB)'}))
        content.extend(chunk)
    tmp_cookie_path = f"{COOKIES_PATH}.tmp"
    with open(tmp_cookie_path, 'wb') as f:
        f.write(content)
    try:
        os.chmod(tmp_cookie_path, 0o600)
    except OSError as exc:
        log.warning(f'Could not restrict permissions on cookies file: {exc}')
    os.replace(tmp_cookie_path, COOKIES_PATH)
    warn_if_cookiefile_shadowed()
    config.set_runtime_override('cookiefile', COOKIES_PATH)
    return web.Response(text=serializer.encode({'status': 'ok', 'msg': f'Cookies uploaded ({size} bytes)'}))

@routes.post(config.URL_PREFIX + 'delete-cookies')
async def delete_cookies(request):
    has_uploaded_cookies = os.path.exists(COOKIES_PATH)
    configured_cookiefile = config.YTDL_OPTIONS.get('cookiefile')
    has_manual_cookiefile = isinstance(configured_cookiefile, str) and configured_cookiefile and configured_cookiefile != COOKIES_PATH
    if not has_uploaded_cookies:
        if has_manual_cookiefile:
            return web.Response(status=400, text=serializer.encode({'status': 'error', 'msg': 'Cookies are configured manually via YTDL_OPTIONS.'}))
        return web.Response(status=400, text=serializer.encode({'status': 'error', 'msg': 'No uploaded cookies to delete'}))
    os.remove(COOKIES_PATH)
    config.remove_runtime_override('cookiefile')
    success, msg = config.load_ytdl_options()
    if not success:
        return web.Response(status=500, text=serializer.encode({'status': 'error', 'msg': f'Cookies file deleted, but failed to reload YTDL_OPTIONS: {msg}'}))
    return web.Response(text=serializer.encode({'status': 'ok'}))

@routes.get(config.URL_PREFIX + 'cookie-status')
async def cookie_status(request):
    configured_cookiefile = config.YTDL_OPTIONS.get('cookiefile')
    has_configured_cookies = isinstance(configured_cookiefile, str) and os.path.exists(configured_cookiefile)
    has_uploaded_cookies = os.path.exists(COOKIES_PATH)
    exists = has_uploaded_cookies or has_configured_cookies
    return web.Response(text=serializer.encode({'status': 'ok', 'has_cookies': exists}))

@routes.get(config.URL_PREFIX + 'history')
async def history(request):
    env = await env_for_request(request)
    history = { 'done': [], 'queue': [], 'pending': []}
    for _, v in env['queue'].queue.items():
        history['queue'].append(v.info)
    for _, v in env['queue'].done.items():
        history['done'].append(v.info)
    for _, v in env['queue'].pending.items():
        history['pending'].append(v.info)
    return web.Response(text=serializer.encode(history))

async def _emit_connect_finish(sid, user_config):
    loop = asyncio.get_running_loop()
    if user_config.CUSTOM_DIRS:
        dirs = await loop.run_in_executor(None, get_custom_dirs, user_config)
        await sio.emit('custom_dirs', serializer.encode(dirs), to=sid)
    if user_config.YTDL_OPTIONS_FILE:
        options_update = await loop.run_in_executor(None, get_options_update_time)
        await sio.emit('ytdl_options_changed', serializer.encode(options_update), to=sid)

@sio.event
async def connect(sid, environ):
    user = None
    if config.ENABLE_AUTH:
        session_user = session_manager.user_from_environ(environ) if session_manager is not None else None
        if session_user is not None and user_store is not None:
            persisted_user = user_store.get_session_user(session_user['username'])
            if persisted_user is not None and persisted_user.get('session_version') == session_user.get('session_version'):
                user = public_user_from_session_user(persisted_user)
        if user is None:
            log.warning("Rejected unauthenticated socket connection: %s", sid)
            return False
        env = await user_envs.get(user['username'])
        await sio.enter_room(sid, user_envs.room(user['username']))
        log.info("Client connected: %s (%s)", sid, user['username'])
    else:
        env = {'queue': dqueue, 'submgr': submgr, 'config': config}
        log.info("Client connected: %s", sid)

    await sio.emit('all', serializer.encode(env['queue'].get()), to=sid)
    await sio.emit('subscriptions_all', serializer.encode([s.to_public_dict() for s in env['submgr'].list_all()]), to=sid)
    await sio.emit('formats', serializer.encode(format_catalog.public_catalog()), to=sid)
    
    cfg = env['config'].frontend_safe()
    cfg['AUTH_ENABLED'] = config.ENABLE_AUTH
    if user:
        cfg['AUTH_USER'] = user
    await sio.emit('configuration', serializer.encode(cfg), to=sid)
    
    bg_tasks.create_task(_emit_connect_finish(sid, env['config']), name=f"connect_finish_{sid}")

_CUSTOM_DIRS_CACHE_TTL_SECONDS = 5
_custom_dirs_cache = None

def _list_subdirs(base: str, exclude_re) -> list[str]:
    root = pathlib.Path(base)
    dirs = ['']
    for path in root.glob('**/'):
        rel = path.relative_to(root).as_posix()
        if rel == '.' or (exclude_re is not None and exclude_re.search(rel)):
            continue
        dirs.append(rel)
    return dirs

def get_custom_dirs(target_config):
    global _custom_dirs_cache
    now = time.monotonic()
    key = (target_config.DOWNLOAD_DIR, target_config.AUDIO_DOWNLOAD_DIR, target_config.CUSTOM_DIRS_EXCLUDE_REGEX)
    if _custom_dirs_cache is not None and _custom_dirs_cache[0] == key and now - _custom_dirs_cache[1] < _CUSTOM_DIRS_CACHE_TTL_SECONDS:
        return _custom_dirs_cache[2]
    download_dir = _list_subdirs(target_config.DOWNLOAD_DIR, target_config.CUSTOM_DIRS_EXCLUDE_RE)
    audio_download_dir = download_dir
    if target_config.DOWNLOAD_DIR != target_config.AUDIO_DOWNLOAD_DIR:
        audio_download_dir = _list_subdirs(target_config.AUDIO_DOWNLOAD_DIR, target_config.CUSTOM_DIRS_EXCLUDE_RE)
    result = {"download_dir": download_dir, "audio_download_dir": audio_download_dir}
    _custom_dirs_cache = (key, now, result)
    return result

def safe_file_response(base_directory, relative_path):
    base_path = pathlib.Path(base_directory).resolve()
    decoded_path = relative_path
    candidate = (base_path / decoded_path).resolve()
    try:
        candidate.relative_to(base_path)
    except ValueError as exc:
        raise web.HTTPForbidden() from exc
    if not candidate.is_file():
        raise web.HTTPNotFound()
    return web.FileResponse(candidate)

if config.ENABLE_AUTH:
    @routes.get(config.URL_PREFIX + 'download/{path:.*}')
    async def download_file(request):
        user = require_authenticated_user(request)
        env = await user_envs.get(user['username'])
        return safe_file_response(env['config'].DOWNLOAD_DIR, request.match_info.get('path', ''))

    @routes.get(config.URL_PREFIX + 'audio_download/{path:.*}')
    async def audio_download_file(request):
        user = require_authenticated_user(request)
        env = await user_envs.get(user['username'])
        return safe_file_response(env['config'].AUDIO_DOWNLOAD_DIR, request.match_info.get('path', ''))
else:
    routes.static(config.URL_PREFIX + 'download/', config.DOWNLOAD_DIR, show_index=config.DOWNLOAD_DIRS_INDEXABLE)
    routes.static(config.URL_PREFIX + 'audio_download/', config.AUDIO_DOWNLOAD_DIR, show_index=config.DOWNLOAD_DIRS_INDEXABLE)

routes.static(config.URL_PREFIX, os.path.join(config.BASE_DIR, 'ui/dist/metube/browser'))
try:
    app.add_routes(routes)
except ValueError as e:
    if 'ui/dist/metube/browser' in str(e):
        raise RuntimeError('Could not find the frontend UI static assets.') from e
    raise e

async def add_cors(request):
    return web.Response(text=serializer.encode({"status": "ok"}))

for endpoint in ['add', 'browse', 'cancel-add', 'retry', 'subscribe', 'subscriptions', 'subscriptions/update', 'subscriptions/delete', 'subscriptions/check', 'upload-cookies', 'delete-cookies']:
    app.router.add_route('OPTIONS', config.URL_PREFIX + endpoint, add_cors)

async def on_prepare(request, response):
    origin = request.headers.get('Origin')
    if not origin or not _cors_origins:
        return
    wildcard = '*' in _cors_origins
    trusted = not wildcard and origin in _cors_origins
    if not (wildcard or trusted):
        return
    response.headers['Access-Control-Allow-Origin'] = origin
    response.headers['Access-Control-Allow-Headers'] = 'Content-Type, Authorization' if trusted else 'Content-Type'
    if trusted:
        response.headers['Access-Control-Allow-Credentials'] = 'true'
    vary = response.headers.get('Vary')
    if not vary:
        response.headers['Vary'] = 'Origin'
    elif 'origin' not in (v.strip().lower() for v in vary.split(',')):
        response.headers['Vary'] = f'{vary}, Origin'

app.on_response_prepare.append(on_prepare)

def isAccessLogEnabled():
    return access_logger if config.ENABLE_ACCESSLOG else None

if __name__ == '__main__':
    logging.getLogger().setLevel(parseLogLevel(config.LOGLEVEL) or logging.INFO)
    log.info(f"Listening on {config.HOST or '*'}:{config.PORT}")

    if os.path.exists(COOKIES_PATH):
        warn_if_cookiefile_shadowed()
        config.set_runtime_override('cookiefile', COOKIES_PATH)
        log.info(f'Cookie file detected at {COOKIES_PATH}')

    if config.HTTPS:
        ssl_context = ssl.create_default_context(ssl.Purpose.CLIENT_AUTH)
        ssl_context.load_cert_chain(certfile=config.CERTFILE, keyfile=config.KEYFILE)
        web.run_app(app, host=config.HOST, port=config.PORT, ssl_context=ssl_context, access_log=isAccessLogEnabled())
    else:
        web.run_app(app, host=config.HOST, port=config.PORT, access_log=isAccessLogEnabled())
    if _RESTART_FOR_UPDATE:
        sys.exit(42)
