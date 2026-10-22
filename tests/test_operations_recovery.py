from datetime import datetime
from types import SimpleNamespace
import importlib.util
from pathlib import Path

import pytest
from scripts import recover_operations as recovery
from src import codex_plan, run_history

spec = importlib.util.spec_from_file_location('recover_services', Path('deploy/recover_services.py'))
services = importlib.util.module_from_spec(spec)
spec.loader.exec_module(services)


def test_restart_budget_and_active_services():
    commands = []
    def run(args, **kwargs):
        commands.append(args)
        return SimpleNamespace(stdout='failed' if args[-1] == 'cron' else 'active', returncode=0)
    state = {}
    for now in [1000, 1060, 1300, 1600, 1900]:
        list(services.tick(now, state, run))
    restarts = [c for c in commands if c[1] == 'restart']
    assert restarts == [['systemctl', 'restart', 'cron']] * 3
    list(services.tick(5000, state, run))
    assert len([c for c in commands if c[1] == 'restart']) == 4


@pytest.mark.parametrize('hour,minute,reason,started,already,expected', [
    (7,4,None,False,False,False), (7,5,None,False,False,True),
    (8,0,None,False,False,False), (7,30,'codex_capacity_unavailable',False,False,True),
    (7,30,'codex_capacity_unavailable',True,False,False),
    (7,30,None,False,True,False), (7,30,'no_candidates',False,False,False),
    (7,30,'process_interrupted',False,False,False),
])
def test_missed_start_boundaries(hour, minute, reason, started, already, expected):
    at = datetime(2026,10,23,hour,minute,tzinfo=codex_plan.KST)
    rows = [] if reason is None else [SimpleNamespace(reason=reason,analysis_started=started)]
    assert recovery.eligible(at,rows,already) is expected


def test_prior_day_interrupted_run_is_finalized_without_touching_pending(monkeypatch,tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(recovery,'STATE',tmp_path/'state.json')
    monkeypatch.setattr(recovery,'SERVICES',tmp_path/'none')
    monkeypatch.setattr(run_history,'RUN_LOG_PATH',tmp_path/'runs.jsonl')
    monkeypatch.setattr(run_history,'RUN_LOCK_PATH',tmp_path/'run.lock')
    at=datetime(2026,10,23,18,tzinfo=codex_plan.KST)
    class Clock:
        @staticmethod
        def now(tz): return at
    monkeypatch.setattr(recovery,'datetime',Clock)
    row=run_history.new_run(datetime(2026,10,22,7,tzinfo=codex_plan.KST),'codex_plan','gpt-5.6-sol')
    row.analysis_started=True
    run_history.write(row)
    monkeypatch.setattr(recovery.notify,'send_telegram_alert',lambda *a:None)
    monkeypatch.setattr(recovery.decide_buys,'_write_pending_state',lambda *a,**k:pytest.fail('old run overwrote pending'))
    monkeypatch.setattr(recovery.subprocess,'run',lambda *a,**k:pytest.fail('analysis restarted'))
    recovery.main()
    rows=run_history.load_runs()
    assert rows[-1].status=='failed'
    assert rows[-1].reason=='process_interrupted'
    assert rows[-1].ended_at==at
    recovery.main()
    assert len(run_history.load_runs())==2


def test_recovery_rechecks_latest_run_inside_analysis_lock(monkeypatch,tmp_path):
    import asyncio
    db=recovery.decide_buys
    monkeypatch.chdir(tmp_path)
    at=datetime(2026,10,23,7,20,tzinfo=codex_plan.KST)
    class Clock:
        @staticmethod
        def now(tz): return at
    monkeypatch.setattr(db,'datetime',Clock)
    monkeypatch.setattr(run_history,'RUN_LOG_PATH',tmp_path/'runs')
    monkeypatch.setattr(run_history,'RUN_LOCK_PATH',tmp_path/'lock')
    row=run_history.new_run(at,'codex_plan','test')
    row.status='completed'
    row.ended_at=at
    run_history.write(row)
    async def forbidden(): pytest.fail('cron completed while recovery was checking readiness')
    monkeypatch.setattr(db,'_main',forbidden)
    asyncio.run(db.main(recovery=True))
    assert len(run_history.load_runs())==1


def test_health_probe_uses_deployed_trusted_host():
    commands=[]
    state={}
    def run(args,**kwargs):
        commands.append(args)
        if args[0]=='curl':
            assert args[args.index('--header')+1]=='Host: sima.example.com'
            return SimpleNamespace(returncode=0,stdout='{"ok":true}')
        return SimpleNamespace(returncode=0,stdout='active')
    for now in [1000,1060,1120]:
        list(services.tick(now,state,run,'sima.example.com'))
    assert state['sima-pwa']['health_failures']==0
    assert not any(c[0]=='systemctl' and c[1]=='restart' for c in commands)
