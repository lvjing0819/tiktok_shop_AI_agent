import base64
import copy
import hashlib
import json
import os
from pathlib import Path
import runpy
import time
from uuid import uuid4
import pytest
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient
from app import sandbox_files as files,store,agent_artifacts as artifacts,agent_maintenance as maintenance
from app.main import app
from app.tool_registry import ExecutionError

PRICE={'proposed_price':'21.67','proposed_channel_quantity':8}
CTX={'workspace':'default','run_id':'run','step_id':'a','attempt_id':'attempt','sandbox_id':'sandbox'}


@pytest.fixture(autouse=True)
def env(tmp_path,monkeypatch):
    monkeypatch.delenv('DATABASE_URL',raising=False);monkeypatch.delenv('APP_TOKEN',raising=False)
    monkeypatch.setenv('DATABASE_PATH',str(tmp_path/'files.sqlite'))
    monkeypatch.setenv('SANDBOX_ARTIFACT_DIR',str(tmp_path/'files'))
    monkeypatch.setenv('SANDBOX_ARTIFACT_KEY',Fernet.generate_key().decode())
    return TestClient(app)


def entry(data=PRICE,**changes):
    raw=json.dumps(data,separators=(',',':')).encode()
    return {'path':'pricing.json','purpose':'output','sensitivity':'restricted','media_type':'application/json',
            'size':len(raw),'sha256':hashlib.sha256(raw).hexdigest(),'content_b64':base64.b64encode(raw).decode(),**changes}


def bundle(data=PRICE):
    return [entry(data),entry({},path='intermediate.json',purpose='temporary',content_b64=None)]


def staged():return files.stage(CTX,'pricing_export_v1',bundle())


def available():
    refs=staged()
    with store.db(write=True) as conn:files.publish(conn,CTX,refs,PRICE)
    return refs


def blob(key):return Path(os.environ['SANDBOX_ARTIFACT_DIR'])/hashlib.sha256(b'default').hexdigest()/(key+'.bin')


def test_classification_encryption_and_audited_delivery(env):
    refs=staged();assert len(refs)==1
    key=refs[0]['file_id'];metadata=files.metadata(key,'default')
    assert metadata['purpose']=='output' and metadata['sensitivity']=='restricted' and metadata['state']=='staged'
    assert b'proposed_price' not in blob(key).read_bytes()
    assert blob(key).stat().st_mode&0o777==0o600
    assert env.get('/api/agent-files/'+key+'/download').status_code==404
    with store.db(write=True) as conn:files.publish(conn,CTX,refs,PRICE)
    response=env.get('/api/agent-files/'+key+'/download')
    assert response.status_code==200 and response.json()==PRICE
    assert response.headers['x-content-type-options']=='nosniff'
    assert 'attachment' in response.headers['content-disposition'] and response.headers['cache-control']=='no-store'
    events=store.list_records(files.EVENT)
    assert {'validated','temporary_discarded','staged','published','read'} <= {event['action'] for event in events}
    assert 'proposed_price' not in json.dumps(events)


@pytest.mark.parametrize('change',[
 {'path':'../pricing.json'},{'path':'/etc/passwd'},{'path':'folder/pricing.json'},
 {'sensitivity':'public'},{'purpose':'temporary'},{'media_type':'text/html'},
 {'size':9000},{'size':1},{'sha256':'0'*64},{'content_b64':'!!invalid!!'},
])
def test_invalid_manifest_never_persists_body(change):
    value=bundle();value[0].update(change)
    with pytest.raises(files.FileError):files.stage(CTX,'pricing_export_v1',value)
    assert not store.list_records(files.KIND)
    assert not list(Path(os.environ['SANDBOX_ARTIFACT_DIR']).rglob('*.bin'))
    assert store.list_records(files.EVENT)[0]['action']=='rejected'


@pytest.mark.parametrize('value',[
 {'access_token':'sensitive-value'}, {'proposed_price':'<script>bad</script>','proposed_channel_quantity':8},
 {'proposed_price':'10.00','proposed_channel_quantity':8,'extra':'unexpected'},
])
def test_sensitive_or_active_content_rejected_without_body_logging(value):
    with pytest.raises(files.FileError):files.stage(CTX,'pricing_export_v1',bundle(value))
    assert 'sensitive-value' not in json.dumps(store.list_records(files.EVENT))
    assert not store.list_records(files.KIND)


def test_known_credentials_and_missing_encryption_key_fail_closed(monkeypatch):
    monkeypatch.setenv('SOME_API_KEY','confidential-credential')
    with pytest.raises(files.FileError):files.scan_content(json.dumps({'value':'confidential-credential'}).encode())
    monkeypatch.delenv('SANDBOX_ARTIFACT_KEY');monkeypatch.delenv('ADS_TOKEN_ENCRYPTION_KEY',raising=False)
    with pytest.raises(files.FileError) as error:staged()
    assert error.value.code=='FILE_KEY_UNAVAILABLE' and not store.list_records(files.KIND)


def test_exact_content_binding_and_failed_staging_cleanup():
    refs=staged();key=refs[0]['file_id']
    with pytest.raises(files.FileError):
        with store.db(write=True) as conn:files.publish(conn,CTX,refs,{**PRICE,'proposed_price':'999.00'})
    assert files.metadata(key,'default')['state']=='staged'
    files.discard(CTX,refs)
    assert not blob(key).exists() and files.metadata(key,'default')['state']=='discarded'


def test_ciphertext_corruption_and_file_swapping_are_detected():
    first=available()[0]['file_id'];second=available()[0]['file_id']
    original=blob(first).read_bytes();blob(first).write_bytes(blob(second).read_bytes())
    with pytest.raises(files.FileError):files.download(first,'default')
    blob(first).write_bytes(original[:-4]+b'bad!')
    with pytest.raises(files.FileError):files.download(first,'default')


def test_workspace_and_owner_auth_enforced(env,monkeypatch):
    key=available()[0]['file_id'];path='/api/agent-files/'+key
    assert env.get(path,headers={'X-Shop-ID':'other'}).status_code==404
    assert env.get(path+'/download',headers={'X-Shop-ID':'other'}).status_code==404
    monkeypatch.setenv('APP_TOKEN','owner')
    assert env.get(path+'/download').status_code==401
    assert env.get(path+'/download',headers={'Authorization':'Bearer owner'}).status_code==200


def execution(key,*,agent='content',dependencies=('a',),attempt='attempt',run_id='run'):
    return {'id':run_id,'plan':{'steps':[{'id':'b','agent':agent,'depends_on':list(dependencies)}]},
            'steps':{'a':{'status':'completed','attempt_id':attempt}}}


def test_agents_only_read_completed_dependencies_in_same_attempt():
    key=available()[0]['file_id']
    scope={**CTX,'step_id':'b','assert_current':lambda conn:execution(key)}
    assert files.read_for_agent(scope,key)['pricing']==PRICE
    for changes in ({'dependencies':()},{'agent':'ads'},{'attempt':'new-attempt'}):
        denied={**scope,'assert_current':lambda conn,changes=changes:execution(key,**changes)}
        with pytest.raises(files.FileError):files.read_for_agent(denied,key)
    other={**scope,'run_id':'another'}
    with pytest.raises(files.FileError):files.read_for_agent(other,key)
    assert any(event['action']=='access_denied' for event in store.list_records(files.EVENT))


def age(key):
    item=files.metadata(key,'default');item['created_at']=time.time()-40*86400;store.save(item,files.KIND)


def apply(plan):return maintenance.apply('default',plan['id'],plan['confirmation'])


def test_retention_grace_pinning_expiry_and_physical_cleanup(env):
    key=available()[0]['file_id'];age(key)
    plan=maintenance.preview('default');apply(plan)
    assert blob(key).exists()
    item=files.metadata(key,'default');item['cleanup_due_at']=time.time()-1;store.save(item,files.KIND)
    due=maintenance.preview('default')
    pinned=files.pin(key,'default',item['revision'],True)
    with pytest.raises(ExecutionError):apply(due)
    assert not any(a['kind']==files.KIND for a in maintenance.preview('default')['actions'])
    files.pin(key,'default',pinned['revision'],False)
    apply(maintenance.preview('default'))
    item=files.metadata(key,'default');item['cleanup_due_at']=0.1;store.save(item,files.KIND)
    apply(maintenance.preview('default'))
    assert not blob(key).exists() and files.metadata(key,'default')['body_removed_at']
    assert env.get('/api/agent-files/'+key+'/download').status_code==410


def test_active_tasks_and_current_draft_references_protect_files():
    refs=available();key=refs[0]['file_id'];age(key)
    store.save({'id':'run','status':'waiting_input','created_at':time.time(),'steps':{}},'operation_run')
    assert not any(a['kind']==files.KIND for a in maintenance.preview('default')['actions'])
    store.remove('run','operation_run','default')
    with store.db(write=True) as conn:ref=artifacts.put(conn,'default',{'data':{'file_refs':refs},'evidence':[]})
    store.save({'id':'draft','artifact_ref':ref},'listing_draft')
    assert not any(a['kind']==files.KIND for a in maintenance.preview('default')['actions'])
    store.remove('draft','listing_draft','default')
    assert any(a['kind']==files.KIND for a in maintenance.preview('default')['actions'])


def test_new_reference_after_preview_aborts_cleanup():
    refs=available();key=refs[0]['file_id'];age(key)
    plan=maintenance.preview('default')
    with store.db(write=True) as conn:ref=artifacts.put(conn,'default',{'data':{'file_refs':refs}})
    store.save({'id':'draft','artifact_ref':ref},'content_draft')
    with pytest.raises(ExecutionError):apply(plan)
    assert blob(key).exists()


def test_worker_rejects_symlinks_hardlinks_and_directories(tmp_path):
    worker=runpy.run_path('sandbox/worker.py');folder=tmp_path/'output';folder.mkdir()
    victim=tmp_path/'outside';victim.write_text('private')
    target=folder/'pricing.json';target.symlink_to(victim)
    with pytest.raises(OSError):worker['package_file'](folder,'pricing.json','output')
    target.unlink();os.link(victim,target)
    with pytest.raises(ValueError):worker['package_file'](folder,'pricing.json','output')
    target.unlink();target.mkdir()
    with pytest.raises((ValueError,OSError)):worker['package_file'](folder,'pricing.json','output')


def test_worker_fixed_export_creates_files_and_suppresses_temporary_body(tmp_path):
    worker=runpy.run_path('sandbox/worker.py');folder=tmp_path/'output'
    data={'unit_cost':'10','logistics':'2','fixed_fees':'1','fee_rate':'0.1','target_margin':'0.3','available':23,'listing_cap':8}
    assert worker['pricing_export'](data,folder)==PRICE
    value=[worker['package_file'](folder,'pricing.json','output'),worker['package_file'](folder,'intermediate.json','temporary')]
    assert value[1]['content_b64'] is None
    assert len(files.stage(CTX,'pricing_export_v1',value))==1


def test_storage_directory_symlink_is_refused_and_orphans_are_scoped(tmp_path,monkeypatch):
    outside=tmp_path/'outside';outside.mkdir();link=tmp_path/'symlink';link.symlink_to(outside)
    monkeypatch.setenv('SANDBOX_ARTIFACT_DIR',str(link))
    with pytest.raises(files.FileError):staged()
    assert not list(outside.iterdir())
    monkeypatch.setenv('SANDBOX_ARTIFACT_DIR',str(tmp_path/'private'))
    key=available()[0]['file_id'];orphan=blob(str(uuid4()));orphan.write_bytes(b'encrypted-orphan')
    os.utime(orphan,(time.time()-9*86400,)*2)
    unrelated=orphan.parent/'keep.txt';unrelated.write_text('Keep')
    files.sweep_orphans('default')
    assert not orphan.exists() and unrelated.exists() and blob(key).exists()


def test_existing_ciphertext_is_never_overwritten_or_deleted_on_name_collision():
    key=available()[0]['file_id'];original=blob(key).read_bytes()
    with pytest.raises(FileExistsError):files._write('default',key,b'new')
    assert blob(key).read_bytes()==original


def test_file_management_api_versions_and_task_audit(env):
    from app import harness
    task=harness.create('default','test',harness.Plan(title='test',steps=[],clarification='Need product'))
    context={**CTX,'run_id':task['id']}
    refs=files.stage(context,'pricing_export_v1',bundle());key=refs[0]['file_id']
    with store.db(write=True) as conn:files.publish(conn,context,refs,PRICE)
    metadata=env.get('/api/agent-files/'+key).json()
    path='/api/agent-files/'+key+'/retention'
    assert env.put(path,json={'revision':metadata['revision'],'pinned':True}).status_code==200
    assert env.put(path,json={'revision':metadata['revision'],'pinned':False}).status_code==409
    assert env.put(path,json={'revision':1,'pinned':True},headers={'X-Shop-ID':'other'}).status_code==404
    audit=env.get('/api/operations/'+task['id']+'/file-events')
    assert audit.status_code==200 and any(e['action']=='temporary_discarded' for e in audit.json())
    assert env.get('/api/operations/'+task['id']+'/file-events',headers={'X-Shop-ID':'other'}).status_code==404


def test_operator_interrupt_rolls_back_staging_without_turning_it_into_a_file_error(monkeypatch):
    def interrupt(*args):raise KeyboardInterrupt()
    monkeypatch.setattr(files,'_write',interrupt)
    with pytest.raises(KeyboardInterrupt):staged()
    assert not store.list_records(files.KIND)
