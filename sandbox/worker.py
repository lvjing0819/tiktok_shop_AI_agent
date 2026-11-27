"""Fixed offline jobs. No imports from the application, credentials or database."""
import json
import base64
import hashlib
import stat
import os
import resource
import socket
import sys
import time
from decimal import Decimal, ROUND_UP
from pathlib import Path


def pricing(data):
    fields={'unit_cost','logistics','fixed_fees','fee_rate','target_margin','available','listing_cap'}
    if set(data)!=fields:raise ValueError('Invalid pricing input')
    values={k:Decimal(data[k]) for k in fields-{'available','listing_cap'}}
    if any(not v.is_finite() or v<0 for v in values.values()):raise ValueError('Invalid amount')
    denominator=Decimal(1)-values['fee_rate']-values['target_margin']
    if denominator<=0:raise ValueError('Invalid pricing rules')
    if any(type(data[k]) is not int or data[k]<0 for k in ('available','listing_cap')):raise ValueError('Invalid quantity')
    price=((values['unit_cost']+values['logistics']+values['fixed_fees'])/denominator).quantize(Decimal('.01'),rounding=ROUND_UP)
    return {'proposed_price':str(price),'proposed_channel_quantity':min(data['available'],data['listing_cap'])}



def package_file(folder,name,purpose):
    # Only regular, single-linked files under this fixed directory may be read.
    if name not in ('pricing.json','intermediate.json'):raise ValueError('Invalid output path')
    directory=os.open(folder,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW)
    try:
        fd=os.open(name,os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK,dir_fd=directory)
        with os.fdopen(fd,'rb') as f:
            info=os.fstat(f.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_nlink!=1 or info.st_size>8192:raise ValueError('Invalid output file')
            raw=f.read(8193)
            if len(raw)>8192:raise ValueError('Output too large')
    finally:os.close(directory)
    return {'path':name,'purpose':purpose,'sensitivity':'restricted','media_type':'application/json',
            'size':len(raw),'sha256':hashlib.sha256(raw).hexdigest(),
            'content_b64':base64.b64encode(raw).decode() if purpose=='output' else None}


def pricing_export(data,folder=None):
    result=pricing(data)
    folder=Path('/work/output') if folder is None else Path(folder);folder.mkdir(mode=0o700)
    (folder/'intermediate.json').write_text(json.dumps(data),encoding='utf-8')
    (folder/'pricing.json').write_text(json.dumps(result,separators=(',',':')),encoding='utf-8')
    return result


def runtime_probe(data):
    if data:raise ValueError('Unexpected probe arguments')
    status={line.split(':',1)[0]:line.split(':',1)[1].strip() for line in Path('/proc/self/status').read_text().splitlines() if ':' in line}
    network_blocked=False
    try:
        with socket.create_connection(('1.1.1.1',443),timeout=1):pass
    except OSError:network_blocked=True
    readonly=False
    try:Path('/root-write-probe').write_text('probe')
    except OSError:readonly=True
    limits={}
    for name in ('memory.max','memory.swap.max','pids.max','cpu.max'):
        p=Path('/sys/fs/cgroup')/name
        limits[name]=p.read_text().strip() if p.exists() else None
    return {'uid':os.getuid(),'seccomp':status.get('Seccomp'),'no_new_privs':status.get('NoNewPrivs'),
            'capabilities':status.get('CapEff'),'network_blocked':network_blocked,'root_readonly':readonly and bool(os.statvfs('/').f_flag & os.ST_RDONLY),
            'limits':limits,'user_mapping':Path('/proc/self/uid_map').read_text().strip()}



def selftest(data):
    proof=runtime_probe(data)
    limit=int(proof['limits']['pids.max'])
    if limit>16:raise ValueError('Probe requires a small process limit')
    children=[];blocked=False
    try:
        for _ in range(limit+1):
            try:pid=os.fork()
            except OSError:blocked=True;break
            if pid==0:
                time.sleep(2);os._exit(0)
            children.append(pid)
    finally:
        for pid in children:os.waitpid(pid,0)
    events=dict(line.split() for line in Path('/sys/fs/cgroup/pids.events').read_text().splitlines())
    if not blocked or int(events.get('max','0'))<1:raise ValueError('PID limit not enforced')
    return {**proof,'process_limit_enforced':True}


def oom_probe(data):
    if data:raise ValueError('Unexpected probe arguments')
    # Only enabled by the broker with a 64MiB hard memory limit.
    block=bytearray(128*1024*1024)
    return {'unexpected_success':len(block)}


def timeout_probe(data):
    if data:raise ValueError('Unexpected probe arguments')
    pid=os.fork()
    time.sleep(60)
    if pid==0:os._exit(0)
    os.waitpid(pid,0)
    return {'unexpected_success':True}


def main():
    raw=sys.stdin.buffer.readline(65537)
    if len(raw)>65536:raise ValueError('Input too large')
    request=json.loads(raw)
    if set(request)!={'job','data','limits'} or not isinstance(request['data'],dict):raise ValueError('Invalid request')
    jobs={'pricing_v1':pricing,'pricing_export_v1':pricing_export,'selftest_v1':selftest,'oom_probe_v1':oom_probe,'timeout_probe_v1':timeout_probe}
    if request['job'] not in jobs:raise ValueError('Unknown job')
    probe=runtime_probe({})
    limits=probe['limits'];expected=request['limits']
    quota,period=map(int,limits['cpu.max'].split())
    if (probe['uid']!=65532 or probe['seccomp']!='2' or probe['no_new_privs']!='1'
        or int(probe['capabilities'],16)!=0 or not probe['network_blocked'] or not probe['root_readonly']
        or int(limits['memory.max'])>expected['memory'] or int(limits['memory.swap.max'])!=0
        or os.statvfs('/work').f_frsize*os.statvfs('/work').f_blocks>expected['tmp_bytes']
        or int(limits['pids.max'])>expected['pids'] or quota/period>expected['nano_cpus']/1_000_000_000):
        raise ValueError('Isolation not enforced')
    answer={'protocol':1,'job':request['job'],'data':jobs[request['job']](request['data'])}
    if request['job']=='pricing_export_v1':
        answer['files']=[package_file('/work/output','pricing.json','output'),package_file('/work/output','intermediate.json','temporary')]
    usage=resource.getrusage(resource.RUSAGE_SELF)
    answer['usage']={'cpu_seconds':round(usage.ru_utime+usage.ru_stime,6),'peak_rss_kib':usage.ru_maxrss}
    sys.stdout.write(json.dumps(answer,separators=(',',':'))+'\n')


if __name__=='__main__':
    try:main()
    except Exception:
        sys.stderr.write('Sandbox job rejected or failed\n');sys.exit(2)
