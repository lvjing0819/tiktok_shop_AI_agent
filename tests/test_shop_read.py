import asyncio
import json
import time
from datetime import datetime, timezone
import httpx
import pytest
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient
from app import shop_api as api, shop_read, store, model
from app.main import app


class Provider:
    def __init__(self):
        self.requests = []
        self.products = [{'id': '10', 'title': '示例手表', 'status': 'ACTIVATE', 'skus': [{'id': '100', 'seller_sku': 'WATCH-红', 'price': {'currency': 'MYR', 'sale_price': '8.10'}}]}]
        self.orders = []
        self.fail = None
        self.after = None
        self.catalog_size = None
        self.repeat_token = False
        self.omit_stock = False
        self.expired_once = False
        self.business_count = 0
        self.malformed = False

    def __call__(self, request):
        self.requests.append(request)
        path, params = request.url.path, dict(request.url.params)
        if path == '/api/v2/token/refresh':
            assert params['refresh_token'] == 'fake-refresh'
            return httpx.Response(200, json={'code': 0, 'data': token('fake-new')})
        assert request.headers['x-tts-access-token'] in ('fake-access', 'fake-new')
        assert params['sign'] == api.sign(path, params, request.content, 'fake-secret')
        assert 'app_secret' not in params and 'access_token' not in params
        if path == '/authorization/202309/shops':
            return httpx.Response(200, json={'code': 0, 'request_id': 'fake-shops', 'data': {'shops': SHOPS}})
        self.business_count += 1
        assert request.method == 'POST'
        assert params['shop_cipher'] in ('fake-my-cipher', 'fake-th-cipher')
        body = json.loads(request.content)
        assert request.content == json.dumps(body, ensure_ascii=False, separators=(',', ':')).encode()
        if self.after:
            self.after()
        code = 105005 if self.fail and self.fail in path else 0
        if self.expired_once and self.business_count == 1:
            code = 105002
        if 'products/search' in path:
            if self.catalog_size:
                offset = int(params.get('page_token', '0'))
                limit = int(params['page_size'])
                end = min(offset + limit, self.catalog_size)
                rows = [{'id': str(1000 + i), 'title': 'watch', 'skus': []} for i in range(offset, end)]
                next_token = str(end) if end < self.catalog_size else ''
                data = {'products': rows, 'total_count': self.catalog_size, 'next_page_token': next_token}
            else:
                data = {'products': self.products, 'total_count': len(self.products)}
            if self.repeat_token:
                data['next_page_token'] = 'repeat'
        elif 'orders/search' in path:
            data = {'orders': self.orders, 'total_count': len(self.orders)}
        else:
            assert path == '/product/202309/inventory/search'
            ids = body.get('product_ids') or ['10']
            data = {'inventory': [{'product_id': i, 'skus': [{'id': '100', 'seller_sku': 'WATCH-红', 'total_available_quantity': 4, 'total_committed_quantity': 2, 'warehouse_inventory': [{'warehouse_id': '9', 'available_quantity': 4, 'committed_quantity': 2}]}]} for i in ids]}
            if self.omit_stock:
                data = {'inventory': []}
        if self.malformed:
            data = {}
        return httpx.Response(200, json={'code': code, 'request_id': f'fake-read-{self.business_count}', 'data': data})


SHOPS = [{'id': '1', 'name': 'Fake MY', 'region': 'MY', 'cipher': 'fake-my-cipher'}, {'id': '2', 'name': 'Fake TH', 'region': 'TH', 'cipher': 'fake-th-cipher'}]


def token(access='fake-access'):
    return {'access_token': access, 'refresh_token': 'fake-refresh', 'open_id': 'fake-seller', 'user_type': 0, 'access_token_expire_in': time.time() + 604800, 'refresh_token_expire_in': time.time() + 864000, 'granted_scopes': []}


@pytest.fixture
def setup(tmp_path, monkeypatch):
    monkeypatch.delenv('DATABASE_URL', raising=False)
    monkeypatch.delenv('APP_TOKEN', raising=False)
    monkeypatch.setenv('DATABASE_PATH', str(tmp_path / 'shop-read.sqlite'))
    monkeypatch.setenv('MODEL_MODE', 'bailian')
    monkeypatch.setenv('SHOP_APP_KEY', 'fake-key')
    monkeypatch.setenv('SHOP_APP_SECRET', 'fake-secret')
    monkeypatch.setenv('SHOP_TOKEN_ENCRYPTION_KEY', Fernet.generate_key().decode())
    with store.db() as conn:
        api.store_tokens(conn, 'default', {'id': 'shop:default', 'generation': 'fake-gen', 'status': 'connected', 'binding': api.config()['binding']}, api.config(), token(), SHOPS)
    provider = Provider()
    actual = httpx.AsyncClient
    monkeypatch.setattr(httpx, 'AsyncClient', lambda **kw: actual(transport=httpx.MockTransport(provider), **kw))
    return TestClient(app), provider


def post(client, operation, **kwargs):
    return client.post('/api/shop/' + operation + '/query', json={'shop_id': '1', **kwargs})


def test_products_body_signature_unicode_and_full_pagination(setup):
    client, p = setup
    p.catalog_size = 101
    r = post(client, 'products', seller_skus=['WATCH-红'])
    assert r.status_code == 200, r.text
    d = r.json()
    assert d['complete'] and d['retrieved_count'] == 101 and d['pages_fetched'] == 2
    reads = [r for r in p.requests if r.method == 'POST']
    assert len(reads) == 2 and reads[1].url.params['page_token'] == '100'
    assert json.loads(reads[0].content)['seller_skus'] == ['WATCH-红']
    assert all(s not in r.text for s in ['fake-my-cipher', 'fake-access', 'fake-secret'])


def test_orders_dates_amounts_and_pii_filtered(setup):
    client, p = setup
    p.orders = [
        {'id': '10', 'status': 'DELIVERED', 'paid_time': 1, 'payment': {'currency': 'MYR', 'total_amount': '0.10'}, 'recipient_address': {'phone_number': 'private-phone'}, 'buyer_email': 'private-email', 'buyer_message': 'private-message'},
        {'id': '11', 'status': 'CANCELLED', 'paid_time': 1, 'payment': {'currency': 'MYR', 'total_amount': '0.20'}},
        {'id': '12', 'status': 'UNPAID', 'payment': {'currency': 'USD', 'total_amount': '3.00'}},
    ]
    r = post(client, 'orders', start_date='2026-11-18', end_date='2026-11-18')
    assert r.status_code == 200, r.text
    d = r.json()
    body = json.loads(p.requests[-1].content)
    assert body['create_time_lt'] - body['create_time_ge'] == 86400
    assert datetime.fromtimestamp(body['create_time_ge'], timezone.utc).isoformat() == '2026-11-17T16:00:00+00:00'
    amounts = d['summary']['payment_amounts_by_currency']
    assert amounts['MYR']['all_status_total_amount'] == '0.30'
    assert amounts['MYR']['paid_non_cancelled_total_amount'] == '0.10'
    assert amounts['USD']['paid_non_cancelled_count'] == 0
    assert all(v not in r.text for v in ['private-phone', 'private-email', 'private-message', 'recipient_address'])
    assert d['date_window']['timezone_source'] == 'market_region_default'


def test_relative_dates_shop_zone_not_host_and_unknown_zone():
    q = shop_read.OrdersQuery(shop_id='1', relative_date='yesterday')
    now = datetime(2026, 11, 18, 17, 30, tzinfo=timezone.utc)
    assert shop_read.date_window(q, SHOPS[0], now)['start_date'] == '2026-11-18'
    assert shop_read.date_window(q, SHOPS[1], now)['timezone'] == 'Asia/Bangkok'
    with pytest.raises(api.ShopError, match='时区'):
        shop_read.date_window(q, {'region': 'US'}, now)


def test_inventory_catalog_batching_and_missing_is_not_zero(setup):
    client, p = setup
    p.catalog_size = 101
    r = post(client, 'inventory')
    assert r.status_code == 200, r.text
    d = r.json()
    assert d['complete'] and d['requested_count'] == 101
    batches = [json.loads(r.content)['product_ids'] for r in p.requests if r.url.path.endswith('inventory/search')]
    assert list(map(len, batches)) == [100, 1]
    assert d['summary']['available_quantity'] == 404 and d['summary']['committed_quantity'] == 202
    p.omit_stock = True
    d = post(client, 'inventory', product_ids=['1001']).json()
    assert not d['complete'] and d['missing_ids'] == ['1001']
    assert '不能当作零库存' in d['warnings'][0]


def test_page_limit_repeated_cursor_empty_and_invalid_shape(setup):
    client, p = setup
    p.catalog_size = 101
    d = post(client, 'products', max_pages=1).json()
    assert not d['complete'] and d['next_page_token'] == '100'
    p.catalog_size = None
    p.repeat_token = True
    d = post(client, 'products').json()
    assert not d['complete'] and d['pages_fetched'] == 2
    assert any('重复' in w for w in d['warnings'])
    p.repeat_token = False
    p.orders = []
    d = post(client, 'orders', relative_date='yesterday').json()
    assert d['complete'] and d['retrieved_count'] == 0
    p.malformed = True
    assert post(client, 'products').status_code == 502


def test_validation_authorization_and_no_arbitrary_operation(setup, monkeypatch):
    client, p = setup
    for op, fields in [('orders', {}), ('orders', {'start_date': '2026-12-01', 'end_date': '2026-11-01'}), ('orders', {'relative_date': 'yesterday', 'start_date': '2026-11-01'}), ('inventory', {'product_ids': ['10'], 'sku_ids': ['100']}), ('products', {'status': 'bad'}), ('products', {'url': 'https://bad.test'})]:
        assert post(client, op, **fields).status_code == 422
    assert client.post('/api/shop/products/query', json={'shop_id': '999'}).status_code == 502
    assert client.post('/api/shop/products/query', json={'shop_id': '1'}, headers={'X-Shop-ID': 'other'}).status_code == 502
    assert p.business_count == 0
    monkeypatch.setenv('APP_TOKEN', 'test-guard')
    assert post(client, 'products').status_code == 401
    with pytest.raises(api.ShopError, match='未开放'):
        asyncio.run(shop_read.post('default', SHOPS[0], 'publish', {}))


def test_business_error_not_verified_and_expired_token_retry(setup):
    client, p = setup
    p.fail = 'orders'
    r = post(client, 'orders', relative_date='yesterday')
    assert r.status_code == 502 and '105005' in r.text
    p.fail = None
    p.expired_once = True
    p.business_count = 0
    r = post(client, 'products')
    assert r.status_code == 200
    assert p.business_count == 2
    assert any(r.url.path.endswith('/refresh') for r in p.requests)
    assert p.requests[-1].headers['x-tts-access-token'] == 'fake-new'


def test_disconnect_during_read_rejects_stale_result(setup):
    client, p = setup
    p.after = lambda: api.disconnect('default')
    r = post(client, 'products')
    assert r.status_code == 502 and '连接已变化' in r.text


def script_router(monkeypatch, decisions):
    choices = iter(decisions)
    def message(messages, tools):
        name, args = next(choices)
        return {'tool_calls': [{'id': 'fake-route', 'type': 'function', 'function': {'name': name, 'arguments': json.dumps(args)}}]}
    monkeypatch.setattr(model, 'message', message)


def test_conversation_clarification_combined_query_followup_and_evidence(setup, monkeypatch):
    client, p = setup
    script_router(monkeypatch, [
        ('query_shop', {'question': '订单和库存', 'resources': ['orders', 'inventory']}),
        ('query_shop', {'question': 'MY', 'resources': ['orders', 'inventory'], 'shop': 'MY'}),
        ('query_shop', {'question': '昨天', 'resources': ['orders', 'inventory'], 'relative_date': 'yesterday'}),
        ('query_shop', {'question': '那TH呢', 'resources': ['orders'], 'shop': 'TH', 'reuse_previous_dates': True}),
    ])
    sid = client.post('/api/sessions', json={}).json()['id']
    def send(text):
        r = client.post(f'/api/sessions/{sid}/messages', json={'content': text})
        assert r.status_code == 200, r.text
        return r.json()
    d = send('订单和库存')
    assert d['last_run']['status'] == 'needs_input' and p.business_count == 0
    d = send('MY')
    assert d['last_run']['status'] == 'needs_input' and p.business_count == 0
    d = send('昨天')
    assert d['last_run']['data_verified'] and d['last_run']['status'] == 'completed'
    assert len(d['query_context']['last_shop']['resources']) == 2
    assert '库存' in d['messages'][-1]['content']
    d = send('那TH呢')
    assert d['query_context']['last_shop']['shop'] == '2'
    run = client.get(f"/api/sessions/{sid}/runs/{d['last_run']['id']}").json()
    assert run['resolved_shop']['region'] == 'TH'
    assert run['sources'][-1]['api_success']
    assert 'fake-th-cipher' not in json.dumps(run)
    assert client.get(f"/api/sessions/{sid}/runs/{run['id']}", headers={'X-Shop-ID': 'other'}).status_code == 404


def test_conversation_partial_and_business_failure_do_not_claim_success(setup, monkeypatch):
    client, p = setup
    p.fail = 'orders'
    script_router(monkeypatch, [('query_shop', {'question': 'MY昨天订单和库存', 'resources': ['orders', 'inventory'], 'shop': 'MY', 'relative_date': 'yesterday'}), ('query_shop', {'question': 'MY昨天订单', 'resources': ['orders'], 'shop': 'MY', 'relative_date': 'yesterday'})])
    sid = client.post('/api/sessions', json={}).json()['id']
    def send(text):
        return client.post(f'/api/sessions/{sid}/messages', json={'content': text}).json()
    d = send('MY昨天订单和库存')
    assert d['last_run']['status'] == 'partial' and d['last_run']['data_verified']
    assert '105005' in d['messages'][-1]['content']
    d = send('MY昨天订单')
    assert d['last_run']['status'] == 'failed' and not d['last_run']['data_verified']


def test_stock_seller_sku_filter_and_unsupported_date_not_ignored(setup, monkeypatch):
    client, p = setup
    d = post(client, 'inventory', seller_skus=['WATCH-红']).json()
    assert d['complete']
    catalog_request = next(r for r in p.requests if r.url.path.endswith('products/search'))
    assert json.loads(catalog_request.content)['seller_skus'] == ['WATCH-红']
    script_router(monkeypatch, [('query_shop', {'question': '昨天库存', 'resources': ['inventory'], 'shop': 'MY', 'relative_date': 'yesterday'})])
    sid = client.post('/api/sessions', json={}).json()['id']
    before = p.business_count
    d = client.post(f'/api/sessions/{sid}/messages', json={'content': '昨天库存'}).json()
    assert d['last_run']['status'] == 'needs_input'
    assert '当前状态' in d['messages'][-1]['content']
    assert p.business_count == before


def test_shop_signed_url_is_not_logged():
    import logging
    record = logging.LogRecord('httpx', logging.INFO, '', 0, 'HTTP POST https://open-api.tiktokglobalshop.com/p?shop_cipher=fake-cipher&sign=fake-sign', (), None)
    assert not api.HideShopSecrets().filter(record)
