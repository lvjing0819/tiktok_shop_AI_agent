"""Explicit product flow. Database stores stage outputs for restart/retry."""
from decimal import Decimal, ROUND_UP
from typing import TypedDict
from langgraph.graph import StateGraph, START, END
from app.model import complete
from app.store import save


class State(TypedDict):
    task: dict


def validate(state):
    t = state['task']
    p = t['product']
    if p['stock'] <= 0:
        raise ValueError('库存不足，无法准备上架')
    price = (Decimal(str(p['cost'])) / (1 - Decimal(str(t['target_margin'])))).quantize(Decimal('.01'), rounding=ROUND_UP)
    t['price'] = str(price)
    t['events'].append('商品校验完成；价格为仅考虑输入成本的示例报价，不含平台费、税费及退款')
    save(t)
    return {'task': t}


def prepare(state):
    t = state['task']
    if 'copy' not in t:
        t['copy'] = complete('你是商品运营专员。仅依据输入事实，用中文输出卖点、英文商品标题和素材需求，不添加认证或功效。', str(t['product']))
        save(t)
    # Explicitly mock media: no images/videos are fabricated as real assets.
    t['materials'] = {'mode': 'mock', 'image': '待接入图片生成服务', 'video': '待接入视频生成服务'}
    t['status'] = 'awaiting_review'
    t['events'].append('商品方案已准备；图片和视频接口仍为模拟占位，等待审核')
    save(t)
    return {'task': t}


builder = StateGraph(State)
builder.add_node('validate', validate)
builder.add_node('prepare', prepare)
builder.add_edge(START, 'validate')
builder.add_edge('validate', 'prepare')
builder.add_edge('prepare', END)
graph = builder.compile()


def run(task):
    task['status'] = 'running'
    save(task)
    try:
        return graph.invoke({'task': task})['task']
    except Exception:
        # Do not persist provider exception text: may include request metadata.
        task['status'] = 'failed'
        task['events'].append('执行失败：请检查库存、模型配置及服务状态后重试')
        return save(task)
