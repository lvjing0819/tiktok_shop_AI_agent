"""Analysis reports are execution outputs; conversations and memory are untouched."""
import time
from app import store, agent_artifacts
from app.tool_registry import ExecutionError

KINDS = ('ads_report', 'social_report')
TOOLS = {'ads.analyze':'ads_report', 'social.analyze':'social_report'}


class ReportExpired(ExecutionError):
    pass


def metadata(item, kind):
    return {**{k:v for k,v in item.items() if k not in ('answer','evidence')},
            'report_type':kind, 'retention_revision':item.get('retention_revision',1),
            'pinned':bool(item.get('pinned')), 'expired':bool(item.get('expired'))}


def get(key, kind, workspace):
    item=store.get(key,kind,workspace)
    if not item:raise LookupError('分析报告不存在')
    if item.get('expired'):raise ReportExpired('分析报告已过期，正文和证据已回收；请重新查询获取数据')
    return {**item,'retention_revision':item.get('retention_revision',1),'pinned':bool(item.get('pinned'))}


def pin(key, kind, workspace, revision, pinned):
    if kind not in KINDS:raise ValueError('不支持的报告类型')
    with store.db(write=True) as conn:
        item=store.get(key,kind,workspace,conn)
        if not item:raise LookupError('分析报告不存在')
        if item.get('expired'):raise ReportExpired('报告已过期，无法通过收藏恢复正文')
        if item.get('retention_revision',1)!=revision:raise ExecutionError('报告保留设置已变化，请刷新后操作')
        item.update(pinned=pinned,retention_revision=revision+1,retention_updated_at=time.time())
        item.pop('cleanup_due_at',None)  # Removing a bookmark starts a fresh grace period.
        store.put(conn,kind,item,workspace)
        return metadata(item,kind)


def tombstone(item, kind, now):
    return {'id':item['id'],'report_type':kind,'created_at':item['created_at'],
            'expired':True,'expired_at':now,'pinned':False,
            'retention_revision':item.get('retention_revision',1)+1}


def protected(workspace, runs, conn):
    """Inspect persisted task deliveries/receipts, without reading chat history."""
    from app.agent_maintenance import scan, CLOSED
    active={key:run for key,run in runs.items() if run['status'] not in CLOSED or run.get('requires_reconciliation')}
    ids=set();blocked=set();resolved=set();specs={}
    def inspect(run_id,step_id,result):
        kind=specs.get((run_id,step_id))
        if not kind or not result:return
        try:data=agent_artifacts.load(result,workspace,conn).get('data',{})
        except ExecutionError:
            blocked.add(kind);return
        if isinstance(data,dict) and isinstance(data.get('id'),str):
            ids.add((kind,data['id']));resolved.add((run_id,step_id))
    for key,run in active.items():
        for step in run.get('plan',{}).get('steps',[]):
            kind=TOOLS.get(step['tool'])
            if kind:specs[(key,step['id'])]=kind
        for step_id,state in run['steps'].items():inspect(key,step_id,state.get('result'))
    if specs:
        for record_kind in ('agent_call','tool_trace'):
            for record in scan(record_kind,workspace,conn):
                if record.get('run_id') in active:inspect(record['run_id'],record.get('step_id'),record.get('result'))
        for (key,step_id),kind in specs.items():
            if (key,step_id) not in resolved and active[key]['steps'].get(step_id,{}).get('status') in ('running','failed','completed'):
                blocked.add(kind)  # Unknown/inconsistent evidence fails closed.
    return ids,blocked
