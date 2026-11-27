"""Preview-first retention and bounded migration for Agent-owned records only."""
import hashlib
import json
import os
import time
from uuid import uuid4
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select, delete
from app import store, agent_artifacts, agent_state, report_retention, sandbox_files
from app.tool_registry import ExecutionError

DAY = 86400
CLOSED = {'completed','rejected','cancelled'}
KINDS = ('operation_run','tool_trace','agent_call','agent_artifact','agent_event','content_draft','listing_draft','memory_candidate','agent_cleanup_plan')


class Policy(BaseModel):
    model_config = ConfigDict(extra='forbid')
    trace_days: int = Field(default=14, ge=1, le=3650)
    result_days: int = Field(default=30, ge=1, le=3650)
    summary_days: int = Field(default=180, ge=1, le=3650)
    grace_days: int = Field(default=7, ge=1, le=90)
    report_days: int = Field(default=30, ge=1, le=3650)


def scan(kind, workspace, conn=None):
    if conn is None:
        with store.db() as opened:yield from scan(kind,workspace,opened)
        return
    after = ''
    while True:
        rows = conn.execute(select(store.records.c.id,store.records.c.payload).where(
            store.records.c.kind==kind,store.records.c.shop==workspace,store.records.c.id>after
        ).order_by(store.records.c.id).limit(20)).all()
        if not rows:break
        for key,payload in rows:yield json.loads(payload)
        after=rows[-1][0]


def fingerprint(item):
    return hashlib.sha256(agent_artifacts.encoded(item).encode()).hexdigest()


def refs(value):
    if isinstance(value,dict):
        if isinstance(value.get('artifact_ref'),dict):yield value['artifact_ref']['id']
        for k,v in value.items():
            if k!='artifact_ref':yield from refs(v)
    elif isinstance(value,list):
        for v in value:yield from refs(v)


def inventory(workspace, policy, now, conn):
    runs={r['id']:r for r in scan('operation_run',workspace,conn)}
    protected=set();recent=set()
    def ended(run):return run.get('finished_at') or run.get('updated_at') or run['created_at']
    for run in runs.values():
        if run['status'] not in CLOSED or run.get('requires_reconciliation'):
            protected.update(refs(run))
        elif now-ended(run)<policy.result_days*DAY:
            recent.update(refs(run))
    contents={d['id']:d for d in scan('content_draft',workspace,conn)}
    for draft in contents.values():
        if not draft.get('retired') or draft.get('approval_status')=='approved':protected.update(refs(draft))
    for draft in scan('listing_draft',workspace,conn):
        if not draft.get('retired') or draft.get('approval_status')=='approved':
            protected.update(refs(draft))
            protected.update(refs(contents.get(draft.get('content_draft_id'),{})))
    for kind in ('tool_trace','agent_call'):
        for record in scan(kind,workspace,conn):
            run=runs.get(record.get('run_id'))
            if record.get('effect')=='external_write':protected.update(refs(record))
            if run and run['status'] not in CLOSED:protected.update(refs(record))
    return runs,protected|recent,ended


def candidates(workspace,policy,now,conn):
    runs,protected,ended=inventory(workspace,policy,now,conn)
    report_ids,blocked_report_kinds=report_retention.protected(workspace,runs,conn)
    protected_files=sandbox_files.protected(workspace,runs,protected,conn)
    for kind in (*KINDS,*report_retention.KINDS,sandbox_files.KIND,sandbox_files.EVENT):
        for item in scan(kind,workspace,conn):
            action=None;reason=None
            if kind==sandbox_files.KIND:
                if item['state'] in ('expired','discarded') or protected_files is None or item['id'] in protected_files:continue
                days=1 if item['state']=='staged' else policy.result_days
                if now-item['created_at']<days*DAY:continue
                if not item.get('cleanup_due_at'):action='mark_record'
                elif now>=item['cleanup_due_at']:action='expire_file'
                reason='文件产物按期限进入缓冲期，活动依赖与保留标记受到保护'
            elif kind==sandbox_files.EVENT:
                if now-item['at']<policy.summary_days*DAY:continue
                source=runs.get(item.get('run_id'))
                if source and source['status'] not in CLOSED:continue
                if not item.get('cleanup_due_at'):action='mark_record'
                elif now>=item['cleanup_due_at']:action='delete'
                reason='过期文件生命周期审计，先标记再回收'
            elif kind in report_retention.KINDS:
                if item.get('expired') or item.get('pinned') or (kind,item['id']) in report_ids or kind in blocked_report_kinds:continue
                created=item.get('created_at')
                if not isinstance(created,(int,float)) or now-created<policy.report_days*DAY:continue
                if not item.get('cleanup_due_at'):action='mark_record'
                elif now>=item['cleanup_due_at']:action='expire_report'
                reason='过期分析报告进入缓冲期，收藏及未完成任务引用受到保护'
            elif kind=='agent_artifact':
                if item.get('expired') or item['id'] in protected:continue
                if now-item['created_at']<policy.result_days*DAY:continue
                if not item.get('cleanup_due_at'):
                    action='mark';reason='无有效引用的过期结果，进入缓冲期'
                elif now>=item['cleanup_due_at']:
                    action='expire';reason='缓冲期结束，回收结果正文并保留校验元信息'
            elif kind=='operation_run':
                if item['status'] not in CLOSED or item.get('requires_reconciliation') or item.get('archived'):continue
                if now-ended(item)>=policy.summary_days*DAY:
                    action='archive';reason='已结束任务保留概要，关闭原任务重执行入口'
            elif kind in ('tool_trace','agent_call','agent_event'):
                run=runs.get(item.get('run_id'))
                if not run or run['status'] not in CLOSED or run.get('requires_reconciliation') or item.get('effect')=='external_write':continue
                days=policy.trace_days if kind=='tool_trace' else policy.summary_days if kind=='agent_event' else policy.result_days
                if now-ended(run)>=days*DAY:
                    if kind=='agent_call' and item.get('status')=='expired':continue
                    if not item.get('cleanup_due_at'):action='mark_record'
                    elif now>=item['cleanup_due_at']:action='expire_call' if kind=='agent_call' else 'delete'
                    reason='已结束任务的过期执行详情，先进入缓冲期再回收'
            elif kind=='memory_candidate':
                if item['status'] not in ('accepted','rejected'):continue
                if now-item.get('reviewed_at',now)>=policy.summary_days*DAY:
                    if not item.get('cleanup_due_at'):action='mark_record'
                    elif now>=item['cleanup_due_at']:action='delete'
                    reason='已处理的过期记忆候选，长期偏好保持独立'
            elif kind=='agent_cleanup_plan':
                if not item.get('compacted') and item.get('actions') and now-item['created_at']>=policy.trace_days*DAY and item['expires_at']<now:
                    if not item.get('cleanup_due_at'):action='mark_record'
                    elif now>=item['cleanup_due_at']:action='compact_plan'
                    reason='过期清理预览保留确认凭据，回收候选明细'
            if action:
                yield {'kind':kind,'id':item['id'],'action':action,'fingerprint':fingerprint(item),
                       'bytes':len(agent_artifacts.encoded(item).encode()),'reason':reason}


def preview(workspace,policy=None):
    policy=policy or Policy();now=time.time()
    with store.db(write=True) as conn:
        actions=list(candidates(workspace,policy,now,conn))
        report={'id':str(uuid4()),'status':'preview','created_at':now,'expires_at':now+3600,
                'policy':policy.model_dump(),'actions':actions,
                'estimated_reclaim_bytes':sum(a['bytes'] for a in actions if a['action'] in ('expire','delete','expire_call','expire_report','expire_file'))}
        report['confirmation']=fingerprint({'id':report['id'],'policy':report['policy'],'actions':actions})
        store.put(conn,'agent_cleanup_plan',report,workspace)
    return report


def apply(workspace,preview_id,confirmation):
    # SQLite's write transaction serializes reference creation and collection.
    # PostgreSQL cleanup remains disabled until its concurrent-GC tests are run.
    if not store.database_url().startswith('sqlite'):
        raise ExecutionError('当前清理执行仅支持已验证的SQLite部署，PostgreSQL可先使用预览')
    now=time.time()
    with store.db(write=True) as conn:
        report=store.get(preview_id,'agent_cleanup_plan',workspace,conn)
        if not report or report.get('confirmation')!=confirmation:raise ExecutionError('清理预览或确认信息无效')
        if report['status']=='applied':return report
        if report['expires_at']<now:raise ExecutionError('清理预览已过期，请重新预览')
        policy=Policy(**report['policy'])
        valid={(a['kind'],a['id'],a['action']):a for a in candidates(workspace,policy,now,conn)}
        for action in report['actions']:
            latest=valid.get((action['kind'],action['id'],action['action']))
            if not latest or latest['fingerprint']!=action['fingerprint']:
                raise ExecutionError('记录或引用已变化，未执行清理，请重新预览')
        for action in report['actions']:
            kind,key=action['kind'],action['id'];item=store.get(key,kind,workspace,conn)
            if action['action']=='delete':
                conn.execute(delete(store.records).where(store.records.c.kind==kind,store.records.c.shop==workspace,store.records.c.id==key))
                continue
            if action['action'] in ('mark','mark_record'):item['cleanup_due_at']=now+policy.grace_days*DAY
            elif action['action']=='expire':
                item.pop('payload',None);item.update(expired=True,expired_at=now)
            elif action['action']=='expire_call':
                item.pop('result',None);item.update(status='expired',expired_at=now)
            elif action['action']=='expire_file':
                item=sandbox_files.expire(conn,workspace,item)
            elif action['action']=='expire_report':
                item=report_retention.tombstone(item,kind,now)
            elif action['action']=='compact_plan':
                item.pop('actions',None);item.update(compacted=True,compacted_at=now)
            elif action['action']=='archive':
                item=agent_state.initialize(item);item.update(archived=True,archived_at=now,lease_owner=None,lease_until=0)
                item['question']=item.get('question','')[:500]
                item['events']=[]
                if item.get('final_delivery'):item['final_delivery']['answer']=item['final_delivery'].get('answer','')[:2000]
                for step in item['steps'].values():
                    result=step.get('result',{})
                    step['result']={k:v[:500] if isinstance(v,str) else v for k,v in result.items() if k in ('summary','agent_summary','artifact_ref','data_verified','status')}
                agent_state.save(conn,item,workspace)
                continue
            store.put(conn,kind,item,workspace)
        report.update(status='applied',applied_at=now)
        store.put(conn,'agent_cleanup_plan',report,workspace)
    sandbox_files.purge(workspace)
    sandbox_files.sweep_orphans(workspace)
    return report


def migrate(workspace,*,apply_changes=False):
    counts={'runs':0,'traces':0,'drafts':0,'removed_inline_bytes':0}
    def size(conn):
        kinds=[*KINDS,'agent_artifact_head']
        return sum(len(payload.encode()) for payload, in conn.execute(select(store.records.c.payload).where(store.records.c.shop==workspace,store.records.c.kind.in_(kinds))))
    with store.db(write=apply_changes) as conn:
        counts['agent_bytes_before']=size(conn)
        for kind in ('operation_run','tool_trace','content_draft','listing_draft'):
            for item in scan(kind,workspace,conn):
                before=agent_artifacts.encoded(item)
                if kind=='operation_run':
                    if item['status'] in ('running','queued'):continue
                    changed=item.get('runtime_schema',1)<2 or any('data' in s.get('result',{}) or 'evidence' in s.get('result',{}) for s in item['steps'].values())
                    if not changed:continue
                    counts['runs']+=1
                    if apply_changes:agent_state.save(conn,agent_state.initialize(item),workspace)
                elif kind=='tool_trace':
                    if 'data' not in item.get('result',{}) and 'evidence' not in item.get('result',{}):continue
                    counts['traces']+=1
                    if apply_changes:
                        item['result']=agent_artifacts.pack(conn,workspace,item['result']);store.put(conn,kind,item,workspace)
                else:
                    if item.get('artifact_ref'):continue
                    counts['drafts']+=1
                    if apply_changes:agent_artifacts.save_draft(conn,kind,item,workspace)
                if apply_changes:
                    after=store.get(item['id'],kind,workspace,conn)
                    counts['removed_inline_bytes']+=max(0,len(before.encode())-len(agent_artifacts.encoded(after).encode()))
        counts['agent_bytes_after']=size(conn)
        counts['logical_bytes_saved']=max(0,counts['agent_bytes_before']-counts['agent_bytes_after'])
    return counts


def tick():
    if os.getenv('AGENT_CLEANUP_ENABLED','0')!='1':return False
    now=time.time()
    with store.db(write=True) as conn:
        schedule=store.get('retention','agent_maintenance', 'system',conn) or {'id':'retention','next_at':0}
        if schedule['next_at']>now:return False
        schedule['next_at']=now+DAY;store.put(conn,'agent_maintenance',schedule,'system')
        workspaces=conn.execute(select(store.records.c.shop).where(store.records.c.kind.in_(('operation_run',*report_retention.KINDS,sandbox_files.KIND,sandbox_files.EVENT))).distinct()).scalars().all()
    policy=Policy(trace_days=int(os.getenv('AGENT_TRACE_RETENTION_DAYS','14')),result_days=int(os.getenv('AGENT_RESULT_RETENTION_DAYS','30')),summary_days=int(os.getenv('AGENT_SUMMARY_RETENTION_DAYS','180')),grace_days=int(os.getenv('AGENT_CLEANUP_GRACE_DAYS','7')),report_days=int(os.getenv('AGENT_REPORT_RETENTION_DAYS','30')))
    for workspace in workspaces:
        report=preview(workspace,policy)
        apply(workspace,report['id'],report['confirmation'])
    return True
