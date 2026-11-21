"""Canonical goods, multi-warehouse inventory and operational snapshots.

Platform listing stock never becomes warehouse stock. All manual stock mutations
are scoped, transactional, revision checked and idempotent.
"""
import hashlib
import json
import time
from datetime import datetime, timezone
from decimal import Decimal
from uuid import uuid4
from typing import Literal
from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy import Table, Column, String, Text, Integer, Float, UniqueConstraint, select, update, and_
from app import store

M = store.metadata
warehouses = Table('warehouses_v1', M, Column('id', String(64), primary_key=True), Column('workspace', String(64), index=True, nullable=False), Column('name', String(200)), Column('country', String(2)), Column('kind', String(20)), Column('created_at', Float))
goods = Table('goods_v1', M, Column('id', String(64), primary_key=True), Column('workspace', String(64), index=True, nullable=False), Column('name', String(200)), Column('facts', Text), Column('revision', Integer), Column('created_at', Float))
skus = Table('skus_v1', M, Column('id', String(64), primary_key=True), Column('workspace', String(64), index=True, nullable=False), Column('product_id', String(64)), Column('code', String(100)), Column('name', String(200)), Column('created_at', Float), UniqueConstraint('workspace', 'code'))
stock = Table('warehouse_stock_v1', M, Column('id', String(64), primary_key=True), Column('workspace', String(64), index=True, nullable=False), Column('warehouse_id', String(64)), Column('sku_id', String(64)), Column('on_hand', Integer), Column('warehouse_reserved', Integer), Column('safety_stock', Integer), Column('revision', Integer), Column('updated_at', Float), Column('source', String(32)), UniqueConstraint('workspace', 'warehouse_id', 'sku_id'))
reservations = Table('stock_reservations_v1', M, Column('id', String(64), primary_key=True), Column('workspace', String(64), index=True, nullable=False), Column('warehouse_id', String(64)), Column('sku_id', String(64)), Column('order_ref', String(200)), Column('quantity', Integer), Column('state', String(32)), Column('created_at', Float), Column('updated_at', Float), UniqueConstraint('workspace', 'warehouse_id', 'sku_id', 'order_ref'))
transfers = Table('stock_transfers_v1', M, Column('id', String(64), primary_key=True), Column('workspace', String(64), index=True, nullable=False), Column('source_id', String(64)), Column('destination_id', String(64)), Column('sku_id', String(64)), Column('quantity', Integer), Column('state', String(32)), Column('created_at', Float), Column('received_at', Float))
events = Table('inventory_events_v1', M, Column('id', String(64), primary_key=True), Column('workspace', String(64), index=True, nullable=False), Column('operation_id', String(100)), Column('fingerprint', String(64)), Column('kind', String(64)), Column('entity_id', String(64)), Column('payload', Text), Column('created_at', Float), UniqueConstraint('workspace', 'operation_id'))
mappings = Table('sku_mappings_v1', M, Column('id', String(64), primary_key=True), Column('workspace', String(64), index=True, nullable=False), Column('sku_id', String(64)), Column('source', String(32)), Column('account_id', String(64)), Column('external_sku', String(100)), UniqueConstraint('workspace', 'source', 'account_id', 'external_sku'))
orders = Table('order_snapshots_v1', M, Column('id', String(100), primary_key=True), Column('workspace', String(64), index=True, nullable=False), Column('shop_id', String(40)), Column('order_id', String(40)), Column('create_time', Float), Column('status', String(40)), Column('payload', Text), Column('synced_at', Float), UniqueConstraint('workspace', 'shop_id', 'order_id'))
financials = Table('order_financials_v1', M, Column('id', String(100), primary_key=True), Column('workspace', String(64), index=True, nullable=False), Column('shop_id', String(40)), Column('order_id', String(40)), Column('payload', Text), Column('revision', Integer), Column('updated_at', Float), UniqueConstraint('workspace', 'shop_id', 'order_id'))


class BusinessError(Exception):
    pass


class Input(BaseModel):
    model_config = ConfigDict(extra='forbid')


class WarehouseInput(Input):
    name: str = Field(min_length=1, max_length=200)
    country: str = Field(pattern=r'^[A-Z]{2}$')
    kind: Literal['mainland', 'overseas']


class GoodsInput(Input):
    name: str = Field(min_length=1, max_length=200)
    facts: str = Field(default='', max_length=8000)


class SKUInput(Input):
    product_id: str = Field(min_length=1, max_length=64)
    code: str = Field(min_length=1, max_length=100)
    name: str = Field(default='', max_length=200)


class MappingInput(Input):
    sku_id: str = Field(min_length=1, max_length=64)
    source: Literal['tiktok_shop', 'warehouse']
    account_id: str = Field(min_length=1, max_length=64)
    external_sku: str = Field(min_length=1, max_length=100)


class Mutation(Input):
    operation_id: str = Field(pattern=r'^[a-zA-Z0-9_-]{8,100}$')
    reason: str = Field(min_length=1, max_length=500)


class StockInput(Mutation):
    warehouse_id: str = Field(min_length=1, max_length=64)
    sku_id: str = Field(min_length=1, max_length=64)
    on_hand: int = Field(ge=0, le=100000000)
    warehouse_reserved: int = Field(default=0, ge=0, le=100000000)
    safety_stock: int = Field(default=0, ge=0, le=100000000)
    expected_revision: int = Field(ge=0)


class ReserveInput(Mutation):
    warehouse_id: str = Field(min_length=1, max_length=64)
    sku_id: str = Field(min_length=1, max_length=64)
    order_ref: str = Field(min_length=1, max_length=200)
    quantity: int = Field(gt=0, le=100000000)


class ReservationAction(Mutation):
    action: Literal['warehouse_ack', 'ship', 'cancel']
    warehouse_confirmed: bool = False


class TransferInput(Mutation):
    source_id: str = Field(min_length=1, max_length=64)
    destination_id: str = Field(min_length=1, max_length=64)
    sku_id: str = Field(min_length=1, max_length=64)
    quantity: int = Field(gt=0, le=100000000)


class OrderSyncInput(Input):
    timezone: str | None = Field(default=None,max_length=80)
    shop_id: str = Field(pattern=r'^[0-9]{1,40}$')
    start_date: str = Field(pattern=r'^\d{4}-\d{2}-\d{2}$')
    end_date: str = Field(pattern=r'^\d{4}-\d{2}-\d{2}$')


class FinanceInput(Input):
    currency: str = Field(pattern=r'^[A-Z]{3}$')
    procurement: str = Field(max_length=50)
    logistics: str = Field(max_length=50)
    advertising: str = Field(max_length=50)
    platform_fee: str = Field(max_length=50)
    refund: str = Field(max_length=50)
    other: str = Field(max_length=50)
    settled_net: str | None = Field(default=None,max_length=50)
    costs_complete: bool = False
    expected_revision: int = Field(ge=0)
    source_note: str = Field(min_length=1, max_length=1000)

    @field_validator('procurement', 'logistics', 'advertising', 'platform_fee', 'refund', 'other', 'settled_net')
    @classmethod
    def money(cls, v):
        if v is None:
            return v
        try:
            n = Decimal(v)
            assert n.is_finite() and 0 <= n <= Decimal('1000000000') and n.as_tuple().exponent >= -4
        except Exception:
            raise ValueError('金额须为非负、最多4位小数的有效数字') from None
        return format(n, 'f')


def row(conn, table, workspace, key):
    return conn.execute(select(table).where(table.c.workspace == workspace, table.c.id == key).with_for_update()).mappings().first()


def require(conn, table, workspace, key):
    r = row(conn, table, workspace, key)
    if not r:
        raise BusinessError('记录不存在或不属于当前工作区')
    return dict(r)


def list_rows(table, workspace, limit=None):
    with store.db() as conn:
        query=select(table).where(table.c.workspace == workspace)
        if limit is not None:query=query.limit(limit)
        return [dict(r) for r in conn.execute(query).mappings()]


def create_warehouse(workspace, body):
    if (body.kind == 'mainland') != (body.country == 'CN'):
        raise BusinessError('大陆仓的国家须为CN；其他国家使用海外仓')
    item = {'id': str(uuid4()), 'workspace': workspace, **body.model_dump(), 'created_at': time.time()}
    with store.db(write=True) as conn:
        conn.execute(warehouses.insert().values(**item))
    return {**item, 'api_status': 'not_connected'}


def create_goods(workspace, body):
    item = {'id': str(uuid4()), 'workspace': workspace, **body.model_dump(), 'revision': 1, 'created_at': time.time()}
    with store.db(write=True) as conn:
        conn.execute(goods.insert().values(**item))
    return item


def create_sku(workspace, body):
    item = {'id': str(uuid4()), 'workspace': workspace, **body.model_dump(), 'created_at': time.time()}
    with store.db(write=True) as conn:
        require(conn, goods, workspace, body.product_id)
        if conn.execute(select(skus.c.id).where(skus.c.workspace == workspace, skus.c.code == body.code)).first():
            raise BusinessError('当前工作区已存在该SKU编码')
        conn.execute(skus.insert().values(**item))
    return item


def create_mapping(workspace, body):
    item = {'id': str(uuid4()), 'workspace': workspace, **body.model_dump()}
    with store.db(write=True) as conn:
        require(conn, skus, workspace, body.sku_id)
        if body.source == 'warehouse':
            require(conn, warehouses, workspace, body.account_id)
        key = and_(mappings.c.workspace == workspace, mappings.c.source == body.source, mappings.c.account_id == body.account_id, mappings.c.external_sku == body.external_sku)
        previous = conn.execute(select(mappings).where(key)).mappings().first()
        if previous:
            if previous['sku_id'] != body.sku_id:
                raise BusinessError('外部SKU已关联到其他内部SKU')
            return dict(previous)
        conn.execute(mappings.insert().values(**item))
    return item


def fingerprint(kind, body):
    return hashlib.sha256(json.dumps([kind, body.model_dump()], sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def prior(conn, workspace, kind, body):
    r = conn.execute(select(events).where(events.c.workspace == workspace, events.c.operation_id == body.operation_id)).mappings().first()
    if r and r['fingerprint'] != fingerprint(kind, body):
        raise BusinessError('幂等操作号已用于不同内容，拒绝重复执行')
    return dict(r) if r else None


def event(conn, workspace, kind, body, entity_id, detail):
    conn.execute(events.insert().values(id=str(uuid4()), workspace=workspace, operation_id=body.operation_id, fingerprint=fingerprint(kind, body), kind=kind, entity_id=entity_id, payload=json.dumps({'request': body.model_dump(), 'change': detail}, ensure_ascii=False), created_at=time.time()))


def stock_row(conn, workspace, warehouse_id, sku_id):
    # Lock the parent warehouse even for a not-yet-created snapshot.
    require(conn, warehouses, workspace, warehouse_id)
    require(conn, skus, workspace, sku_id)
    return conn.execute(select(stock).where(stock.c.workspace == workspace, stock.c.warehouse_id == warehouse_id, stock.c.sku_id == sku_id).with_for_update()).mappings().first()


def local_reserved(conn, workspace, warehouse_id, sku_id):
    rows = conn.execute(select(reservations.c.quantity).where(reservations.c.workspace == workspace, reservations.c.warehouse_id == warehouse_id, reservations.c.sku_id == sku_id, reservations.c.state == 'local')).all()
    return sum(r[0] for r in rows)


def available(conn, s):
    return s['on_hand'] - s['warehouse_reserved'] - s['safety_stock'] - local_reserved(conn, s['workspace'], s['warehouse_id'], s['sku_id'])


def set_stock(workspace, body):
    with store.db(write=True) as conn:
        old = prior(conn, workspace, 'manual_snapshot', body)
        if old:
            return require(conn, stock, workspace, old['entity_id'])
        s = stock_row(conn, workspace, body.warehouse_id, body.sku_id)
        if (s['revision'] if s else 0) != body.expected_revision:
            raise BusinessError('库存版本已变化，请刷新后重新确认')
        if body.warehouse_reserved > body.on_hand:
            raise BusinessError('仓库占用不能超过实物库存')
        # An API snapshot must acknowledge existing warehouse reservations too.
        managed = sum(r[0] for r in conn.execute(select(reservations.c.quantity).where(reservations.c.workspace == workspace, reservations.c.warehouse_id == body.warehouse_id, reservations.c.sku_id == body.sku_id, reservations.c.state == 'warehouse')).all())
        if body.warehouse_reserved < managed:
            raise BusinessError('仓库占用小于已确认的订单占用，请先对账处理订单状态')
        item = {'id': s['id'] if s else str(uuid4()), 'workspace': workspace, 'warehouse_id': body.warehouse_id, 'sku_id': body.sku_id, 'on_hand': body.on_hand, 'warehouse_reserved': body.warehouse_reserved, 'safety_stock': body.safety_stock, 'revision': body.expected_revision + 1, 'updated_at': time.time(), 'source': 'manual'}
        if s:
            conn.execute(update(stock).where(stock.c.id == s['id']).values(**item))
        else:
            conn.execute(stock.insert().values(**item))
        event(conn, workspace, 'manual_snapshot', body, item['id'], {'previous_on_hand': s['on_hand'] if s else None, 'on_hand': body.on_hand, 'note': '人工确认快照差异，不冒充仓库实际出入库流水'})
    return item


def reserve(workspace, body):
    with store.db(write=True) as conn:
        old = prior(conn, workspace, 'reserve', body)
        if old:
            return require(conn, reservations, workspace, old['entity_id'])
        existing=conn.execute(select(reservations).where(reservations.c.workspace==workspace, reservations.c.warehouse_id==body.warehouse_id, reservations.c.sku_id==body.sku_id, reservations.c.order_ref==body.order_ref)).mappings().first()
        if existing:
            if existing['quantity']!=body.quantity or existing['state'] not in ('local','warehouse'):
                raise BusinessError('该订单行已有不同数量或已结束的占用，请对账后处理')
            event(conn,workspace,'reserve',body,existing['id'],{'reused_business_key':True})
            return dict(existing)
        s = stock_row(conn, workspace, body.warehouse_id, body.sku_id)
        if not s:
            raise BusinessError('该仓库SKU库存未知，请先录入真实库存')
        if available(conn, s) < body.quantity:
            raise BusinessError('可售库存不足')
        item = {'id': str(uuid4()), 'workspace': workspace, 'warehouse_id': body.warehouse_id, 'sku_id': body.sku_id, 'order_ref': body.order_ref, 'quantity': body.quantity, 'state': 'local', 'created_at': time.time(), 'updated_at': time.time()}
        conn.execute(reservations.insert().values(**item))
        event(conn, workspace, 'reserve', body, item['id'], {'quantity': body.quantity})
    return item


def reservation_action(workspace, key, body):
    kind = 'reservation:' + key + ':' + body.action
    with store.db(write=True) as conn:
        old = prior(conn, workspace, kind, body)
        if old:
            return require(conn, reservations, workspace, key)
        r = require(conn, reservations, workspace, key)
        s = stock_row(conn, workspace, r['warehouse_id'], r['sku_id'])
        if r['state'] not in ('local', 'warehouse'):
            raise BusinessError('订单占用已结束')
        q = r['quantity'];changes = {};new_state = r['state']
        if body.action == 'warehouse_ack':
            if r['state'] != 'local' or not body.warehouse_confirmed:
                raise BusinessError('需要确认仓库已接收，且该占用尚未确认')
            if s['warehouse_reserved']+q>s['on_hand']:
                raise BusinessError('仓库确认后的占用超过实物库存，请先对账')
            changes['warehouse_reserved'] = s['warehouse_reserved'] + q
            new_state = 'warehouse'
        elif body.action == 'ship':
            if not body.warehouse_confirmed or s['on_hand'] < q:
                raise BusinessError('实际出库需要仓库确认且实物库存充足')
            changes['on_hand'] = s['on_hand'] - q
            if r['state'] == 'warehouse':
                changes['warehouse_reserved'] = s['warehouse_reserved'] - q
            new_state = 'shipped'
        else:
            if r['state'] == 'warehouse':
                if not body.warehouse_confirmed:
                    raise BusinessError('仓库占用取消必须先确认仓库已释放')
                changes['warehouse_reserved'] = s['warehouse_reserved'] - q
            new_state = 'cancelled'
        if changes:
            changes.update(revision=s['revision'] + 1, updated_at=time.time())
            conn.execute(update(stock).where(stock.c.id == s['id']).values(**changes))
        conn.execute(update(reservations).where(reservations.c.id == key).values(state=new_state, updated_at=time.time()))
        event(conn, workspace, kind, body, key, {'previous_state': r['state'], 'state': new_state, 'stock_changes': changes})
        return require(conn, reservations, workspace, key)


def dispatch_transfer(workspace, body):
    if body.source_id == body.destination_id:
        raise BusinessError('调出仓和调入仓不能相同')
    with store.db(write=True) as conn:
        old = prior(conn, workspace, 'transfer_dispatch', body)
        if old:
            return require(conn, transfers, workspace, old['entity_id'])
        # Stable lock order across warehouses prevents opposing-transfer deadlocks.
        for key in sorted([body.source_id, body.destination_id]):
            require(conn, warehouses, workspace, key)
        s = stock_row(conn, workspace, body.source_id, body.sku_id)
        if not s or available(conn, s) < body.quantity:
            raise BusinessError('调出仓库存未知或可售库存不足')
        item = {'id': str(uuid4()), 'workspace': workspace, 'source_id': body.source_id, 'destination_id': body.destination_id, 'sku_id': body.sku_id, 'quantity': body.quantity, 'state': 'in_transit', 'created_at': time.time(), 'received_at': None}
        conn.execute(transfers.insert().values(**item))
        conn.execute(update(stock).where(stock.c.id == s['id']).values(on_hand=s['on_hand'] - body.quantity, revision=s['revision'] + 1, updated_at=time.time()))
        event(conn, workspace, 'transfer_dispatch', body, item['id'], {'quantity': body.quantity, 'state': 'in_transit'})
    return item


def receive_transfer(workspace, key, body):
    kind = 'transfer_receive:' + key
    with store.db(write=True) as conn:
        old = prior(conn, workspace, kind, body)
        if old:
            return require(conn, transfers, workspace, key)
        t = require(conn, transfers, workspace, key)
        if t['state'] != 'in_transit':
            raise BusinessError('调拨已经收货，不能重复增加库存')
        s = stock_row(conn, workspace, t['destination_id'], t['sku_id'])
        if not s:
            raise BusinessError('调入仓该SKU原始库存未知，请先录入盘点数（不含本次收货）')
        conn.execute(update(stock).where(stock.c.id == s['id']).values(on_hand=s['on_hand'] + t['quantity'], revision=s['revision'] + 1, updated_at=time.time()))
        conn.execute(update(transfers).where(transfers.c.id == key).values(state='received', received_at=time.time()))
        event(conn, workspace, kind, body, key, {'quantity': t['quantity'], 'state': 'received'})
        return require(conn, transfers, workspace, key)


def inventory_view(workspace):
    with store.db() as conn:
        ws = [dict(r) for r in conn.execute(select(warehouses).where(warehouses.c.workspace == workspace)).mappings()]
        sku_rows = [dict(r) for r in conn.execute(select(skus).where(skus.c.workspace == workspace)).mappings()]
        snapshots = {(r['warehouse_id'], r['sku_id']): dict(r) for r in conn.execute(select(stock).where(stock.c.workspace == workspace)).mappings()}
        local = {}
        for r in conn.execute(select(reservations.c.warehouse_id,reservations.c.sku_id,reservations.c.quantity).where(reservations.c.workspace==workspace,reservations.c.state=='local')):
            key=(r.warehouse_id,r.sku_id)
            local[key]=local.get(key,0)+r.quantity
        rows = []
        for w in ws:
            for sku in sku_rows:
                s = snapshots.get((w['id'], sku['id']))
                held=local.get((w['id'],sku['id']),0)
                qty = s['on_hand']-s['warehouse_reserved']-s['safety_stock']-held if s else None
                rows.append({'warehouse_id': w['id'], 'warehouse': w['name'], 'country': w['country'], 'kind': w['kind'], 'sku_id': sku['id'], 'sku': sku['code'], 'product_id': sku['product_id'], 'on_hand': s['on_hand'] if s else None, 'warehouse_reserved': s['warehouse_reserved'] if s else None, 'local_reserved': held, 'safety_stock': s['safety_stock'] if s else None, 'available': max(0, qty) if qty is not None else None, 'over_reserved': qty is not None and qty < 0, 'low_stock': qty is not None and qty <= 0, 'source': s['source'] if s else 'unknown', 'updated_at': s['updated_at'] if s else None, 'revision': s['revision'] if s else 0, 'stale': bool(s and time.time() - s['updated_at'] > 86400)})
    return {'warehouses': [{**w, 'api_status': 'not_connected'} for w in ws], 'rows': rows, 'transfers': list_rows(transfers, workspace), 'reservations': list_rows(reservations, workspace), 'source': 'warehouse_records', 'channel_inventory_is_physical': False, 'auto_sync': False}


async def sync_orders(workspace, body):
    from app import shop_read
    request = shop_read.OrdersQuery(shop_id=body.shop_id, start_date=body.start_date, end_date=body.end_date,timezone=body.timezone)
    result = await shop_read.execute(workspace, 'orders', request)
    now = time.time()
    with store.db(write=True) as conn:
        for r in result['rows']:
            key = hashlib.sha256(json.dumps([workspace, body.shop_id, r['id']]).encode()).hexdigest()
            previous = row(conn, orders, workspace, key)
            item = {'id': key, 'workspace': workspace, 'shop_id': body.shop_id, 'order_id': r['id'], 'create_time': r.get('create_time'), 'status': r.get('status'), 'payload': json.dumps(r, ensure_ascii=False), 'synced_at': now}
            if previous:
                conn.execute(update(orders).where(orders.c.id == key).values(**item))
            else:
                conn.execute(orders.insert().values(**item))
        receipt = {'id': hashlib.sha256(json.dumps([workspace, body.shop_id]).encode()).hexdigest(), 'shop_id': body.shop_id, 'synced_at': now, 'complete': result['complete'], 'window': result['date_window'], 'retrieved_count': result['retrieved_count'], 'warnings': result['warnings'], 'request_ids': [e['result']['structuredContent'].get('request_id') for e in result['evidence']]}
        store.put(conn, 'business_sync', receipt, workspace)
    return receipt


def save_finance(workspace, shop_id, order_id, body):
    with store.db(write=True) as conn:
        o = conn.execute(select(orders).where(orders.c.workspace == workspace, orders.c.shop_id == shop_id, orders.c.order_id == order_id).with_for_update()).mappings().first()
        if not o:
            raise BusinessError('请先同步该订单')
        payment = json.loads(o['payload']).get('payment', {})
        if payment.get('currency') != body.currency:
            raise BusinessError('成本必须按该订单币种录入，并在来源备注说明换算依据')
        old = row(conn, financials, workspace, o['id'])
        if (old['revision'] if old else 0) != body.expected_revision:
            raise BusinessError('财务记录版本已变化')
        item = {'id': o['id'], 'workspace': workspace, 'shop_id': shop_id, 'order_id': order_id, 'payload': json.dumps(body.model_dump(), ensure_ascii=False), 'revision': body.expected_revision + 1, 'updated_at': time.time()}
        if old:
            conn.execute(update(financials).where(financials.c.id == o['id']).values(**item))
        else:
            conn.execute(financials.insert().values(**item))
        return {**item, 'payload': body.model_dump(), 'source': 'manual', 'settlement_api_connected': False}


def dashboard(workspace):
    os = list_rows(orders, workspace)
    fs = {r['id']: r for r in list_rows(financials, workspace)}
    amounts = {};statuses = {};rows = []
    for r in os:
        data = json.loads(r['payload']);payment = data.get('payment', {});currency = payment.get('currency')
        statuses[r['status']] = statuses.get(r['status'], 0) + 1
        f = json.loads(fs[r['id']]['payload']) if r['id'] in fs else None
        amount = None
        try:
            amount = Decimal(payment['total_amount'])
            if not amount.is_finite(): amount = None
        except Exception: pass
        estimate = settled = None
        if f and f['costs_complete'] and amount is not None:
            base_costs = sum(Decimal(f[k]) for k in ('procurement', 'logistics', 'advertising', 'other'))
            if data.get('paid_time') and data.get('status') not in ('UNPAID','CANCELLED'):
                estimate = amount - base_costs - Decimal(f['platform_fee']) - Decimal(f['refund'])
            if f['settled_net'] is not None:
                # Net settlement already excludes platform deductions/refunds.
                settled = Decimal(f['settled_net']) - base_costs
        if currency and amount is not None:
            group = amounts.setdefault(currency, {'all_status_order_amount': Decimal(0), 'confirmed_paid_non_cancelled_amount': Decimal(0), 'estimated_contribution_known_orders': Decimal(0), 'settled_profit_known_orders': Decimal(0), 'cost_complete_orders': 0, 'settled_known_orders': 0})
            group['all_status_order_amount'] += amount
            if data.get('paid_time') and data.get('status') not in ('UNPAID', 'CANCELLED'):
                group['confirmed_paid_non_cancelled_amount'] += amount
            if estimate is not None:
                group['estimated_contribution_known_orders'] += estimate;group['cost_complete_orders'] += 1
            if settled is not None:
                group['settled_profit_known_orders'] += settled;group['settled_known_orders'] += 1
        rows.append({'shop_id': r['shop_id'], 'order_id': r['order_id'], 'status': r['status'], 'create_time': r['create_time'], 'currency': currency, 'total_amount': str(amount) if amount is not None else None, 'estimated_contribution': str(estimate) if estimate is not None else None, 'settled_profit': str(settled) if settled is not None else None, 'costs_complete': bool(f and f['costs_complete']), 'finance_revision': fs[r['id']]['revision'] if r['id'] in fs else 0, 'finance_input': f, 'synced_at': r['synced_at']})
    groups = {c: {k: str(v) if isinstance(v, Decimal) else v for k,v in g.items()} for c,g in amounts.items()}
    # No known profit rows means unknown, rather than a zero-profit claim.
    for g in groups.values():
        if not g['cost_complete_orders']:g['estimated_contribution_known_orders'] = None
        if not g['settled_known_orders']:g['settled_profit_known_orders'] = None
    return {'generated_at': time.time(), 'inventory': inventory_view(workspace), 'orders': {'count': len(rows), 'status_counts': statuses, 'rows': rows, 'sync': store.list_records('business_sync', workspace), 'polling': store.list_records('order_poll',workspace), 'coverage': 'persisted snapshots only; see each sync date window and completeness'}, 'finance': {'by_currency': groups, 'missing_cost_orders': sum(not r['costs_complete'] for r in rows), 'settlement_api_connected': False, 'cost_source': 'manual', 'note': '订单金额不是结算收入；贡献估算仅含人工确认完整成本的订单。结算利润基于人工录入净结算额，不重复扣平台费与退款。'}}


class PricingInput(Input):
    unit_cost: str
    logistics: str = Field(max_length=50)
    fixed_fees: str
    currency: str = Field(pattern=r'^[A-Z]{3}$')
    fee_rate: str
    target_margin: str
    listing_cap: int = Field(default=10, ge=1, le=100000)
    expected_revision: int = Field(ge=0)
    source_note: str = Field(min_length=1, max_length=1000)

    @field_validator('unit_cost', 'logistics', 'fixed_fees')
    @classmethod
    def amounts(cls,v):
        return FinanceInput.money(v)

    @field_validator('fee_rate', 'target_margin')
    @classmethod
    def ratio(cls,v):
        try:
            n=Decimal(v);assert n.is_finite() and 0<=n<1
        except Exception:raise ValueError('比例须为0至1之间的数值') from None
        return str(n)


def save_pricing(workspace, sku_id, market, body):
    currencies={'MY':'MYR','TH':'THB','US':'USD','GB':'GBP','SG':'SGD','VN':'VND','PH':'PHP','JP':'JPY'}
    if body.currency!=currencies.get(market):raise BusinessError('定价规则须使用目标市场币种；请先人工换算成本并备注依据')
    if Decimal(body.fee_rate)+Decimal(body.target_margin)>=1:raise BusinessError('费率与目标利润率之和必须小于1')
    key=sku_id+':'+market
    with store.db(write=True) as conn:
        require(conn,skus,workspace,sku_id)
        old=store.get(key,'pricing_profile',workspace,conn)
        if (old['revision'] if old else 0)!=body.expected_revision:raise BusinessError('定价规则版本已变化')
        item={'id':key,'sku_id':sku_id,'market':market,**body.model_dump(),'revision':body.expected_revision+1,'updated_at':time.time(),'source':'manual_confirmed'}
        return store.put(conn,'pricing_profile',item,workspace)
