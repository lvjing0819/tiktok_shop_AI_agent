"""Execution telemetry propagated through asyncio.to_thread, never model reasoning."""
from contextvars import ContextVar

observer = ContextVar('agent_execution_observer', default=None)
thinking = ContextVar('agent_model_thinking', default=None)


def model_start(max_tokens):
    scope = observer.get()
    return scope.model_start(max_tokens) if scope else max_tokens


def model_end(usage):
    scope = observer.get()
    if scope:
        scope.model_end(usage)


def remote_call(name):
    scope = observer.get()
    if scope:
        scope.remote_call(name)
