"""프로세스가 달라도 하나의 KIS 요청 간격을 사용해야 한다."""
import subprocess
import sys
import time
from pathlib import Path

from src import kis


_POPEN = subprocess.Popen

def test_separate_processes_share_spacing(tmp_path):
    clock = tmp_path / 'clock'
    code = '''
import sys, time
from pathlib import Path
from src import kis
kis.RATE_LIMIT_PATH = Path(sys.argv[1])
kis.MIN_REQUEST_SPACING_SECONDS = .08
kis._throttle()
print(kis._last_request_at, flush=True)
'''
    processes=[_POPEN([sys.executable,'-c',code,str(clock)],stdout=subprocess.PIPE,text=True) for _ in range(3)]
    stamps=[]
    for p in processes:
        stdout,_=p.communicate(timeout=10)
        assert p.returncode==0
        stamps.append(float(stdout))
    stamps.sort()
    assert all(b-a >= .075 for a,b in zip(stamps,stamps[1:]))


def test_corrupt_clock_waits_and_repairs(monkeypatch):
    kis.RATE_LIMIT_PATH.write_text('nan')
    sleeps=[]
    monkeypatch.setattr(kis.time,'sleep',sleeps.append)
    kis._throttle()
    assert .9 < sleeps[0] <= 1
    assert float(kis.RATE_LIMIT_PATH.read_text()) > 0
