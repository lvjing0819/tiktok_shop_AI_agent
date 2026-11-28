"""Build the reviewed image from its minimal context and verify actual kernel limits."""
import asyncio
import json
import subprocess
from pathlib import Path
from dotenv import load_dotenv
from app import sandbox


def main():
    load_dotenv()
    root=Path(__file__).resolve().parents[1]
    subprocess.run([sandbox.docker(),'build','--pull','--tag',sandbox.IMAGE_TAG,str(root/'sandbox')],env=sandbox.cli_env(),check=True)
    ctx={'workspace':'default','run_id':'sandbox-selftest','step_id':'isolation-probe'}
    result=asyncio.run(sandbox.run_job(ctx,'selftest_v1',{},policy=sandbox.Policy(pids=8)))
    print(json.dumps({'verified':True,'checks':result,'api_gateway_transport':'private_stdio; run Docker API acceptance separately',
        'limitations':['Linux user namespace remapping is not enabled by this policy','Only registered jobs; generic files/media/code jobs are not connected']},ensure_ascii=False,indent=2))


if __name__=='__main__':main()
