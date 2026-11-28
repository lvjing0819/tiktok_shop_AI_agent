"""Task-bound, one-use API capabilities over the sandbox's private stdio channel.

No URL proxy/listening port. Only reviewed adapters construct upstream requests.
The container has no network or platform credentials. OAuth and transport remain
in this trusted credential boundary, not in the container's execution context.
"""
import asyncio
from contextlib import contextmanager
from contextvars import ContextVar
from functools import wraps
import hashlib
import hmac
import inspect
import json
import os
import secrets
import time
from uuid import uuid4
from jsonschema import Draft202012Validator
from app import sandbox, execution_context

active = ContextVar('agent_api_gateway', default=None)
MAX_RESPONSE = 2_000_000
MAX_TOTAL = 16_000_000
MAX_CALLS = 32
TOOLS = {'shop.discover': 'shop', 'shop.query': 'shop', 'ads.accounts': 'ads',
         'ads.analyze': 'ads', 'social.analyze': 'social'}
SECRET_KEYS = {'access_token', 'refresh_token', 'app_secret', 'client_secret',
               'authorization', 'x-tts-access-token', 'api_key', 'password'}


def encoded(value):
    return json.dumps(value, ensure_ascii=False, separators=(',', ':'), sort_keys=True,
                      allow_nan=False).encode()


def deny(code='GATEWAY_DENIED', message='请求不属于本次任务的授权范围'):
    return sandbox.SandboxError(code, message)


def check_result(value, sensitive=()):
    raw = encoded(value)
    if len(raw) > MAX_RESPONSE:
        raise deny('GATEWAY_RESPONSE_LIMIT', 'API 返回超过大小限制，请缩小查询范围')
    def walk(node):
        if isinstance(node, dict):
            for key, item in node.items():
                # Schema property definitions are not actual credentials.
                if str(key).lower() in SECRET_KEYS and isinstance(item, (str, int, float)):
                    raise deny('GATEWAY_SECRET_OUTPUT', 'API 返回含凭据字段，未交付沙箱')
                walk(item)
        elif isinstance(node, list):
            for item in node: walk(item)
    walk(value)
    for secret in sensitive:
        if isinstance(secret, str) and len(secret) >= 8 and secret.encode() in raw:
            raise deny('GATEWAY_SECRET_OUTPUT', 'API 返回含凭据，未交付沙箱')
    return raw


class Scope:
    def __init__(self, ctx, tool, arguments):
        if tool not in TOOLS: raise deny()
        self.ctx, self.tool = dict(ctx), tool
        self.arguments = json.loads(encoded(arguments))
        self.calls, self.bytes, self.open = 0, 0, True
        self.account = str(arguments.get('advertiser_id', ''))
        self.service = TOOLS[tool]

    async def current(self):
        if not self.open or time.monotonic() >= self.ctx['deadline_monotonic']:
            raise deny('GATEWAY_EXPIRED', 'API 任务授权已结束或过期')
        if not callable(self.ctx.get('assert_current')): raise deny()
        await sandbox.current(self.ctx)

    async def invoke(self, operation, arguments, perform, *, sensitive=()):
        await self.current()
        if self.calls >= MAX_CALLS or self.bytes >= MAX_TOTAL:
            raise deny('GATEWAY_BUDGET', 'API 请求次数或数据额度已用尽')
        if len(encoded(arguments)) > 48_000: raise deny('INPUT_LIMIT', 'API 请求参数过大')
        check_result(arguments,sensitive)
        self.calls += 1
        grant = Grant(self, operation, arguments, perform, sensitive)
        return await sandbox.run_job(self.ctx, 'api_gateway_v1', grant.input(),
                                    policy=sandbox.Policy(timeout=90), gateway=grant)


class Grant:
    """One exact request, one private channel, one current execution attempt."""
    def __init__(self, scope, operation, arguments, perform, sensitive=()):
        self.scope, self.operation, self.perform = scope, operation, perform
        self.arguments = json.loads(encoded(arguments))
        self.token, self.used, self.error, self.sandbox_id = secrets.token_urlsafe(32), False, None, None
        self.expires = min(time.monotonic()+60, scope.ctx['deadline_monotonic'])
        self.response = None
        self.sensitive = tuple(sensitive) + tuple(v for k,v in os.environ.items()
            if any(s in k.upper() for s in ('SECRET','TOKEN','API_KEY','PASSWORD')))

    def input(self):
        return {'capability': self.token, 'service': self.scope.service,
                'operation': self.operation, 'arguments': self.arguments}

    def audit(self, status, **fields):
        if self.sandbox_id:
            sandbox.audit(self.scope.ctx, self.sandbox_id, gateway={
                'transport': 'private_stdio', 'service': self.scope.service,
                'operation': self.operation, 'status': status,
                'request_checksum': hashlib.sha256(encoded(self.arguments)).hexdigest(), **fields})

    async def dispatch(self, frame):
        await self.scope.current()
        expected = {'protocol': 1, 'kind': 'gateway_request', **self.input()}
        if (self.used or time.monotonic() >= self.expires or not isinstance(frame, dict)
                or set(frame) != set(expected)
                or not isinstance(frame.get('capability'), str)
                or not hmac.compare_digest(frame['capability'], self.token)
                or encoded({k:v for k,v in frame.items() if k!='capability'}) != encoded({k:v for k,v in expected.items() if k!='capability'})):
            self.audit('denied', reason='capability_or_request_mismatch')
            raise deny()
        self.used = True  # Consume before await; upstream failures are not replayable.
        self.audit('allowed')
        token = active.set(None)  # Trusted adapter cannot recursively re-enter this gateway.
        try:
            async with asyncio.timeout(45): value = await self.perform()
            await self.scope.current()
            raw = check_result(value, self.sensitive)
            if self.scope.bytes + len(raw) > MAX_TOTAL:
                raise deny('GATEWAY_BUDGET', 'API 返回数据额度已用尽')
            self.scope.bytes += len(raw)
            self.response = value
            self.audit('completed', response_bytes=len(raw), response_checksum=hashlib.sha256(raw).hexdigest())
            return {'protocol':1, 'kind':'gateway_response', 'data':value}
        except Exception as exc:
            self.error = exc
            self.audit('failed', reason=exc.observation['code'] if isinstance(exc,sandbox.SandboxError) else 'upstream_failed')
            raise
        finally:
            if self.response is None and self.error is None:self.audit('aborted',reason='cancelled_or_timeout')
            active.reset(token)

    def validate_delivery(self, value):
        if not self.used or self.response is None or encoded(value) != encoded(self.response):
            raise deny('INVALID_OUTPUT', '沙箱 API 交付与本次网关结果不一致')


@contextmanager
def execution(ctx, tool, arguments):
    if active.get() is not None: raise deny()
    scope = Scope(ctx, tool, arguments)
    token = active.set(scope)
    try: yield scope
    finally:
        scope.open = False
        active.reset(token)


def isolated(tool):
    """Also protect natural-language/direct specialist entry points, not polling."""
    def decorate(fn):
        signature = inspect.signature(fn)
        @wraps(fn)
        async def wrapped(*args, **kwargs):
            values = signature.bind(*args, **kwargs).arguments
            workspace = values.get('shop', values.get('workspace'))
            scope = active.get()
            if scope is not None:
                if scope.service != TOOLS[tool] or scope.ctx['workspace'] != workspace: raise deny()
                return await fn(*args, **kwargs)
            with direct(workspace,tool,{k:v for k,v in values.items() if k not in ('shop','workspace','conversation_context','context','run')}):
                return await fn(*args, **kwargs)
        return wrapped
    return decorate


@contextmanager
def direct(workspace, tool, arguments):
    scope=active.get()
    if scope is not None:
        if scope.service!=TOOLS[tool] or scope.ctx['workspace']!=workspace:raise deny()
        yield scope
        return
    alive=[True];deadline=time.monotonic()+240
    def fence(conn):
        if not alive[0] or time.monotonic()>=deadline:raise deny('GATEWAY_EXPIRED')
    ctx={'workspace':workspace,'run_id':'api-'+str(uuid4()),'step_id':tool,
         'attempt_id':str(uuid4()),'assert_current':fence,'deadline_monotonic':deadline}
    with execution(ctx,tool,arguments) as created:
        try:yield created
        finally:alive[0]=False


class MCPSession:
    def __init__(self, raw, workspace, service):
        self.raw, self.workspace, self.service, self.catalog = raw, workspace, service, {}
        self.authorization=self.auth_identity()

    def auth_identity(self):
        from app import ads_mcp, social_mcp
        profile=ads_mcp if self.service=='ads' else social_mcp
        if self.service=='social' and social_mcp.auth_mode()=='api_token':
            if social_mcp.public_status(self.workspace)['status']!='connected':raise deny('GATEWAY_AUTH','Social 连接尚未验证或已经断开')
            return ('api_token',hashlib.sha256(os.environ.get('SOCIAL_API_ACCESS_TOKEN','').encode()).hexdigest())
        item=profile.client.connection(self.workspace)
        if not item or item.get('status')!='connected' or item.get('server')!=profile.server_url():
            raise deny('GATEWAY_AUTH','平台连接已断开或尚未验证')
        return ('oauth',item['generation'])

    def check_authorization(self):
        if self.authorization!=self.auth_identity():raise deny('GATEWAY_AUTH','查询期间平台授权已变化，结果未交付')

    def scope(self):
        scope = active.get()
        if not scope or scope.service != self.service or scope.ctx['workspace'] != self.workspace: raise deny()
        self.check_authorization()
        return scope

    def permitted(self, tool):
        from app import ads_mcp, social_mcp
        return (ads_mcp if self.service=='ads' else social_mcp).permitted(tool)

    def credentials(self):
        from app import ads_mcp, social_mcp
        profile=ads_mcp if self.service=='ads' else social_mcp
        item=profile.client.connection(self.workspace)
        if not item or not item.get('sealed'):return ()
        data=profile.client.unseal(item['sealed'])
        return tuple(v for section in ('tokens','client') for k,v in data.get(section,{}).items()
                     if k in SECRET_KEYS and isinstance(v,str))

    async def list_tools(self, *, cursor=None):
        from mcp.types import ListToolsResult
        scope = self.scope()
        async def perform():
            self.check_authorization()
            execution_context.remote_call(self.service+':tools')
            page = await self.raw.list_tools(cursor=cursor)
            self.check_authorization()
            result = page.model_dump(mode='json',by_alias=True)
            result['tools'] = [t for t in result.get('tools',[]) if self.permitted(t)]
            check_result(result,self.credentials())
            return result
        result = await scope.invoke('tools.list', {'cursor':cursor}, perform)
        for tool in result['tools']: self.catalog[tool['name']] = tool
        return ListToolsResult.model_validate(result)

    async def call_tool(self, name, arguments=None, **kwargs):
        from mcp.types import CallToolResult
        from app import ads_mcp, ads_accounts
        scope = self.scope()
        tool = self.catalog.get(name)
        if kwargs or not tool or not self.permitted(tool) or not isinstance(arguments,dict): raise deny()
        try: Draft202012Validator(tool['inputSchema']).validate(arguments)
        except Exception: raise deny() from None
        if self.service=='ads':
            if name == 'auth_advertiser_get':
                if arguments: raise deny()
            else:
                if not scope.account: raise deny('GATEWAY_ACCOUNT', '尚未唯一确认广告账户')
                bound = ads_mcp.bind_account(arguments, tool['inputSchema'], scope.account)
                if bound != arguments: raise deny('GATEWAY_ACCOUNT', '广告账户参数不属于本次授权')
                def has_account(node):
                    if isinstance(node,dict):return any(k in ('advertiser_id','advertiser_ids') or has_account(v) for k,v in node.items())
                    if isinstance(node,list):return any(has_account(v) for v in node)
                    return False
                if not has_account(arguments):raise deny('GATEWAY_ACCOUNT','该工具无法显式绑定选定广告账户，未执行')
                if scope.tool=='ads.accounts' and name!='advertiser_info_get': raise deny()
                if scope.tool=='ads.analyze' and name!='advertiser_info_get':
                    from app.ad_metric_comparisons import windows
                    periods = windows(scope.arguments['start_date'],scope.arguments['end_date'])
                    props=tool['inputSchema'].get('properties',{})
                    if 'start_date' in props or 'end_date' in props:
                        if not any(arguments.get('start_date')==p['start_date'] and arguments.get('end_date')==p['end_date'] for p in periods.values()): raise deny('GATEWAY_DATES', '广告查询日期不属于批准区间')
        async def perform():
            self.check_authorization()
            execution_context.remote_call(self.service+':'+name)
            value = (await self.raw.call_tool(name, arguments)).model_dump(mode='json',by_alias=True)
            self.check_authorization()
            check_result(value,self.credentials())
            if self.service=='ads' and name=='auth_advertiser_get':
                from app.mcp_results import payload, success
                rows=(payload(value).get('data') or {}).get('list',[]) if success(value) else []
                accounts=[{'advertiser_id':str(r['advertiser_id']), 'name':r.get('advertiser_name') or r.get('name') or str(r['advertiser_id'])} for r in rows if isinstance(r,dict) and str(r.get('advertiser_id','')).isdigit()]
                selector=scope.arguments.get('selector',scope.arguments.get('account'))
                if selector is not None:
                    matched=ads_accounts.matches(accounts,selector)
                    scope.account=matched[0]['advertiser_id'] if len(matched)==1 else ''
            return value
        return CallToolResult.model_validate(await scope.invoke('tools.call:'+name, arguments, perform))


def wrap_mcp(raw, workspace, service):
    return MCPSession(raw,workspace,service) if active.get() is not None else raw


async def shop_request(url, *, params, headers=None, method='GET', content=None):
    """Return None only outside Agent execution; never a Docker failure fallback."""
    scope = active.get()
    if scope is None: return None
    from app import shop_api as api, shop_read
    # Token refresh is the credential service's work, never exposed to the worker.
    if url in (api.TOKEN_ROOT+'/get', api.TOKEN_ROOT+'/refresh'):
        return await api._request_json(url,params=params,headers=headers,method=method,content=content)
    if scope.service!='shop': raise deny()
    if url==api.API_ROOT+'/authorization/202309/shops' and method=='GET':
        operation='shops'
    elif scope.tool=='shop.query' and method=='POST':
        allowed={scope.arguments.get('operation')}
        # Inventory's existing implementation first resolves SKU IDs from catalog.
        if 'inventory' in allowed:allowed.add('products')
        operation=next((op for op in allowed if op in shop_read.PATHS and url==api.API_ROOT+shop_read.PATHS[op]),None)
        if operation is None:raise deny()
    else: raise deny()
    cfg=api.config(); item=api.connection(scope.ctx['workspace'])
    if not item or item.get('status') not in ('connected','authorized') or not item.get('sealed'): raise deny()
    sealed=api.unseal(item['sealed'],cfg)
    token=sealed['tokens'].get('access_token')
    if not headers or not hmac.compare_digest(headers.get('x-tts-access-token',''),token or ''): raise deny()
    if operation!='shops':
        row=next((s for s in sealed.get('shops',[]) if s['id']==scope.arguments.get('shop_id')),None)
        if not row or params.get('shop_cipher')!=row['cipher']: raise deny('GATEWAY_ACCOUNT', '店铺不属于本次授权')
    public={'operation':operation,'shop_id':scope.arguments.get('shop_id'),
            'query':{k:v for k,v in params.items() if k not in ('sign','timestamp','app_key','shop_cipher')},
            'body':json.loads(content) if content else None}
    generation=item['generation']
    async def perform():
        with api.store.db() as conn: api.locked(conn,scope.ctx['workspace'],generation,cfg)
        result=await api._request_json(url,params=params,headers=headers,method=method,content=content)
        original.append(result)
        with api.store.db() as conn: api.locked(conn,scope.ctx['workspace'],generation,cfg)
        # Buyer identities and shop ciphers do not enter the worker.
        if operation=='shops':
            result={**result,'data':{**result['data'],'shops':[
                {k:v for k,v in r.items() if k!='cipher'} for r in result['data'].get('shops',[])]}}
        elif operation=='orders' and isinstance(result['data'].get('orders'),list):
            result={**result,'data':{**result['data'],'orders':[shop_read.clean_order(r) for r in result['data'].get('orders',[])]}}
        return result
    original=[]
    await scope.invoke('shop.'+operation,public,perform,sensitive=(token,cfg['app_secret']))
    # Credential-bearing discovery data stays in the trusted service, never stdout.
    return original[0]


async def preflight(workspace,service):
    scope=active.get()
    if scope is None:return
    if scope.ctx['workspace']!=workspace or scope.service!=service:raise deny()
    await scope.current()
    state=await sandbox.status()
    if not state.get('ready'):
        raise deny('RUNTIME_UNAVAILABLE','API 沙箱未就绪，请先安装启动 Docker 并构建 v3 镜像；不会直接执行查询')
