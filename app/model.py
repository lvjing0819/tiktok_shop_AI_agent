"""Bailian-only adapter. Demo responses never pretend to be model output."""
import os
import httpx


def complete(role: str, prompt: str) -> str:
    if os.getenv('MODEL_MODE', 'demo') == 'demo':
        return f'[演示模式，无模型调用] {role}：已接收任务。请配置百炼后获取真实分析。'
    key = os.getenv('DASHSCOPE_API_KEY')
    if not key:
        raise ValueError('尚未配置 DASHSCOPE_API_KEY')
    base = os.getenv('BAILIAN_BASE_URL', 'https://dashscope.aliyuncs.com/compatible-mode/v1')
    if not base.startswith('https://'):
        raise ValueError('百炼接口必须使用 HTTPS')
    with httpx.Client(timeout=90) as client:
        response = client.post(base.rstrip('/') + '/chat/completions',
            headers={'Authorization': f'Bearer {key}'},
            json={'model': os.getenv('BAILIAN_MODEL', 'qwen-plus'),
                  'messages': [{'role': 'system', 'content': role},
                               {'role': 'user', 'content': prompt}]})
        response.raise_for_status()
        return response.json()['choices'][0]['message']['content']
