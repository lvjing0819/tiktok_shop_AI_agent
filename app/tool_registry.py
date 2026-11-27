"""Static, reviewed tool definitions shared by planning and execution."""
from dataclasses import dataclass
from typing import Callable
from jsonschema import Draft202012Validator


class ExecutionError(Exception):
    pass


@dataclass(frozen=True)
class Tool:
    name: str
    description: str
    agents: tuple[str, ...]
    schema: dict
    handler: Callable
    effect: str = 'read'
    timeout_seconds: int = 90
    available: bool = True
    version: str = '1'
    output_schema: dict | None = None
    execution: str = 'trusted_service'
    sandbox_job: str | None = None

    def __post_init__(self):
        Draft202012Validator.check_schema(self.schema)
        if self.output_schema:Draft202012Validator.check_schema(self.output_schema)
        if self.execution not in ('trusted_service','service_with_sandbox'):raise ValueError('Unknown execution policy')
        if (self.execution=='service_with_sandbox' and self.sandbox_job!='pricing_v1') or (self.execution=='trusted_service' and self.sandbox_job is not None):raise ValueError('Invalid sandbox job policy')
        if self.effect not in ('read', 'draft', 'external_write'):
            raise ValueError('Unknown tool effect')

    def public(self):
        return {k: getattr(self, k) for k in ('name', 'description', 'agents', 'schema', 'effect', 'timeout_seconds', 'available', 'version', 'output_schema', 'execution', 'sandbox_job')}


class Registry:
    def __init__(self, tools):
        self.tools = {}
        for tool in tools:
            if tool.name in self.tools:
                raise ValueError('Duplicate tool name')
            self.tools[tool.name] = tool

    def get(self, name, agent):
        tool = self.tools.get(name)
        if not tool or agent not in tool.agents:
            raise ExecutionError('工具不存在或不属于该 Agent 的权限范围')
        return tool

    def public(self):
        return [t.public() for t in self.tools.values()]
