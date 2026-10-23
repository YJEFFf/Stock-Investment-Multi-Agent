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


@pytest.mark.parametrize('failed_command', ['is-active', 'reset-failed', 'restart'])
def test_service_timeout_isolated_and_reported(failed_command):
    import subprocess
    commands = []
    state = {}
    def run(args, **kwargs):
        commands.append(args)
        if args[1] == failed_command and args[-1] == 'cron':
            raise subprocess.TimeoutExpired(args, 20)
        return SimpleNamespace(stdout='failed' if args[-1] == 'cron' else 'active', returncode=0)
    list(services.tick(1000, state, run))
    assert ['systemctl', 'is-active', 'caddy'] in commands
    assert state['cron']['result'] == 124
    assert state['cron']['status'] == 'unknown'
    if failed_command != 'is-active':
        assert state['cron']['attempts'] == [1000]
        commands.clear()
        list(services.tick(1060, state, run))
        assert ['systemctl', 'restart', 'cron'] not in commands


@pytest.mark.parametrize('raw', ['{broken', '[]', '{"cron":{"attempts":["oops"]}}', '{"cron":{"attempts":[],"health_failures":null}}'])
def test_corrupt_state_preserves_evidence_and_budget(monkeypatch, tmp_path, raw):
    monkeypatch.setattr(services, 'STATE', tmp_path / 'services.json')
    services.STATE.write_text(raw)
    state = services.load_state(1000)
    assert list(tmp_path.glob('*.corrupt-*'))[0].read_text() == raw
    commands = []
    def run(args, **kwargs):
        commands.append(args)
        return SimpleNamespace(stdout='failed', returncode=0)
    list(services.tick(1000, state, run))
    assert not any(c[1] == 'restart' for c in commands)
    services.persist(state)
    state = services.load_state(2000)
    list(services.tick(2000, state, run))
    assert not any(c[1] == 'restart' for c in commands)
    assert state['_state_error']['result'] == 1
    list(services.tick(4601, state, run))
    assert len([c for c in commands if c[1] == 'restart']) == len(services.SERVICES)


def test_logrotate_timeout_still_dispatches_operations(monkeypatch, tmp_path):
    import subprocess
    monkeypatch.setattr(services, 'STATE', tmp_path / 'services.json')
    commands = []
    def run(args, **kwargs):
        commands.append(args)
        if args[0] == 'logrotate':
            raise subprocess.TimeoutExpired(args, 20)
        return SimpleNamespace(stdout='active', returncode=0)
    monkeypatch.setattr(services.subprocess, 'run', run)
    with pytest.raises(subprocess.TimeoutExpired):
        services.main()
    assert commands[-1] == ['systemctl', 'start', '--no-block', 'sima-operations.service']


def test_corrupt_state_alert_is_delivered_once(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(services, 'STATE', tmp_path / 'services.json')
    services.STATE.write_text('{broken')
    services.load_state(1000)
    monkeypatch.setattr(recovery, 'STATE', tmp_path / 'operations.json')
    monkeypatch.setattr(recovery, 'SERVICES', services.STATE)
    monkeypatch.setattr(run_history, 'RUN_LOG_PATH', tmp_path / 'runs')
    monkeypatch.setattr(run_history, 'RUN_LOCK_PATH', tmp_path / 'run.lock')
    at = datetime(2026, 10, 23, 18, tzinfo=codex_plan.KST)
    class Clock:
        @staticmethod
        def now(tz): return at
    monkeypatch.setattr(recovery, 'datetime', Clock)
    alerts = []
    monkeypatch.setattr(recovery.notify, 'send_telegram_alert', alerts.append)
    recovery.main()
    recovery.main()
    assert len(alerts) == 1
    assert 'state_corrupt' in alerts[0]


@pytest.mark.parametrize('probe', ['is-active', 'curl'])
def test_repeated_probe_timeout_has_stable_notification_stamp(probe):
    import subprocess
    state = {}
    def run(args, **kwargs):
        if (probe == 'is-active' and args[:2] == ['systemctl', probe]) or args[0] == probe:
            raise subprocess.TimeoutExpired(args, 20)
        return SimpleNamespace(stdout='active', returncode=0)
    list(services.tick(1000, state, run, 'example.test'))
    stamp = dict(state['sima-pwa'])
    list(services.tick(1060, state, run, 'example.test'))
    assert state['sima-pwa']['last_attempt'] == stamp['last_attempt']
    assert state['sima-pwa']['attempts'] == []
