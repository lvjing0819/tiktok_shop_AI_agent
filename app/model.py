"""Bailian chat adapter, with explicit offline demo and safe error reporting."""
import os
import httpx
from app import execution_context


class ModelError(Exception):
    pass


def mode():
    return os.getenv('MODEL_MODE', 'demo')


def message(messages, tools=None):
    if mode() == 'demo':
        return {'role': 'assistant', 'content': '[演示模式，未调用模型] 已读取当前会话和店铺记忆。配置百炼后可进行真实对话与分析。'}
    if mode() != 'bailian':
        raise ModelError('MODEL_MODE 仅支持 demo 或 bailian')
    key = os.getenv('DASHSCOPE_API_KEY', '').strip()
    if not key:
        raise ModelError('尚未配置百炼密钥 DASHSCOPE_API_KEY')
    base = os.getenv('BAILIAN_BASE_URL', 'https://dashscope.aliyuncs.com/compatible-mode/v1')
    if not base.startswith('https://'):
        raise ModelError('百炼接口必须使用 HTTPS')
    max_tokens = execution_context.model_start(2000)
    try:
        with httpx.Client(timeout=httpx.Timeout(90, connect=10)) as client:
            response = client.post(base.rstrip('/')+'/chat/completions',
                headers={'Authorization': 'Bearer '+key},
                json={'model': os.getenv('BAILIAN_MODEL', 'qwen-plus'), 'messages': messages, 'max_tokens': max_tokens, **({'enable_thinking': execution_context.thinking.get()} if execution_context.thinking.get() is not None else {}), **({'tools': tools, 'tool_choice': 'auto'} if tools else {})})
            if response.status_code >= 400:
                errors = {401: '百炼密钥无效或地域不匹配', 403: '百炼模型访问被拒绝', 429: '百炼限流或配额不足'}
                raise ModelError(errors.get(response.status_code, f'百炼服务返回 HTTP {response.status_code}'))
            payload = response.json()
            execution_context.model_end(payload.get('usage', {}))
            value = payload['choices'][0]['message']
            if not value.get('tool_calls') and not (isinstance(value.get('content'), str) and value['content'].strip()):
                raise ModelError('百炼未返回有效文本或工具调用')
            return {k: value[k] for k in ('role', 'content', 'tool_calls') if k in value}
    except (httpx.HTTPError, KeyError, IndexError, ValueError) as exc:
        raise ModelError('百炼连接超时、网络异常或返回格式无效') from exc


def chat(messages):
    value = message(messages)
    if not isinstance(value.get('content'), str):
        raise ModelError('百炼未返回文本')
    return value['content']


def complete(role, prompt):
    return chat([{'role': 'system', 'content': role}, {'role': 'user', 'content': prompt}])
