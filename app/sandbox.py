"""Fixed offline Docker jobs. The trusted broker, never an Agent, controls Docker."""
import asyncio
from dataclasses import asdict,dataclass
import hashlib
import json
import math
import os
import re
import shutil
import time
from uuid import uuid4
from pathlib import Path
from app import store
from app.tool_registry import ExecutionError

PROJECT='tiktok-shop-agent-sandbox-v1-'+hashlib.sha256(str(Path(__file__).resolve().parents[1]).encode()).hexdigest()[:12]
IMAGE_VERSION='v2'
IMAGE_TAG='tiktok-shop-agent-sandbox:v2'
POLICY_VERSION='offline-files-v2'
MAX_INPUT=65536
MAX_OUTPUT=32768


@dataclass(frozen=True)
class Policy:
    memory: int=1024*1024*1024
    nano_cpus: int=1_000_000_000
    pids: int=64
    timeout: float=120
    tmp_bytes: int=128*1024*1024


class SandboxError(ExecutionError):
    def __init__(self,code,message,*,stopped=False,sandbox_id=None,elapsed=0):
        super().__init__(message)
        self.observation={'type':'sandbox_failure','code':code,'message':message,
                          'sandbox_id':sandbox_id,'elapsed_seconds':round(elapsed,3),
                          'stopped_confirmed':stopped,'external_effect':'none',
                          'allowed_actions':['ask','stop'],
                          'automatic_retry':False}


def docker():
    for path in (shutil.which('docker'),'/Applications/Docker.app/Contents/Resources/bin/docker',os.path.expanduser('~/.docker/bin/docker')):
        if path and os.path.isfile(path):return path
    raise SandboxError('RUNTIME_UNAVAILABLE','未找到Docker，沙箱步骤受阻；不会退回宿主机执行')


def cli_env():
    # Docker needs client configuration; application secrets are never inherited.
    allowed=('PATH','HOME','DOCKER_HOST','DOCKER_CONTEXT','DOCKER_CONFIG','DOCKER_CERT_PATH','DOCKER_TLS_VERIFY')
    return {key:os.environ[key] for key in allowed if key in os.environ}


async def command(args,*,input=None,timeout=15,limit=MAX_OUTPUT):
    proc=await asyncio.create_subprocess_exec(docker(),*args,stdin=asyncio.subprocess.PIPE if input is not None else asyncio.subprocess.DEVNULL,
              stdout=asyncio.subprocess.PIPE,stderr=asyncio.subprocess.PIPE,env=cli_env())
    async def read(stream):
        data=bytearray()
        while True:
            chunk=await stream.read(4096)
            if not chunk:return bytes(data)
            data.extend(chunk)
            if len(data)>limit:raise SandboxError('OUTPUT_LIMIT','沙箱输出超过大小限制')
    async def write():
        if input is not None:
            try:proc.stdin.write(input);await proc.stdin.drain()
            except (BrokenPipeError,ConnectionResetError):pass
            finally:proc.stdin.close()
    tasks=[asyncio.create_task(read(proc.stdout)),asyncio.create_task(read(proc.stderr)),asyncio.create_task(write())]
    try:
        async with asyncio.timeout(timeout):
            stdout,stderr,_,_=await asyncio.gather(*tasks,proc.wait())
        return proc.returncode,stdout,stderr
    finally:
        for task in tasks:
            if not task.done():task.cancel()
        await asyncio.gather(*tasks,return_exceptions=True)
        if proc.returncode is None:proc.kill()
        # Drain without accumulating: a full stdout pipe can otherwise prevent
        # asyncio's process wait from completing after the client was killed.
        async def discard(stream):
            while await stream.read(4096):pass
        try:
            async with asyncio.timeout(3):
                await asyncio.gather(discard(proc.stdout),discard(proc.stderr),proc.wait())
        except TimeoutError:
            proc._transport.close()  # Close inherited pipes held open by a CLI helper.
        # Killing the Docker client does not stop its container; remove separately.


def identity(ctx,sandbox_id):
    def hashed(value):return hashlib.sha256(str(value).encode()).hexdigest()
    return {'ai.shop.project':PROJECT,'ai.shop.workspace':hashed(ctx['workspace']),
            'ai.shop.run':hashed(ctx['run_id']),'ai.shop.step':hashed(ctx.get('step_id','direct')),
            'ai.shop.attempt':hashed(ctx.get('attempt_id') or sandbox_id),'ai.shop.sandbox':sandbox_id}


async def inspect(name):
    rc,out,_=await command(['inspect',name])
    if rc:
        if b'No such object' in _ or b'No such container' in _:return None
        raise SandboxError('RUNTIME_UNAVAILABLE','无法向Docker确认容器状态')
    try:return json.loads(out)[0]
    except (ValueError,IndexError,TypeError):raise SandboxError('RUNTIME_PROTOCOL','容器状态返回无效') from None


def verify_container(item,labels,image_id,policy,image_env):
    h=item['HostConfig'];c=item['Config']
    expected={'ReadonlyRootfs':True,'Privileged':False,'NetworkMode':'none','Memory':policy.memory,
              'MemorySwap':policy.memory,'NanoCpus':policy.nano_cpus,'PidsLimit':policy.pids,
              'IpcMode':'private','CgroupnsMode':'private'}
    if any(h.get(k)!=v for k,v in expected.items()):raise SandboxError('POLICY_MISMATCH','容器资源或隔离配置未生效')
    if c.get('User')!='65532:65532' or item['Image']!=image_id or c.get('Entrypoint')!=['python','-I','-B','/opt/shop-sandbox/worker.py']:
        raise SandboxError('POLICY_MISMATCH','容器身份、镜像或入口不符合固定策略')
    if any(c.get('Labels',{}).get(k)!=v for k,v in labels.items()) or h.get('Binds') or any(m.get('Type')!='tmpfs' or m.get('Destination') not in ('/work','/tmp') for m in item.get('Mounts',[])):
        raise SandboxError('POLICY_MISMATCH','容器标签或挂载不符合固定策略')
    if sorted(c.get('Env') or [])!=sorted(image_env or []):raise SandboxError('POLICY_MISMATCH','拒绝容器注入的额外环境变量或代理配置')
    if h.get('PidMode') not in ('','private') or h.get('UTSMode') not in ('','private') or h.get('CapAdd'):
        raise SandboxError('POLICY_MISMATCH','禁止共享宿主命名空间或增加权限')
    if not {'ALL'}.issubset({v.upper() for v in h.get('CapDrop',[])}) or 'no-new-privileges' not in ' '.join(h.get('SecurityOpt') or []):
        raise SandboxError('POLICY_MISMATCH','容器权限限制未生效')
    if any('unconfined' in opt for opt in h.get('SecurityOpt') or []):raise SandboxError('POLICY_MISMATCH','禁止取消系统调用保护')
    expected_tmp={'/work':f'rw,nosuid,nodev,noexec,size={policy.tmp_bytes},uid=65532,gid=65532,mode=0700','/tmp':'rw,nosuid,nodev,noexec,size=16777216,uid=65532,gid=65532,mode=0700'}
    if h.get('Tmpfs')!=expected_tmp:raise SandboxError('POLICY_MISMATCH','临时空间限额未生效')
    if h.get('LogConfig',{}).get('Type')!='none' or set(h.get('Tmpfs',{}))!={'/work','/tmp'}:
        raise SandboxError('POLICY_MISMATCH','临时空间或日志配置不符合策略')


async def stop_remove(name,labels):
    item=await inspect(name)
    if item is None:return True
    if any(item['Config'].get('Labels',{}).get(k)!=v for k,v in labels.items()):return False
    target=item['Id']  # Immutable identity avoids deleting a later container with the same name.
    if item.get('State',{}).get('Running'):
        await command(['stop','--time','3',target],timeout=8)
        item=await inspect(target)
        if item and item['State'].get('Running'):await command(['kill',target],timeout=8)
    await command(['rm','--force',target],timeout=8)
    return await inspect(target) is None


def audit(ctx,key,**fields):
    with store.db(write=True) as conn:
        item=store.get(key,'sandbox_run',ctx['workspace'],conn) or {
            'id':key,'run_id':ctx['run_id'],'step_id':ctx.get('step_id'),
            'attempt_id':ctx.get('attempt_id'),'created_at':time.time(),'policy_version':POLICY_VERSION}
        item.update(fields,updated_at=time.time());store.put(conn,'sandbox_run',item,ctx['workspace'])


async def current(ctx):
    if ctx.get('assert_current'):
        with store.db() as conn:ctx['assert_current'](conn)


async def run_job(ctx,job,data,*,policy=None):
    if job not in ('pricing_v1','pricing_export_v1','selftest_v1','oom_probe_v1','timeout_probe_v1'):raise SandboxError('UNKNOWN_JOB','未登记的沙箱任务')
    policy=policy or Policy()
    if job=='oom_probe_v1' and policy.memory!=64*1024*1024:raise SandboxError('PROBE_POLICY','内存探针必须使用64MiB独立限额')
    if job=='selftest_v1' and policy.pids>16:raise SandboxError('PROBE_POLICY','进程探针必须使用不超过16的限额')
    request=json.dumps({'job':job,'data':data,'limits':asdict(policy)},ensure_ascii=False,separators=(',',':'),allow_nan=False).encode()+b'\n'
    if len(request)>MAX_INPUT:raise SandboxError('INPUT_LIMIT','沙箱输入超出大小限制')
    key=str(uuid4());name='shop-sbx-'+key;labels=identity(ctx,key);started=time.monotonic();created=False
    from sqlalchemy import select
    with store.db(write=True) as conn:
        for raw, in conn.execute(select(store.records.c.payload).where(store.records.c.kind=='sandbox_run',store.records.c.shop==ctx['workspace'])):
            prior=json.loads(raw)
            if prior['run_id']==ctx['run_id'] and not prior.get('stopped_confirmed'):
                raise SandboxError('CLEANUP_UNCONFIRMED','前一次沙箱尚未确认回收，禁止启动新执行')
        store.put(conn,'sandbox_run',{'id':key,'run_id':ctx['run_id'],'step_id':ctx.get('step_id'),
              'attempt_id':ctx.get('attempt_id'),'created_at':time.time(),'updated_at':time.time(),
              'policy_version':POLICY_VERSION,'status':'preparing','job':job,'container':name,'labels':labels,'policy':asdict(policy),
              'input_checksum':hashlib.sha256(request).hexdigest(),'deadline_at':time.time()+policy.timeout+90},ctx['workspace'])
    failure=None;result=None
    try:
        await current(ctx)
        rc,out,_=await command(['info','--format','{{json .}}'],limit=131072)
        if rc:raise SandboxError('RUNTIME_UNAVAILABLE','Docker未就绪')
        info=json.loads(out)
        if info.get('CgroupVersion')!='2' or not any('seccomp' in str(v) for v in info.get('SecurityOptions',[])):
            raise SandboxError('RUNTIME_UNAVAILABLE','沙箱需要cgroup v2和seccomp保护')
        rc,out,_=await command(['image','inspect',IMAGE_TAG])
        if rc:raise SandboxError('IMAGE_UNAVAILABLE','沙箱镜像未构建，请先运行沙箱设置脚本')
        image=json.loads(out)[0];image_id=image['Id']
        if not re.fullmatch(r'sha256:[0-9a-f]{64}',image_id) or image['Config'].get('Labels',{}).get('ai.tiktok.shop.sandbox')!=IMAGE_VERSION:
            raise SandboxError('IMAGE_UNAVAILABLE','沙箱镜像身份校验失败')
        args=['create','--name',name,'--interactive','--network','none','--read-only','--user','65532:65532',
              '--cap-drop','ALL','--security-opt','no-new-privileges','--ipc','private','--cgroupns','private',
              '--memory',str(policy.memory),'--memory-swap',str(policy.memory),'--cpus',str(policy.nano_cpus/1_000_000_000),
              '--pids-limit',str(policy.pids),'--log-driver','none','--restart','no',
              '--tmpfs',f'/work:rw,nosuid,nodev,noexec,size={policy.tmp_bytes},uid=65532,gid=65532,mode=0700',
              '--tmpfs','/tmp:rw,nosuid,nodev,noexec,size=16777216,uid=65532,gid=65532,mode=0700']
        for k,v in labels.items():args+=['--label',k+'='+v]
        # No host binds, app environment, Docker socket, arbitrary command or Agent-selected image.
        args+=[image_id]
        created=True  # Even a timed-out create may have reached the daemon.
        rc,_,_=await command(args)
        if rc:raise SandboxError('CREATE_FAILED','无法创建受限容器')
        item=await inspect(name)
        if not item:raise SandboxError('CREATE_FAILED','容器创建结果无法确认')
        verify_container(item,labels,image_id,policy,image['Config'].get('Env'))
        await current(ctx)
        audit(ctx,key,status='running',image_id=image_id,started_at=time.time())
        task=asyncio.create_task(command(['start','--attach','--interactive',name],input=request,timeout=policy.timeout))
        try:
            while not task.done():
                await asyncio.wait({task},timeout=.25)
                await current(ctx)
            rc,out,_=await task
        finally:
            if not task.done():task.cancel()
            await asyncio.gather(task,return_exceptions=True)
        item=await inspect(name);state=(item or {}).get('State',{})
        if not item or state.get('Running'):raise SandboxError('STATE_UNKNOWN','无法确认沙箱已完成')
        audit(ctx,key,exit_code=state.get('ExitCode'),oom_killed=bool(state.get('OOMKilled')))
        if state.get('OOMKilled'):raise SandboxError('MEMORY_LIMIT','沙箱超过内存限制')
        if rc or state.get('ExitCode')!=0:raise SandboxError('JOB_FAILED','沙箱任务未正常完成')
        result=json.loads(out)
        if set(result) not in ({'protocol','job','data','usage'},{'protocol','job','data','usage','files'}) or type(result['protocol']) is not int or result['protocol']!=1 or result['job']!=job or not isinstance(result['data'],dict):
            raise SandboxError('INVALID_OUTPUT','沙箱输出不符合数据契约')
        if 'file_refs' in result['data']:raise SandboxError('INVALID_OUTPUT','文件引用只能由受控存储生成')
        await current(ctx)
        usage=result['usage']
        if not isinstance(usage,dict) or set(usage)!={'cpu_seconds','peak_rss_kib'} or any(type(v) not in (int,float) or not math.isfinite(v) or v<0 for v in usage.values()):raise SandboxError('INVALID_OUTPUT','资源统计无效')
        audit(ctx,key,status='validating',output_checksum=hashlib.sha256(out).hexdigest(),worker_reported_usage=usage)
    except TimeoutError:failure=SandboxError('TIMEOUT','沙箱执行超时')
    except asyncio.CancelledError:
        failure=SandboxError('TIMEOUT' if time.monotonic()>=ctx.get('deadline_monotonic',float('inf')) else 'CANCELLED','沙箱执行已停止')
    except SandboxError as exc:failure=exc
    except ExecutionError:failure=SandboxError('STALE_ATTEMPT','任务版本或执行尝试已失效')
    except Exception:failure=SandboxError('RUNTIME_ERROR','沙箱运行环境或结果异常')
    finally:
        stopped=not created
        if created:
            try:stopped=await asyncio.shield(stop_remove(name,labels))
            except Exception:stopped=False
        if not stopped:failure=SandboxError('CLEANUP_UNCONFIRMED','沙箱停止或回收未确认，禁止自动重试')
        audit(ctx,key,status='failed' if failure else 'completed',stopped_confirmed=stopped,
              error_code=failure.observation['code'] if failure else None,elapsed_seconds=round(time.monotonic()-started,3),finished_at=time.time())
        if failure:
            failure.observation.update(stopped_confirmed=stopped,sandbox_id=key,elapsed_seconds=round(time.monotonic()-started,3))
            if not stopped:failure.observation['allowed_actions']=['ask','stop']
    if failure:raise failure
    from app import sandbox_files
    try:
        refs=sandbox_files.stage({**ctx,'sandbox_id':key},job,result.get('files',[]))
    except sandbox_files.FileError as exc:
        audit(ctx,key,status='failed',error_code=exc.code)
        raise SandboxError(exc.code,str(exc),stopped=True,sandbox_id=key,elapsed=time.monotonic()-started) from None
    return {**result['data'],**({'file_refs':refs} if refs else {})}


async def status():
    try:
        rc,out,_=await command(['info','--format','{{json .}}'],timeout=5,limit=131072)
        if rc:return {'ready':False,'reason':'Docker未启动或不可访问'}
        info=json.loads(out)
        rc,_,_=await command(['image','inspect',IMAGE_TAG],timeout=5)
        return {'ready':rc==0 and info.get('CgroupVersion')=='2' and any('seccomp' in str(v) for v in info.get('SecurityOptions',[])),
                'image_ready':rc==0,'cgroup_version':info.get('CgroupVersion'),
                'security_options':info.get('SecurityOptions',[]),'policy_version':POLICY_VERSION,
                'execution':'offline fixed JSON jobs','user_namespace_enforced':False}
    except (SandboxError,OSError,TimeoutError,ValueError):return {'ready':False,'reason':'尚未连接可用Docker运行环境'}


async def recover_expired():
    """Reconcile only labelled containers whose recorded execution is no longer valid."""
    from sqlalchemy import select
    with store.db() as conn:
        rows=conn.execute(select(store.records.c.shop,store.records.c.payload).where(store.records.c.kind=='sandbox_run')).all()
    try:
        rc,out,_=await command(['ps','--all','--filter','label=ai.shop.project='+PROJECT,'--format','{{.ID}}'],timeout=5)
        if rc:return 0
        live={}
        for container_id in out.decode().splitlines():
            container=await inspect(container_id)
            if container:
                labels=container.get('Config',{}).get('Labels',{})
                if labels.get('ai.shop.project')==PROJECT:live[labels.get('ai.shop.sandbox')]=container_id
    except (SandboxError,OSError,TimeoutError):return 0
    recovered=0
    for workspace,raw in rows:
        item=json.loads(raw)
        if not item.get('container') or not item.get('labels'):continue
        if item.get('stopped_confirmed') and item['id'] not in live:continue
        # Also catch containers created late after a cancelled create RPC.
        invalid=item.get('stopped_confirmed',False) or item.get('deadline_at',float('inf'))<time.time()
        run=store.get(item['run_id'],'operation_run',workspace)
        if run:
            state=run.get('steps',{}).get(item.get('step_id'),{})
            invalid=invalid or run['status']!='running' or state.get('attempt_id')!=item.get('attempt_id') or run.get('lease_until',0)<time.time()
        if not invalid:continue
        try:
            stopped=await stop_remove(live.get(item['id'],item['container']),item['labels'])
            if stopped:
                audit({'workspace':workspace,'run_id':item['run_id']},item['id'],status='recovered',stopped_confirmed=True,finished_at=time.time())
                recovered+=1
        except (SandboxError,OSError,TimeoutError):continue
    return recovered
