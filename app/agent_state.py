"""Atomic Agent state writes, explicit transitions and immutable attempt identities."""
import json
import time
from uuid import uuid4
from sqlalchemy import update
from app import store
from app.tool_registry import ExecutionError

TRANSITIONS = {
    'draft':{'queued','draft','waiting_input','cancelled'},
    'queued':{'running','cancelled'},
    'running':{'running','completed','failed','blocked','waiting_input','waiting_approval','budget_exceeded','cancelled'},
    'waiting_input':{'draft','waiting_input','queued','cancelled'},
    'waiting_approval':{'queued','rejected','draft','cancelled'},
    'failed':{'queued','draft','waiting_input','cancelled'},
    'blocked':{'queued','draft','waiting_input','cancelled'},
    'budget_exceeded':{'draft','waiting_input','cancelled'},
    'rejected':{'draft','waiting_input','cancelled'},
    'completed':{'draft','waiting_input'},
    'cancelled':set(),
}


STEP_TRANSITIONS = {
    'pending':{'running','cancelled'},
    'running':{'completed','failed','waiting_input','blocked','waiting_approval','cancelled'},
    'failed':{'pending','cancelled'},
    'waiting_input':{'pending','cancelled'},
    'blocked':{'pending','cancelled'},
    'waiting_approval':{'pending','cancelled'},
    'completed':set(), 'cancelled':set(),
}


def initialize(run):
    run.setdefault('runtime_schema', 2)
    run.setdefault('state_version', 1)
    for state in run['steps'].values():
        state.setdefault('state_version', 1)
        state.setdefault('execution_id', str(uuid4()))
        state.setdefault('attempt_id', None)
    return run


def step_change(state, **fields):
    state.update(fields)
    state['state_version'] = state.get('state_version', 0)+1


def check_edit(run, expected_version=None):
    if run.get('archived'):
        raise ExecutionError('任务已归档，仅可查看摘要；请创建新任务')
    if expected_version is not None and run['state_version'] != expected_version:
        raise ExecutionError('任务状态版本已变化，请刷新后再操作')


def save(conn, run, workspace, *, bump=True):
    from app import agent_artifacts
    current = store.get(run['id'], 'operation_run', workspace, conn)
    if not current:
        raise ExecutionError('任务不存在')
    expected = current.get('state_version', 1)
    if run.get('state_version', 1) != expected:
        raise ExecutionError('任务状态版本冲突')
    old, new = current['status'], run['status']
    if old != new and new not in TRANSITIONS.get(old, set()):
        raise ExecutionError(f'不允许的任务状态转换：{old} → {new}')
    if new in ('completed','rejected','cancelled'):
        run.setdefault('finished_at',time.time() if old!=new else current.get('finished_at') or current.get('updated_at') or current['created_at'])
    elif old in ('completed','rejected','cancelled'):
        run.pop('finished_at',None)
    initialize(run)
    for key,state in run['steps'].items():
        old_step=current['steps'].get(key)
        if old_step and old_step['status']!=state['status']:
            changed_plan=run['revision']!=current['revision'] and state.get('execution_id')!=old_step.get('execution_id')
            if not changed_plan and state['status'] not in STEP_TRANSITIONS.get(old_step['status'],set()):
                raise ExecutionError('不允许的步骤状态转换')
    for state in run['steps'].values():
        if state.get('result'):
            state['result'] = agent_artifacts.pack(conn, workspace, state['result'])
    previous_ids={event.get('id') for event in current.get('events',[]) if event.get('id')}
    for event in run.get('events',[]):
        if event.get('id') and event['id'] not in previous_ids:
            store.put(conn,'agent_event', {**event,'run_id':run['id'],'plan_revision':run['revision'],'state_version':expected+int(bump)},workspace)
    run['state_version'] = expected+int(bump)
    if bump:run['updated_at'] = time.time()
    previous = json.dumps(current, ensure_ascii=False)
    changed = conn.execute(update(store.records).where(
        store.records.c.kind=='operation_run', store.records.c.shop==workspace,
        store.records.c.id==run['id'], store.records.c.payload==previous
    ).values(payload=json.dumps(run, ensure_ascii=False)))
    if changed.rowcount != 1:
        raise ExecutionError('任务被其他执行者更新，拒绝覆盖')
    return run
