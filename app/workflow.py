"""Persisted graph, approval interrupt, resumable nodes and mock publication."""
import json
from decimal import Decimal, ROUND_UP
from typing import TypedDict
from langgraph.graph import StateGraph, START, END
from langgraph.types import interrupt
from app import model


class State(TypedDict):
    task: dict


def validate(state):
    t = dict(state['task'])
    if t['product']['stock'] <= 0:
        raise ValueError('库存不足，无法准备上架')
    t['price'] = str((Decimal(str(t['product']['cost']))/(1-Decimal(str(t['target_margin'])))).quantize(Decimal('.01'), rounding=ROUND_UP))
    t['events'] = t['events'] + ['商品校验完成；报价仅含商品成本，未含费用税费']
    return {'task': t}


def prepare(state):
    t = dict(state['task'])
    t['copy'] = model.complete('你是商品运营专员。依据商品事实和已确认店铺偏好生成卖点、英文标题及素材需求，不添加认证功效。', json.dumps({'product': t['product'], 'preferences': t.get('preferences', {})}, ensure_ascii=False))
    t['materials'] = {'mode': 'mock', 'image': '待接入图片生成服务', 'video': '待接入视频生成服务'}
    t['status'] = 'awaiting_review'
    t['events'] = t['events'] + ['方案已准备，等待审核；媒体尚未生成']
    return {'task': t}


def review(state):
    t = dict(state['task'])
    decision = interrupt({'task_id': t['id'], 'version': t['version'], 'price': t['price'], 'copy': t['copy']})
    if decision['version'] != t['version']:
        raise ValueError('审核版本不匹配')
    t['decision'] = decision
    t['approved_version'] = t['version'] if decision['approved'] else None
    t['status'] = 'approved' if decision['approved'] else 'rejected'
    t['events'] = t['events'] + ['已批准' if decision['approved'] else '已驳回']
    return {'task': t}


def publish(state):
    t = dict(state['task'])
    if t['status'] != 'approved' or t['approved_version'] != t['version']:
        raise ValueError('当前版本未经审批')
    # Stable id is an idempotency key for MOCK publication only.
    t['listing_id'] = 'mock-'+t['id']
    t['status'] = 'published'
    t['events'] = t['events'] + ['已模拟上架，未访问 TikTok Shop']
    return {'task': t}


def build(checkpointer):
    g = StateGraph(State)
    for name, fn in [('validate', validate), ('prepare', prepare), ('review', review), ('publish', publish)]:
        g.add_node(name, fn)
    g.add_edge(START, 'validate')
    g.add_edge('validate', 'prepare')
    g.add_edge('prepare', 'review')
    g.add_conditional_edges('review', lambda s: 'publish' if s['task']['status'] == 'approved' else END)
    g.add_edge('publish', END)
    return g.compile(checkpointer=checkpointer)
