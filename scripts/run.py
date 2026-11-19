"""Start the local API and worker together; Ctrl-C shuts down both."""
import subprocess
import sys
import time


def main():
    children=[]
    try:
        for args in [('app.worker',), ('uvicorn','app.main:app','--host','127.0.0.1','--port','8000')]:
            children.append(subprocess.Popen([sys.executable,'-m',*args]))
        print('工作台地址：http://127.0.0.1:8000', flush=True)
        while all(child.poll() is None for child in children):
            time.sleep(.5)
        raise SystemExit('一个服务已退出，请检查上方日志')
    except KeyboardInterrupt:
        pass
    finally:
        for child in children:
            if child.poll() is None:
                child.terminate()
        for child in children:
            try:
                child.wait(timeout=10)
            except subprocess.TimeoutExpired:
                child.kill()
                child.wait()


if __name__=='__main__':
    main()
