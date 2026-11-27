import asyncio
import copy
import json
import runpy
import sys
import time
import pytest
from fastapi.testclient import TestClient
from app import sandbox as s,store,operation_tools as tools,business as b,harness as h,model
from app.main import app


@pytest.fixture(autouse=True)
def env(tmp_path,monkeypatch):
    monkeypatch.delenv('DATABASE_URL',raising=False);monkeypatch.delenv('APP_TOKEN',raising=False)
    monkeypatch.setenv('DATABASE_PATH',str(tmp_path/'sandbox.sqlite'))
    return TestClient(app)


def ctx():return {'workspace':'default','run_id':'run','step_id':'price','attempt_id':'attempt'}


def run(coro):return asyncio.run(coro)


@pytest.fixture
def engine(monkeypatch,tmp_path):
    calls=[];state={'container':None,'created':None,'image':'sha256:'+'a'*64,'job':None,'behavior':'ok'}
    def config(args):
        def option(k):return args[args.index(k)+1]
        labels=dict(v.split('=',1) for i,v in enumerate(args) if i and args[i-1]=='--label')
        tmpfs=dict(v.split(':',1) for i,v in enumerate(args) if i and args[i-1]=='--tmpfs')
        return {'Id':'fixed-container-id','Image':state['image'],'Config':{'User':'65532:65532','Env':['PATH=/usr/local/bin'],
            'Entrypoint':['python','-I','-B','/opt/shop-sandbox/worker.py'],'Labels':labels},'State':{'Running':False,'ExitCode':0},'Mounts':[],
            'HostConfig':{'ReadonlyRootfs':True,'Privileged':False,'NetworkMode':'none','Memory':int(option('--memory')),
            'MemorySwap':int(option('--memory-swap')),'NanoCpus':int(float(option('--cpus'))*1e9),'PidsLimit':int(option('--pids-limit')),
            'IpcMode':'private','CgroupnsMode':'private','Binds':None,'PidMode':'','UTSMode':'','CapAdd':None,'CapDrop':['ALL'],
            'SecurityOpt':['no-new-privileges'],'LogConfig':{'Type':'none'},'Tmpfs':tmpfs}}
    async def command(args,**kwargs):
        calls.append((list(args),kwargs))
        if args[0]=='info':return 0,json.dumps({'CgroupVersion':'2','SecurityOptions':['name=seccomp,profile=builtin']}).encode(),b''
        if args[:2]==['image','inspect']:return 0,json.dumps([{'Id':state['image'],'Config':{'Env':['PATH=/usr/local/bin'],'Labels':{'ai.tiktok.shop.sandbox':s.IMAGE_VERSION}}}]).encode(),b''
        if args[0]=='create':
            state['container']=config(args);state['created']=copy.deepcopy(state['container'])
            if state.get('mutate'):state['mutate'](state['container'])
            return 0,b'container-id',b''
        if args[0]=='ps':return 0,(b'fixed-container-id\n' if state['container'] else b''),b''
        if args[0]=='inspect':
            if state.get('inspect_error'):return 1,b'',b'Cannot connect to daemon'
            if state['container'] is None:return 1,b'',b'Error: No such object'
            return 0,json.dumps([state['container']]).encode(),b''
        if args[0]=='start':
            request=json.loads(kwargs['input']);state['job']=request
            state['container']['State']['Running']=True
            if state['behavior']=='timeout':raise TimeoutError()
            if state['behavior']=='wait':await asyncio.sleep(100)
            if state.get('after_start'):state['after_start']()
            worker=runpy.run_path('sandbox/worker.py')
            data=worker['pricing'](request['data'])
            files=None
            if request['job']=='pricing_export_v1':
                folder=tmp_path/('output-'+str(len(calls)))
                data=worker['pricing_export'](request['data'],folder)
                files=[worker['package_file'](folder,'pricing.json','output'),worker['package_file'](folder,'intermediate.json','temporary')]
            state['container']['State']['Running']=False
            state['container']['State']['OOMKilled']=state['behavior']=='oom'
            result={'protocol':1,'job':request['job'],'data':data,'usage':{'cpu_seconds':.01,'peak_rss_kib':8000}}
            if files is not None:result['files']=files
            if state['behavior']=='bad_output':result['host_path']='/etc/passwd'
            return 0,json.dumps(result).encode(),b''
        if args[0] in ('stop','kill'):
            if not state.get('refuse_stop'):state['container']['State']['Running']=False
            return 0,b'',b''
        if args[0]=='rm':
            if not state.get('refuse_stop'):state['container']=None
            return 0,b'',b''
        raise AssertionError(args)
    monkeypatch.setattr(s,'command',command)
    return state,calls


def payload():return {'unit_cost':'10','logistics':'2','fixed_fees':'1','fee_rate':'0.1','target_margin':'0.3','available':23,'listing_cap':8}


def test_fixed_job_contract_is_offline_and_audit_has_no_input_body(engine):
    state,calls=engine
    result=run(s.run_job(ctx(),'pricing_v1',payload()))
    assert result=={'proposed_price':'21.67','proposed_channel_quantity':8}
    create=next(args for args,_ in calls if args[0]=='create')
    assert '--volume' not in create and '--mount' not in create and '--env' not in create
    assert create[-1]==state['image'] and state['container'] is None
    item=store.list_records('sandbox_run')[0]
    assert item['status']=='completed' and item['stopped_confirmed']
    assert item['input_checksum'] and 'unit_cost' not in json.dumps(item)
    assert item['worker_reported_usage']['peak_rss_kib']==8000


@pytest.mark.parametrize('mutate',[
 lambda c:c['HostConfig'].update(NetworkMode='host'),
 lambda c:c['HostConfig'].update(Privileged=True),
 lambda c:c['HostConfig'].update(Memory=0),
 lambda c:c['HostConfig'].update(PidsLimit=0),
 lambda c:c['HostConfig'].update(Binds=['/:/host']),
 lambda c:c['HostConfig'].update(SecurityOpt=['seccomp=unconfined']),
 lambda c:c['Config'].update(User='0'),
 lambda c:c['Config']['Env'].append('HTTP_PROXY=secret-proxy'),
 lambda c:c['HostConfig']['Tmpfs'].update({'/work':'rw,size=999999999999'}),
])
def test_weakened_policy_is_refused_before_start(engine,mutate):
    state,calls=engine;state['mutate']=mutate
    with pytest.raises(s.SandboxError) as error:run(s.run_job(ctx(),'pricing_v1',payload()))
    assert error.value.observation['code']=='POLICY_MISMATCH'
    assert state['container'] is None and not any(args[0]=='start' for args,_ in calls)


@pytest.mark.parametrize('behavior,code',[('timeout','TIMEOUT'),('oom','MEMORY_LIMIT'),('bad_output','INVALID_OUTPUT')])
def test_failure_is_structured_and_container_is_collected(engine,behavior,code):
    state,calls=engine;state['behavior']=behavior
    with pytest.raises(s.SandboxError) as error:run(s.run_job(ctx(),'pricing_v1',payload()))
    assert error.value.observation['code']==code and error.value.observation['stopped_confirmed']
    assert state['container'] is None
    assert store.list_records('sandbox_run')[0]['status']=='failed'


def test_unconfirmed_cleanup_blocks_next_attempt(engine):
    state,calls=engine;state.update(behavior='timeout',refuse_stop=True)
    with pytest.raises(s.SandboxError) as first:run(s.run_job(ctx(),'pricing_v1',payload()))
    assert first.value.observation['code']=='CLEANUP_UNCONFIRMED' and not first.value.observation['stopped_confirmed']
    count=len(calls)
    with pytest.raises(s.SandboxError):run(s.run_job(ctx(),'pricing_v1',payload()))
    assert len(calls)==count


def test_daemon_failure_is_not_mistaken_for_removed_container(engine):
    state,_=engine;state['inspect_error']=True
    with pytest.raises(s.SandboxError) as error:run(s.run_job(ctx(),'pricing_v1',payload()))
    assert error.value.observation['code']=='CLEANUP_UNCONFIRMED'


def test_stale_attempt_stops_inflight_execution(engine):
    state,_=engine;state['behavior']='wait';checks=[]
    def fence(conn):
        checks.append(1)
        if len(checks)>2:raise s.ExecutionError('stale')
    with pytest.raises(s.SandboxError) as error:run(s.run_job({**ctx(),'assert_current':fence},'pricing_v1',payload()))
    assert error.value.observation['code']=='STALE_ATTEMPT' and state['container'] is None


def test_cancellation_also_cleans_container(engine):
    state,_=engine;state['behavior']='wait'
    async def cancel():
        task=asyncio.create_task(s.run_job(ctx(),'pricing_v1',payload()))
        await asyncio.sleep(.05);task.cancel()
        with pytest.raises(s.SandboxError) as error:await task
        assert error.value.observation['code']=='CANCELLED' and error.value.observation['stopped_confirmed']
    run(cancel());assert state['container'] is None


def test_missing_runtime_fails_closed(monkeypatch):
    monkeypatch.setattr(s,'docker',lambda:(_ for _ in ()).throw(s.SandboxError('RUNTIME_UNAVAILABLE','missing')))
    with pytest.raises(s.SandboxError):run(s.run_job(ctx(),'pricing_v1',payload()))
    assert store.list_records('sandbox_run')[0]['status']=='failed'


def test_input_and_job_allowlists_reject_before_creating_container(engine):
    _,calls=engine
    with pytest.raises(s.SandboxError):run(s.run_job(ctx(),'shell',{'command':'anything'}))
    with pytest.raises(s.SandboxError):run(s.run_job(ctx(),'pricing_v1',{'large':'x'*70000}))
    assert not calls


def test_command_output_is_bounded_and_timeout_kills_client(monkeypatch):
    monkeypatch.setattr(s,'docker',lambda:sys.executable)
    async def checks():
        with pytest.raises(s.SandboxError):await s.command(['-c','print("x"*1000000)'],limit=1000,timeout=2)
        with pytest.raises(TimeoutError):await s.command(['-c','import time; time.sleep(10)'],timeout=.05)
    run(checks())


def product_setup():
    p=b.create_goods('default',b.GoodsInput(name='Watch',facts='Confirmed'))
    sku=b.create_sku('default',b.SKUInput(product_id=p['id'],code='w'))
    wh=b.create_warehouse('default',b.WarehouseInput(name='MY',country='MY',kind='overseas'))
    b.set_stock('default',b.StockInput(warehouse_id=wh['id'],sku_id=sku['id'],on_hand=23,expected_revision=0,operation_id='count-001',reason='Confirmed'))
    b.save_pricing('default',sku['id'],'MY',b.PricingInput(unit_cost='10',logistics='2',fixed_fees='1',currency='MYR',fee_rate='0.1',target_margin='0.3',listing_cap=8,expected_revision=0,source_note='Confirmed'))
    return {'product_id':p['id'],'sku_id':sku['id'],'warehouse_id':wh['id'],'market':'MY'}


def test_real_business_handoff_uses_only_minimal_data_and_refuses_stale_inventory(engine):
    state,_=engine;args=product_setup()
    result=run(tools.product_draft({**ctx(),'operation_id':'receipt'},args))
    assert result['data']['proposed_price']=='21.67' and state['job']['data']==payload()
    def change():
        with store.db(write=True) as conn:conn.execute(b.stock.update().values(revision=2))
    state['after_start']=change
    with pytest.raises(s.ExecutionError):run(tools.product_draft({**ctx(),'operation_id':'second'},args))
    assert len(store.list_records('listing_draft'))==1


def test_recovery_only_touches_owned_expired_attempts(engine):
    state,calls=engine;state['behavior']='timeout';state['refuse_stop']=True
    with pytest.raises(s.SandboxError):run(s.run_job(ctx(),'pricing_v1',payload(),policy=s.Policy(timeout=.01)))
    item=store.list_records('sandbox_run')[0];item['deadline_at']=time.time()-1;store.save(item,'sandbox_run')
    state['refuse_stop']=False
    assert run(s.recover_expired())==1 and state['container'] is None
    assert store.get(item['id'],'sandbox_run')['stopped_confirmed']


def test_recovery_does_not_remove_containers_with_different_labels(engine):
    state,_=engine;state.update(behavior='timeout',refuse_stop=True)
    with pytest.raises(s.SandboxError):run(s.run_job(ctx(),'pricing_v1',payload()))
    state['container']['Config']['Labels']['ai.shop.project']='other-project'
    item=store.list_records('sandbox_run')[0];item['deadline_at']=0;store.save(item,'sandbox_run')
    assert run(s.recover_expired())==0 and state['container'] is not None


def test_audit_api_is_scoped_and_authenticated(env,monkeypatch):
    r=h.create('default','test',h.Plan(title='empty',steps=[],clarification='Provide product'))
    s.audit({'workspace':'default','run_id':r['id']},'sandbox',status='completed')
    assert len(env.get(f"/api/operations/{r['id']}/sandboxes").json())==1
    assert env.get(f"/api/operations/{r['id']}/sandboxes",headers={'X-Shop-ID':'other'}).status_code==404
    monkeypatch.setenv('APP_TOKEN','owner')
    assert env.get('/api/sandbox/status').status_code==401


def test_timeout_observation_is_returned_to_agent_and_disallows_automatic_retry(monkeypatch):
    async def timeout(*args,**kwargs):raise s.SandboxError('TIMEOUT','Timed out',stopped=True,sandbox_id='sandbox',elapsed=120)
    monkeypatch.setattr(h,'gateway',timeout)
    seen=[]
    def decide(messages,functions):
        seen.append(messages)
        return {'tool_calls':[{'function':{'name':'handle_sandbox_failure','arguments':json.dumps({'action':'ask','summary':'请确认是否重新执行'})}}]}
    monkeypatch.setattr(model,'message',decide)
    step=h.Step(id='price',agent='product',goal='Price',tool='product.prepare_listing',arguments={},success_criteria='Draft')
    result=run(h.run_role(None,step,{}))
    assert seen and result['status']=='waiting_input'
    assert result['data']['sandbox_observation']['automatic_retry'] is False
    assert result['data']['agent_decision']['action']=='ask'
    assert not store.list_records('listing_draft')


def test_timeout_observation_survives_model_failure(monkeypatch):
    async def timeout(*args,**kwargs):raise s.SandboxError('TIMEOUT','Timed out',stopped=True)
    monkeypatch.setattr(h,'gateway',timeout)
    monkeypatch.setattr(model,'message',lambda *args:(_ for _ in ()).throw(model.ModelError('model unavailable')))
    step=h.Step(id='price',agent='product',goal='Price',tool='product.prepare_listing',arguments={},success_criteria='Draft')
    result=run(h.run_role(None,step,{}))
    assert result['data']['sandbox_observation']['code']=='TIMEOUT' and result['status']=='waiting_input'


@pytest.mark.skipif(__import__('os').getenv('RUN_DOCKER_SANDBOX_TESTS')!='1',reason='Real Docker acceptance is opt-in; mocks are not isolation proof')
def test_real_docker_isolation_and_business_handoff():
    proof=run(s.run_job({**ctx(),'run_id':'docker-proof'},'selftest_v1',{},policy=s.Policy(pids=8)))
    assert proof['process_limit_enforced']
    assert proof['uid']==65532 and proof['seccomp']=='2' and proof['network_blocked'] and proof['root_readonly']
    assert int(proof['limits']['memory.max'])<=s.Policy().memory and int(proof['limits']['pids.max'])<=64
    args=product_setup()
    result=run(tools.product_draft({**ctx(),'operation_id':'real-docker'},args))
    assert result['data']['proposed_price']=='21.67'
    assert all(item['stopped_confirmed'] for item in store.list_records('sandbox_run'))


@pytest.mark.skipif(__import__('os').getenv('RUN_DOCKER_SANDBOX_TESTS')!='1',reason='Real Docker is required for fault injection')
def test_real_docker_oom_timeout_and_child_process_cleanup():
    for index,(job,policy,code) in enumerate([
        ('oom_probe_v1',s.Policy(memory=64*1024*1024),'MEMORY_LIMIT'),
        ('timeout_probe_v1',s.Policy(timeout=.5),'TIMEOUT')]):
        with pytest.raises(s.SandboxError) as error:
            run(s.run_job({**ctx(),'run_id':'fault-'+str(index)},job,{},policy=policy))
        assert error.value.observation['code']==code and error.value.observation['stopped_confirmed']
    assert all(item['stopped_confirmed'] for item in store.list_records('sandbox_run'))


def test_recovery_finds_late_created_container_even_after_prior_absence(engine):
    state,_=engine
    run(s.run_job(ctx(),'pricing_v1',payload()))
    assert state['container'] is None
    state['container']=copy.deepcopy(state['created'])
    assert run(s.recover_expired())==1 and state['container'] is None


def test_export_handoff_publishes_only_validated_file_and_rolls_back_stale_data(engine,env,monkeypatch,tmp_path):
    from cryptography.fernet import Fernet
    from app import sandbox_files
    monkeypatch.setenv('SANDBOX_ARTIFACT_DIR',str(tmp_path/'files'))
    monkeypatch.setenv('SANDBOX_ARTIFACT_KEY',Fernet.generate_key().decode())
    args={**product_setup(),'export_pricing_report':True}
    result=run(tools.product_draft({**ctx(),'operation_id':'export'},args))
    ref=result['data']['file_refs'][0]
    response=env.get('/api/agent-files/'+ref['file_id']+'/download')
    assert response.status_code==200 and response.json()['proposed_price']=='21.67'
    assert sandbox_files.metadata(ref['file_id'],'default')['state']=='available'
    state,_=engine
    def change():
        with store.db(write=True) as conn:conn.execute(b.stock.update().values(revision=2))
    state['after_start']=change
    with pytest.raises(s.ExecutionError):run(tools.product_draft({**ctx(),'operation_id':'stale-export'},args))
    records=store.list_records(sandbox_files.KIND)
    assert len(records)==2 and any(item['state']=='discarded' and item.get('body_removed_at') for item in records)


@pytest.mark.skipif(__import__('os').getenv('RUN_DOCKER_SANDBOX_TESTS')!='1',reason='Real Docker required for file transport acceptance')
def test_real_docker_file_export(env,monkeypatch,tmp_path):
    from cryptography.fernet import Fernet
    monkeypatch.setenv('SANDBOX_ARTIFACT_DIR',str(tmp_path/'files'))
    monkeypatch.setenv('SANDBOX_ARTIFACT_KEY',Fernet.generate_key().decode())
    result=run(tools.product_draft({**ctx(),'operation_id':'real-file'}, {**product_setup(),'export_pricing_report':True}))
    response=env.get('/api/agent-files/'+result['data']['file_refs'][0]['file_id']+'/download')
    assert response.status_code==200 and response.json()['proposed_price']=='21.67'


def test_multi_agent_file_handoff_uses_current_dependency_attempt(engine,env,monkeypatch,tmp_path):
    from cryptography.fernet import Fernet
    from app import sandbox_files
    monkeypatch.setenv('SANDBOX_ARTIFACT_DIR',str(tmp_path/'files'))
    monkeypatch.setenv('SANDBOX_ARTIFACT_KEY',Fernet.generate_key().decode())
    def message(messages,functions):
        name=functions[0]['function']['name']
        data={'decision':'accept','summary':'Checked'} if name=='review_result' else {'answer':'Done','source_steps':['a','b'],'limitations':[]}
        return {'tool_calls':[{'function':{'name':name,'arguments':json.dumps(data)}}]}
    monkeypatch.setattr(model,'message',message)
    first=h.Step(id='a',agent='product',goal='Prepare and export',tool='product.prepare_listing',arguments={**product_setup(),'export_pricing_report':True},success_criteria='File')
    second=h.Step(id='b',agent='content',goal='Read pricing',tool='artifact.read_pricing',arguments={},depends_on=['a'],bindings={'file_id':h.Binding(step='a',path=['file_refs',0,'file_id'])},success_criteria='Data')
    task=h.create('default','Export then read',h.Plan(title='File handoff',steps=[first,second]))
    h.start('default',task['id'],1);h.process_one()
    saved=h.get(task['id'],'default')
    assert saved['status']=='completed'
    from app import agent_artifacts
    result=agent_artifacts.load(saved['steps']['b']['result'],'default')
    assert result['data']['pricing']['proposed_price']=='21.67'
    assert any(event['action']=='read' and event['actor']=='agent' for event in store.list_records(sandbox_files.EVENT))
