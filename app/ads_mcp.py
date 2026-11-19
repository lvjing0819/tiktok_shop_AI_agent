"""Official TikTok MCP transport, encrypted OAuth storage and fail-closed reads."""
import asyncio
import hashlib
import json
import os
import re
import secrets
import time
from contextlib import asynccontextmanager
from urllib.parse import parse_qs, urlparse
from uuid import uuid4

import httpx
from cryptography.fernet import Fernet, InvalidToken
from mcp import ClientSession
from mcp.client.auth import OAuthClientProvider
from mcp.client.streamable_http import streamable_http_client
from mcp.shared.auth import OAuthClientMetadata, OAuthToken, OAuthClientInformationFull
from sqlalchemy import select, delete
from app import store


class AdsError(Exception):
    pass


def server_url():
    # Flat metadata is filtered locally; generic execute/search tools are NOT exposed.
    return 'https://business-api.tiktok.com/open_mcp/tt-ads-mcp-flat'


def cipher():
    key = os.getenv('ADS_TOKEN_ENCRYPTION_KEY', '')
    if not key:
        raise AdsError('请先运行 python -m scripts.setup_ads，生成本机令牌加密密钥')
    try:
        return Fernet(key.encode())
    except (ValueError, TypeError):
        raise AdsError('广告令牌加密密钥格式无效')


def seal(value):
    return cipher().encrypt(json.dumps(value).encode()).decode()


def unseal(value):
    try:
        return json.loads(cipher().decrypt(value.encode()))
    except (InvalidToken, ValueError):
        raise AdsError('无法解密广告授权，请使用原加密密钥或断开后重新授权')


def connection(shop):
    return store.get('ads:'+shop, 'ads_connection', shop)


def public_status(shop):
    item = connection(shop)
    if not item:
        return {'status': 'disconnected', 'server': server_url(), 'read_only': True}
    return {k: item.get(k) for k in ('status', 'server', 'connected_at', 'error')} | {'read_only': True}


def locked_connection(conn, shop, generation):
    conn.execute(select(store.records.c.id).where(store.records.c.kind == 'ads_connection', store.records.c.id == 'ads:'+shop, store.records.c.shop == shop).with_for_update()).first()
    current = store.get('ads:'+shop, 'ads_connection', shop, conn)
    if not current or current['generation'] != generation:
        raise AdsError('连接已变化，请重新操作')
    return current


class Storage:
    def __init__(self, shop, generation):
        self.shop, self.generation = shop, generation

    def read(self):
        item = connection(self.shop)
        if not item or item['generation'] != self.generation:
            raise AdsError('连接已断开')
        return unseal(item['sealed']) if item.get('sealed') else {}

    def write(self, key, value):
        with store.db(write=True) as conn:
            item = locked_connection(conn, self.shop, self.generation)
            data = unseal(item['sealed']) if item.get('sealed') else {}
            data[key] = value
            item['sealed'] = seal(data)
            store.put(conn, 'ads_connection', item, self.shop)

    async def get_tokens(self):
        value = self.read().get('tokens')
        return OAuthToken.model_validate(value) if value else None

    async def set_tokens(self, tokens):
        self.write('tokens', tokens.model_dump(mode='json'))

    async def get_client_info(self):
        value = self.read().get('client')
        return OAuthClientInformationFull.model_validate(value) if value else None

    async def set_client_info(self, client_info):
        self.write('client', client_info.model_dump(mode='json'))


def redirect_uri():
    uri = os.getenv('ADS_OAUTH_REDIRECT_URI', 'http://127.0.0.1:8000/api/ads/oauth/callback')
    parsed = urlparse(uri)
    if parsed.scheme != 'https' and not (parsed.scheme == 'http' and parsed.hostname in ('127.0.0.1', 'localhost')):
        raise AdsError('回调地址须为 HTTPS 或本机回环地址')
    if parsed.query or parsed.fragment or parsed.username:
        raise AdsError('回调地址不可包含查询参数、片段或用户信息')
    return uri


@asynccontextmanager
async def session(shop, generation=None, attempt_id=None):
    item = connection(shop)
    if not item or (not attempt_id and item['status'] != 'connected'):
        raise AdsError('请先连接 TikTok 广告账户')
    generation = generation or item['generation']

    async def redirect_handler(url):
        if not attempt_id:
            raise AdsError('广告授权已失效，请重新连接')
        parsed = urlparse(url)
        if parsed.scheme != 'https' or parsed.hostname not in ('business-api.tiktok.com', 'ads.tiktok.com', 'business.tiktok.com'):
            raise AdsError('授权地址不是受支持的 TikTok 官方地址')
        state = parse_qs(parsed.query).get('state', [None])[0]
        if not state:
            raise AdsError('授权服务未提供 state 校验参数')
        with store.db(write=True) as conn:
            current = locked_connection(conn, shop, generation)
            attempt = store.get(attempt_id, 'ads_attempt', shop, conn)
            if not attempt or attempt['expires_at'] < time.time():
                raise AdsError('授权会话已过期')
            attempt['authorization_url'] = url
            attempt['state_hash'] = hashlib.sha256(state.encode()).hexdigest()
            store.put(conn, 'ads_attempt', attempt, shop)
            # This index contains no authorization code or tokens.
            store.put(conn, 'ads_state', {'id': attempt['state_hash'], 'shop': shop, 'attempt_id': attempt_id, 'expires_at': attempt['expires_at']}, '_oauth')

    async def callback_handler():
        for _ in range(300):
            current = connection(shop)
            if not current or current['generation'] != generation:
                raise AdsError('授权已取消')
            attempt = store.get(attempt_id, 'ads_attempt', shop)
            if not attempt or attempt['expires_at'] < time.time():
                raise AdsError('授权会话已过期，请重试')
            if attempt.get('callback'):
                data = unseal(attempt['callback'])
                return data['code'], data['state']
            await asyncio.sleep(1)
        raise AdsError('等待授权超时，请重新连接')

    auth = OAuthClientProvider(server_url=server_url(),
        client_metadata=OAuthClientMetadata(redirect_uris=[redirect_uri()], client_name='Shop AI Ads Read-only Workbench',
            token_endpoint_auth_method='none', grant_types=['authorization_code', 'refresh_token'], response_types=['code'], scope='mcp:tt4b'),
        storage=Storage(shop, generation), redirect_handler=redirect_handler, callback_handler=callback_handler)
    try:
        async with asyncio.timeout(360 if attempt_id else 120):
            async with httpx.AsyncClient(auth=auth, timeout=30, follow_redirects=False) as client:
                async with streamable_http_client(server_url(), http_client=client) as (read, write, _):
                    async with ClientSession(read, write) as mcp:
                        await mcp.initialize()
                        yield mcp
    except AdsError:
        raise
    except Exception:
        # Do not reflect exception groups: HTTP errors may contain sensitive URLs.
        raise AdsError('TikTok MCP 连接或授权失败，请检查网络、授权状态及回调地址') from None


def begin_connection(shop):
    cipher()
    redirect_uri()
    generation = str(uuid4())
    attempt_id = str(uuid4())
    with store.db(write=True) as conn:
        old = store.get('ads:'+shop, 'ads_connection', shop, conn)
        if old and old['status'] == 'connecting':
            pending = store.get(old.get('attempt_id', ''), 'ads_attempt', shop, conn)
            if pending and pending['expires_at'] > time.time():
                return pending, False
        item = {'id': 'ads:'+shop, 'generation': generation, 'server': server_url(), 'status': 'connecting', 'sealed': None, 'attempt_id': attempt_id}
        store.put(conn, 'ads_connection', item, shop)
        attempt = {'id': attempt_id, 'generation': generation, 'expires_at': time.time()+360, 'authorization_url': None, 'callback': None}
        store.put(conn, 'ads_attempt', attempt, shop)
    return attempt, True


async def authorize(shop, attempt_id, generation):
    try:
        async with session(shop, generation, attempt_id):
            pass
        with store.db(write=True) as conn:
            item = locked_connection(conn, shop, generation)
            item.update(status='connected', connected_at=time.time(), error=None)
            store.put(conn, 'ads_connection', item, shop)
    except Exception:
        with store.db(write=True) as conn:
            item = store.get('ads:'+shop, 'ads_connection', shop, conn)
            if item and item['generation'] == generation:
                item.update(status='error', error='连接未完成；请检查网络与回调地址后重新授权')
                store.put(conn, 'ads_connection', item, shop)
    finally:
        with store.db(write=True) as conn:
            attempt = store.get(attempt_id, 'ads_attempt', shop, conn)
            if attempt:
                attempt['callback'] = None
                attempt['authorization_url'] = None
                store.put(conn, 'ads_attempt', attempt, shop)
                if attempt.get('state_hash'):
                    conn.execute(delete(store.records).where(store.records.c.kind == 'ads_state', store.records.c.id == attempt['state_hash']))


def accept_callback(state, code=None, denied=False):
    state_hash = hashlib.sha256(state.encode()).hexdigest()
    with store.db(write=True) as conn:
        conn.execute(select(store.records.c.id).where(store.records.c.kind == 'ads_state', store.records.c.id == state_hash).with_for_update()).first()
        lookup = store.get(state_hash, 'ads_state', '_oauth', conn)
        if not lookup or lookup['expires_at'] < time.time():
            raise AdsError('授权 state 无效、已使用或已过期')
        shop = lookup['shop']
        attempt = store.get(lookup['attempt_id'], 'ads_attempt', shop, conn)
        if not attempt or attempt.get('callback'):
            raise AdsError('授权回调已处理')
        current = locked_connection(conn, shop, attempt['generation'])
        if denied:
            attempt['expires_at'] = 0
            current.update(status='error', error='授权未完成，可以重新连接')
            store.put(conn, 'ads_connection', current, shop)
        else:
            attempt['callback'] = seal({'code': code, 'state': state})
        store.put(conn, 'ads_attempt', attempt, shop)
        conn.execute(delete(store.records).where(store.records.c.kind == 'ads_state', store.records.c.id == state_hash))


def disconnect(shop):
    # Local forget invalidates in-flight writers. Remote revocation is separate.
    with store.db(write=True) as conn:
        store.put(conn, 'ads_connection', {'id': 'ads:'+shop, 'generation': str(uuid4()), 'status': 'disconnected', 'server': server_url(), 'sealed': None}, shop)
        for kind in ('ads_attempt',):
            conn.execute(delete(store.records).where(store.records.c.kind == kind, store.records.c.shop == shop))
    return public_status(shop)


WRITE_WORDS = re.compile(r'(^|_)(create|update|delete|remove|set|upload|enable|disable|publish|execute|call|revoke|grant|transfer|payment|recharge|appeal)(_|$)', re.I)
READ_WORDS = re.compile(r'(^|_)(get|list|query|search|report|fetch|read)(_|$)', re.I)


def permitted(tool):
    name = tool['name']
    if not re.fullmatch(r'[a-zA-Z0-9_-]{1,64}', name):
        return False
    # Never trust a model to classify tool safety; generic dispatchers are excluded.
    if WRITE_WORDS.search(name) or not READ_WORDS.search(name):
        return False
    if any(x in name.lower() for x in ('tool', 'schema', 'discovery', 'endpoint', 'http', 'api_request')):
        return False
    configured = {s.strip() for s in os.getenv('ADS_READ_TOOL_ALLOWLIST', '').split(',') if s.strip()}
    annotations = tool.get('annotations') or {}
    return annotations.get('readOnlyHint') is True or name in configured


async def tools(mcp):
    result = []
    cursor = None
    for _ in range(20):
        page = await mcp.list_tools(cursor=cursor)
        result.extend(t.model_dump(mode='json', by_alias=True) for t in page.tools)
        cursor = page.nextCursor
        if not cursor:
            return result
    raise AdsError('工具列表分页过多，停止加载')


def bind_account(arguments, schema, advertiser_id, start_date=None, end_date=None):
    def verify(value):
        if isinstance(value, dict):
            for key, item in value.items():
                if key == 'advertiser_id' and str(item) != advertiser_id:
                    raise AdsError('工具参数引用了其他广告账户')
                if key == 'advertiser_ids' and item != [advertiser_id]:
                    raise AdsError('仅允许查询选定广告账户')
                verify(item)
        elif isinstance(value, list):
            for item in value:
                verify(item)
    arguments = dict(arguments)
    properties = schema.get('properties', {})
    if 'advertiser_id' in properties:
        arguments['advertiser_id'] = advertiser_id
    if 'advertiser_ids' in properties:
        arguments['advertiser_ids'] = [advertiser_id]
    if start_date and 'start_date' in properties:
        arguments['start_date'] = start_date
    if end_date and 'end_date' in properties:
        arguments['end_date'] = end_date
    verify(arguments)
    return arguments
