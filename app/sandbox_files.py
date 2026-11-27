"""Small, typed sandbox outputs. No shared directory or arbitrary file upload API."""
import base64
import hashlib
import heapq
import json
import os
from pathlib import Path
import re
import stat
import time
from uuid import uuid4,UUID
from cryptography.fernet import Fernet,InvalidToken
from app import store,agent_artifacts
from app.tool_registry import ExecutionError

KIND='sandbox_file'
EVENT='sandbox_file_event'
MAX_FILE=8192
MAX_TOTAL=12288
# The trusted service chooses classification. Worker/Agent declarations cannot lower it.
RULES={'pricing_export_v1':{'pricing.json':('output','restricted','application/json'),
                          'intermediate.json':('temporary','restricted','application/json')}}


class FileError(ExecutionError):
    def __init__(self,code,message):super().__init__(message);self.code=code


def event(conn,workspace,ctx,action,*,file_id=None,reason=None,actor='executor',**metadata):
    store.put(conn,EVENT,{'id':str(uuid4()),'file_id':file_id,'run_id':ctx.get('run_id'),
        'step_id':ctx.get('step_id'),'attempt_id':ctx.get('attempt_id'),'sandbox_id':ctx.get('sandbox_id'),'actor':actor,
        'action':action,'reason':reason,'at':time.time(),**metadata},workspace)


def cipher():
    key=os.getenv('SANDBOX_ARTIFACT_KEY') or os.getenv('ADS_TOKEN_ENCRYPTION_KEY')
    try:return Fernet(key.encode())
    except (AttributeError,ValueError):raise FileError('FILE_KEY_UNAVAILABLE','文件存储密钥未配置或格式无效；不会以明文保存') from None


def key_id():
    key=os.getenv('SANDBOX_ARTIFACT_KEY') or os.getenv('ADS_TOKEN_ENCRYPTION_KEY') or ''
    return hashlib.sha256(key.encode()).hexdigest()[:16]


def _directory(workspace):
    root=Path(os.getenv('SANDBOX_ARTIFACT_DIR','.sandbox-files')).absolute()
    root.mkdir(mode=0o700,parents=True,exist_ok=True)
    if root.is_symlink():raise FileError('FILE_STORAGE_FAILED','文件存储目录不能是符号链接')
    fd=os.open(root,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW)
    os.fchmod(fd,0o700)
    name=hashlib.sha256(workspace.encode()).hexdigest()
    try:
        try:os.mkdir(name,0o700,dir_fd=fd)
        except FileExistsError:pass
        target=os.open(name,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW,dir_fd=fd)
        os.fchmod(target,0o700)
    finally:os.close(fd)
    return target


def _filename(key):
    try:
        if str(UUID(key))!=key:raise ValueError()
    except (ValueError,TypeError,AttributeError):raise LookupError('文件不存在') from None
    return key+'.bin'


def _write(workspace,key,body):
    fd=_directory(workspace);name=_filename(key);created=False
    try:
        out=os.open(name,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600,dir_fd=fd)
        created=True
        with os.fdopen(out,'wb') as f:f.write(body);f.flush();os.fsync(f.fileno())
    except BaseException:
        if created:
            try:os.unlink(name,dir_fd=fd)
            except FileNotFoundError:pass
        raise
    finally:os.close(fd)


def _read(workspace,key):
    fd=_directory(workspace)
    try:
        src=os.open(_filename(key),os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK,dir_fd=fd)
        with os.fdopen(src,'rb') as f:
            if not stat.S_ISREG(os.fstat(f.fileno()).st_mode):raise FileError('FILE_INTEGRITY','文件类型异常')
            value=f.read(MAX_FILE*4)
            if len(value)>=MAX_FILE*4:raise FileError('FILE_INTEGRITY','文件正文超出限制')
            return value
    except OSError:raise FileError('FILE_INTEGRITY','文件正文不存在或不可读取') from None
    finally:os.close(fd)


def _unlink(workspace,key):
    fd=_directory(workspace)
    try:
        try:os.unlink(_filename(key),dir_fd=fd)
        except FileNotFoundError:pass
    finally:os.close(fd)


def scan_content(raw):
    try:text=raw.decode('utf-8');data=json.loads(text)
    except (UnicodeError,ValueError):raise FileError('FILE_TYPE','仅接收有效UTF-8 JSON产物') from None
    known=[v for k,v in os.environ.items() if len(v)>=8 and re.search(r'(SECRET|TOKEN|API_KEY|PASSWORD)',k,re.I)]
    if any(value in text for value in known) or re.search(r'-----BEGIN .*PRIVATE KEY|\bBearer\s+\S+|<\s*(script|html)\b',text,re.I):
        raise FileError('FILE_SECRET','检测到凭据或禁止的活动内容，产物已拒绝')
    def walk(value):
        if isinstance(value,dict):
            for key,item in value.items():
                if re.search(r'access.?token|refresh.?token|app.?secret|api.?key|password|authorization|cookie|private.?key',key,re.I):
                    raise FileError('FILE_SECRET','检测到敏感凭据字段，产物已拒绝')
                walk(item)
        elif isinstance(value,list):
            for item in value:walk(item)
    walk(data)
    if (not isinstance(data,dict) or set(data)!={'proposed_price','proposed_channel_quantity'}
        or not isinstance(data['proposed_price'],str) or not re.fullmatch(r'[0-9]{1,60}\.[0-9]{2}',data['proposed_price'])
        or type(data['proposed_channel_quantity']) is not int or data['proposed_channel_quantity']<0):
        raise FileError('FILE_SCHEMA','定价报告字段不符合固定文件契约')
    return data


def validate_bundle(job,bundle):
    rules=RULES.get(job,{})
    if not isinstance(bundle,list) or len(bundle)>4:raise FileError('FILE_MANIFEST','文件清单无效')
    seen=set();total=0;validated=[]
    for item in bundle:
        if not isinstance(item,dict) or set(item)!={'path','purpose','sensitivity','media_type','size','sha256','content_b64'}:
            raise FileError('FILE_MANIFEST','文件清单字段无效')
        name=item['path']
        if not isinstance(name,str) or name not in rules or name in seen:raise FileError('FILE_PATH','未授权的文件名、路径或重复文件')
        seen.add(name);purpose,sensitivity,mime=rules[name]
        if (item['purpose'],item['sensitivity'],item['media_type'])!=(purpose,sensitivity,mime):
            raise FileError('FILE_CLASSIFICATION','文件分类不能由执行任务降级')
        if type(item['size']) is not int or not 0<=item['size']<=MAX_FILE or not re.fullmatch(r'[0-9a-f]{64}',str(item['sha256'])):
            raise FileError('FILE_LIMIT','文件大小或校验信息无效')
        total+=item['size']
        if total>MAX_TOTAL:raise FileError('FILE_LIMIT','文件总量超出上限')
        if purpose=='temporary':
            if item['content_b64'] is not None:raise FileError('FILE_MANIFEST','临时文件正文不应传出沙箱')
            raw=None
        else:
            try:
                if not isinstance(item['content_b64'],str) or len(item['content_b64'])>MAX_FILE*2:raise ValueError()
                raw=base64.b64decode(item['content_b64'],validate=True)
            except (ValueError,TypeError):raise FileError('FILE_MANIFEST','文件内容编码无效') from None
            if len(raw)!=item['size'] or hashlib.sha256(raw).hexdigest()!=item['sha256']:raise FileError('FILE_INTEGRITY','文件正文校验失败')
            scan_content(raw)
        validated.append((name,purpose,sensitivity,mime,item['sha256'],item['size'],raw))
    if set(rules)!=seen:raise FileError('FILE_MANIFEST','固定任务缺少预期文件')
    return validated


def stage(ctx,job,bundle):
    workspace=ctx['workspace'];written=[];refs=[]
    try:
        checked=validate_bundle(job,bundle)
        with store.db(write=True) as conn:
            if ctx.get('assert_current'):ctx['assert_current'](conn)
            for name,purpose,sensitivity,mime,checksum,size,raw in checked:
                key=str(uuid4())
                event(conn,workspace,ctx,'validated',file_id=key,name=name,purpose=purpose,sensitivity=sensitivity,bytes=size,checksum=checksum)
                if purpose=='temporary':
                    event(conn,workspace,ctx,'temporary_discarded',file_id=key,name=name)
                    continue
                packet=json.dumps({'workspace':workspace,'id':key,'checksum':checksum,'body':base64.b64encode(raw).decode()},separators=(',',':')).encode()
                encrypted=cipher().encrypt(packet)
                _write(workspace,key,encrypted);written.append(key)
                item={'id':key,'version':1,'revision':1,'checksum':checksum,'name':name,'purpose':purpose,'sensitivity':sensitivity,
                      'media_type':mime,'bytes':size,'stored_bytes':len(encrypted),'state':'staged','pinned':False,'key_id':key_id(),
                      'run_id':ctx['run_id'],'step_id':ctx.get('step_id'),'attempt_id':ctx.get('attempt_id'),'sandbox_id':ctx.get('sandbox_id'),
                      'allowed_agents':['product','content'],'created_at':time.time()}
                store.put(conn,KIND,item,workspace);event(conn,workspace,ctx,'staged',file_id=key)
                refs.append({'file_id':key,'version':1,'checksum':checksum})
        return refs
    except BaseException as exc:
        for key in written:
            try:_unlink(workspace,key)
            except OSError:pass
        if not isinstance(exc,Exception):raise
        with store.db(write=True) as conn:event(conn,workspace,ctx,'rejected',reason=getattr(exc,'code','FILE_STORAGE_FAILED'))
        if isinstance(exc,(FileError,ExecutionError)):raise
        raise FileError('FILE_STORAGE_FAILED','加密文件保存失败') from None


def metadata(key,workspace,conn=None):
    _filename(key)
    item=store.get(key,KIND,workspace,conn)
    if not item:raise LookupError('文件不存在')
    return item


def publish(conn,ctx,refs,expected_data):
    if ctx.get('assert_current'):ctx['assert_current'](conn)
    for ref in refs:
        item=metadata(ref['file_id'],ctx['workspace'],conn)
        if item['state']!='staged' or item['run_id']!=ctx['run_id'] or item.get('step_id')!=ctx.get('step_id') or item.get('attempt_id')!=ctx.get('attempt_id') or (item['version'],item['checksum'])!=(ref['version'],ref['checksum']):
            raise FileError('FILE_STATE','文件来源或执行版本不匹配')
        if scan_content(_decode(item,ctx['workspace']))!=expected_data:raise FileError('FILE_SCHEMA','报告内容与业务交付不一致')
        item.update(state='available',revision=item['revision']+1,published_at=time.time())
        store.put(conn,KIND,item,ctx['workspace']);event(conn,ctx['workspace'],ctx,'published',file_id=item['id'])


def _decode(item,workspace):
    key=item['id']
    try:
        packet=json.loads(cipher().decrypt(_read(workspace,key)))
        raw=base64.b64decode(packet['body'],validate=True)
        if packet['workspace']!=workspace or packet['id']!=key or packet['checksum']!=item['checksum'] or hashlib.sha256(raw).hexdigest()!=item['checksum'] or len(raw)!=item['bytes']:
            raise ValueError()
    except (InvalidToken,ValueError,KeyError,TypeError):raise FileError('FILE_INTEGRITY','文件解密或校验失败') from None
    return raw


def download(key,workspace,*,actor='owner',ctx=None):
    with store.db(write=True) as conn:
        item=metadata(key,workspace,conn)
        if actor=='agent':ctx['assert_current'](conn)
        if item['state'] in ('expired','discarded'):raise FileError('FILE_EXPIRED','文件已过期或已回收')
        if item['state']!='available':raise LookupError('文件尚未交付')
        raw=_decode(item,workspace)
        event(conn,workspace,ctx or {'run_id':item['run_id']},'read',file_id=key,actor=actor)
        return item,raw


def discard(ctx,refs):
    if not refs:return
    with store.db(write=True) as conn:
        for ref in refs:
            item=metadata(ref['file_id'],ctx['workspace'],conn)
            if item['state']!='staged':continue
            item.update(state='discarded',expired_at=time.time(),revision=item['revision']+1)
            store.put(conn,KIND,item,ctx['workspace']);event(conn,ctx['workspace'],ctx,'discarded',file_id=item['id'])
    purge(ctx['workspace'],{ref['file_id'] for ref in refs})


def file_ids(value):
    if isinstance(value,dict):
        for ref in value.get('file_refs',[]):
            if isinstance(ref,dict) and isinstance(ref.get('file_id'),str):yield ref['file_id']
        for key,item in value.items():
            if key!='file_refs':yield from file_ids(item)
    elif isinstance(value,list):
        for item in value:yield from file_ids(item)


def protected(workspace,runs,artifact_ids,conn):
    from app.agent_maintenance import scan,CLOSED
    ids=set()
    for item in scan(KIND,workspace,conn):
        source=runs.get(item['run_id'])
        if item.get('pinned') or item['state']=='available' and source and (source['status'] not in CLOSED or source.get('requires_reconciliation')):ids.add(item['id'])
        if item['state']=='staged' and source and source['status']=='running' and source.get('lease_until',0)>time.time() and source.get('steps',{}).get(item.get('step_id'),{}).get('attempt_id')==item.get('attempt_id'):ids.add(item['id'])
    for key in artifact_ids:
        record=store.get(key,'agent_artifact',workspace,conn)
        if not record or 'payload' not in record:
            return None  # Unknown references fail closed.
        ids.update(file_ids(record['payload']))
    return ids


def pin(key,workspace,revision,pinned):
    with store.db(write=True) as conn:
        item=metadata(key,workspace,conn)
        if item['state']!='available':raise FileError('FILE_EXPIRED','文件不可保留或已过期')
        if item['revision']!=revision:raise FileError('FILE_STATE','文件保留设置已变化')
        item.update(pinned=pinned,revision=revision+1);item.pop('cleanup_due_at',None)
        store.put(conn,KIND,item,workspace);event(conn,workspace,{'run_id':item['run_id']},'pinned' if pinned else 'unpinned',file_id=key,actor='owner')
        return item


def expire(conn,workspace,item):
    item.update(state='expired',expired_at=time.time(),revision=item['revision']+1)
    event(conn,workspace,{'run_id':item['run_id']},'expired',file_id=item['id'])
    return item


def purge(workspace,keys=None):
    from app.agent_maintenance import scan
    for item in scan(KIND,workspace):
        if keys is not None and item['id'] not in keys:continue
        if item['state'] not in ('expired','discarded') or item.get('body_removed_at'):continue
        try:_unlink(workspace,item['id'])
        except (OSError,FileError):continue  # Tombstone already denies reads; retry deletion later.
        with store.db(write=True) as conn:
            latest=metadata(item['id'],workspace,conn)
            if latest.get('body_removed_at'):continue
            latest['body_removed_at']=time.time();store.put(conn,KIND,latest,workspace)
            event(conn,workspace,{'run_id':item['run_id']},'body_removed',file_id=item['id'])


def read_for_agent(ctx,key):
    with store.db(write=True) as conn:
        if not ctx.get('assert_current'):raise FileError('FILE_ACCESS','Agent文件读取必须绑定有效执行尝试')
        execution=ctx['assert_current'](conn)
        item=metadata(key,ctx['workspace'],conn)
        spec=next(s for s in execution['plan']['steps'] if s['id']==ctx['step_id'])
        if item['run_id']!=ctx['run_id'] or item['step_id'] not in spec.get('depends_on',[]) or execution['steps'][item['step_id']]['status']!='completed' or execution['steps'][item['step_id']].get('attempt_id')!=item.get('attempt_id') or spec['agent'] not in item['allowed_agents']:
            event(conn,ctx['workspace'],ctx,'access_denied',file_id=key,actor='agent')
            denied=True
        else:denied=False
    if denied:raise FileError('FILE_ACCESS','Agent只能读取本任务已完成依赖步骤交付的授权文件')
    # Return only approved business fields, never an arbitrary executable document.
    item,raw=download(key,ctx['workspace'],actor='agent',ctx=ctx)
    return {'file_ref':{'file_id':key,'version':item['version'],'checksum':item['checksum']},'pricing':scan_content(raw)}


def sweep_orphans(workspace):
    # Only private UUID-named ciphertext blobs, never arbitrary project files.
    root=Path(os.getenv('SANDBOX_ARTIFACT_DIR','.sandbox-files')).absolute()
    if not root.exists():return
    try:directory=_directory(workspace)
    except (OSError,FileError):return
    try:
        for name in os.listdir(directory)[:256]:
            if not re.fullmatch(r'[0-9a-f-]{36}\.bin',name):continue
            try:_filename(name[:-4])
            except LookupError:continue
            try:info=os.stat(name,dir_fd=directory,follow_symlinks=False)
            except FileNotFoundError:continue
            if not stat.S_ISREG(info.st_mode) or time.time()-info.st_mtime<7*86400:continue
            with store.db(write=True) as conn:
                if store.get(name[:-4],KIND,workspace,conn):continue
                try:os.unlink(name,dir_fd=directory)
                except FileNotFoundError:continue
                event(conn,workspace,{},'orphan_removed',file_id=name[:-4])
    finally:os.close(directory)


def history(workspace,*,file_id=None,run_id=None):
    from app.agent_maintenance import scan
    events=(item for item in scan(EVENT,workspace)
            if (file_id is None or item.get('file_id')==file_id) and (run_id is None or item.get('run_id')==run_id))
    return sorted(heapq.nlargest(100,events,key=lambda item:item['at']),key=lambda item:item['at'])
