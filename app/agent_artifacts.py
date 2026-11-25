"""Workspace-scoped immutable result bodies; tasks and traces carry references."""
import hashlib
import json
import time
from uuid import uuid4
from app import store
from app.tool_registry import ExecutionError

MAX_BYTES = 2_000_000


def encoded(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'))


def put(conn, workspace, payload):
    from app.harness import redact
    payload = redact(payload)
    raw = encoded(payload).encode()
    if len(raw) > MAX_BYTES:
        raise ExecutionError('工具结果超出可保存大小')
    checksum = hashlib.sha256(raw).hexdigest()
    key = hashlib.sha256((workspace+':'+checksum).encode()).hexdigest()
    head_key=key
    head=store.get(head_key,'agent_artifact_head',workspace,conn)
    if head:key=head['artifact_id']
    item = store.get(key, 'agent_artifact', workspace, conn)
    if item and item.get('expired'):
        # A new task producing the same bytes gets a new lifetime; old expired
        # references must never become valid again.
        key = hashlib.sha256((key+':'+str(time.time_ns())).encode()).hexdigest()
        item = None
    if not item:
        store.put(conn, 'agent_artifact', {'id':key, 'version':1, 'checksum':checksum,
                  'created_at':time.time(), 'bytes':len(raw), 'payload':payload}, workspace)
    store.put(conn,'agent_artifact_head',{'id':head_key,'artifact_id':key},workspace)
    return {'id':key, 'version':1, 'checksum':checksum}


def read(ref, workspace, conn=None):
    item = store.get(ref['id'], 'agent_artifact', workspace, conn)
    if not item or item.get('expired') or 'payload' not in item:
        raise ExecutionError('完整结果已过期或不存在，请创建新任务重新查询')
    if item['version'] != ref.get('version') or item['checksum'] != ref.get('checksum'):
        raise ExecutionError('交付物版本或校验信息不一致')
    if hashlib.sha256(encoded(item['payload']).encode()).hexdigest() != item['checksum']:
        raise ExecutionError('交付物校验失败')
    return item['payload']


def pack(conn, workspace, result):
    if 'data' not in result and 'evidence' not in result:
        return result
    ref = put(conn, workspace, {'data':result.get('data', {}), 'evidence':result.get('evidence', [])})
    return {**{k:v for k,v in result.items() if k not in ('data','evidence')}, 'artifact_ref':ref}


def load(result, workspace, conn=None):
    if not result or not result.get('artifact_ref'):
        return result
    return {**result, **read(result['artifact_ref'], workspace, conn)}


def save_draft(conn, kind, item, workspace):
    ref = put(conn, workspace, {'data':item, 'evidence':[]})
    fields = ('id','revision','product_id','product_revision','market','source_run','status',
              'approval_status','media_status','created_at','content_draft_id','content_draft_revision')
    record = {k:item[k] for k in fields if k in item}
    record['artifact_ref'] = ref
    store.put(conn, kind, record, workspace)
    return item


def get_draft(key, kind, workspace, conn=None):
    item = store.get(key, kind, workspace, conn)
    if item and item.get('retired'):
        raise ExecutionError('该草稿已归档，请创建新任务')
    if item and item.get('artifact_ref'):
        data = read(item['artifact_ref'], workspace, conn)['data']
        if data.get('revision') != item.get('revision'):
            raise ExecutionError('草稿版本已变化')
        return data
    return item


def revise_content(workspace,key,revision,changes):
    from app.harness import redact
    from app.operation_tools import validate_delivery
    if not changes or redact(changes)!=changes:raise ExecutionError('文案变更为空或包含敏感字段')
    with store.db(write=True) as conn:
        data=get_draft(key,'content_draft',workspace,conn)
        if not data:raise LookupError('文案草稿不存在')
        if data['revision']!=revision:raise ExecutionError('草稿版本已变化，请刷新')
        validate_delivery('content.draft',data,workspace,conn)
        previous=store.get(key,'content_draft',workspace,conn).get('artifact_ref')
        data={**data,**changes,'revision':revision+1,'approval_status':'not_reviewed','updated_at':time.time()}
        if previous:data['previous_artifact_ref']=previous
        save_draft(conn,'content_draft',data,workspace)
        store.put(conn,'agent_event',{'id':str(uuid4()),'at':time.time(),'kind':'content_revised',
                  'run_id':data.get('source_run'),'draft_id':key,'revision':data['revision']},workspace)
        return data


def retire_draft(workspace,kind,key,revision):
    from app.agent_maintenance import scan,refs,CLOSED
    with store.db(write=True) as conn:
        draft=store.get(key,kind,workspace,conn)
        if not draft:raise LookupError('草稿不存在')
        if draft['revision']!=revision or draft.get('retired'):raise ExecutionError('草稿已变化或已归档')
        if draft.get('approval_status')=='approved':raise ExecutionError('已审批有效草稿不能直接归档')
        owned=set(refs(draft))
        for run in scan('operation_run',workspace,conn):
            if run['status'] not in CLOSED and owned.intersection(refs(run)):
                raise ExecutionError('草稿仍被未结束任务引用')
        if kind=='content_draft' and any(d.get('content_draft_id')==key and not d.get('retired') for d in scan('listing_draft',workspace,conn)):
            raise ExecutionError('草稿仍被有效上架草案引用')
        draft.update(retired=True,retired_at=time.time(),revision=revision+1)
        store.put(conn,kind,draft,workspace)
        return draft
