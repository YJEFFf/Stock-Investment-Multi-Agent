import copy
import json
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest
import requests

from src import collector_routes as routes, collector_health as health, collector_repair as repair, notify

REAL_NAVER_QUOTES = health.collectors.fetch_naver_quotes

AT = datetime(2026, 10, 19, 6, 20, tzinfo=health.KST)


@pytest.fixture(autouse=True)
def no_external_actions(monkeypatch):
    monkeypatch.setattr(repair, "now", lambda: AT)
    monkeypatch.setattr(repair, "WORK_DEADLINE", None)
    monkeypatch.setattr(repair, "model_call", lambda *a, **k: pytest.fail("real model call"))
    monkeypatch.setattr(health, "get_text", lambda *a, **k: pytest.fail("real source request"))
    monkeypatch.setattr(repair, "git", lambda *a, **k: pytest.fail("real git action"))


@pytest.mark.parametrize("url", [
    "http://stock.naver.com/api", "https://stock.naver.com.evil.test/api",
    "https://user@stock.naver.com/api", "https://stock.naver.com:444/api",
    "https://stock.naver.com/api?symbol=OTHER", "https://stock.naver.com/api#x",
    "https://stock.naver.com/%2e%2e/x", "https://stock.naver.com/../x",
    "https://127.0.0.1/x", "https://stock.naver.com/\\evil.test/x",
])
def test_manifest_rejects_unapproved_urls(url):
    manifest = routes.default_manifest()
    manifest["routes"]["index"]["url"] = url
    with pytest.raises(ValueError):
        routes.validate_manifest(manifest)


@pytest.mark.parametrize("change", [
    {"url": "https://stock.naver.com/news"},
    {"url": "https://stock.naver.com/{ticker.__class__}"},
    {"url": "https://stock.naver.com/{ticker!r}"},
    {"keys": {"title": "row['body']"}},
    {"root": ["items[0]"]},
    {"code": "print('bad')"},
])
def test_manifest_rejects_code_and_lost_identity(change):
    manifest = routes.default_manifest()
    manifest["routes"]["stock_news"].update(change)
    with pytest.raises(ValueError):
        routes.validate_manifest(manifest)


def test_quotes_cannot_be_remapped_to_integrated_prices():
    manifest = routes.default_manifest()
    manifest["routes"]["quotes"]["keys"] = {"closePriceRaw": "integratedPrice"}
    with pytest.raises(ValueError):
        routes.validate_manifest(manifest)


def test_mapping_handles_renamed_json_without_executing_expressions():
    manifest = routes.default_manifest()
    manifest["routes"]["stock_news"].update(root=["result"], keys={"title": "headline"})
    routes.validate_manifest(manifest)
    raw = '{"result":[{"items":[{"headline":"text", "datetime":"202610160900"}]}]}'
    normalized = json.loads(routes.normalize("stock_news", raw, manifest))
    assert normalized[0]["items"][0]["title"] == "text"
    with pytest.raises(ValueError):
        routes.normalize("stock_news", '{"result":{"title":"one","headline":"two"}}', manifest)
    with pytest.raises(ValueError):
        routes.normalize("stock_news", '{"wrong":[]}', manifest)


def index_xml():
    days, day = [], health.expected_session(AT)
    while len(days) < 60:
        if health.is_krx_trading_day(day):
            days.append(day)
        day -= timedelta(days=1)
    rows = "".join(f'<item data="{d:%Y%m%d}|100|102|99|101|2000"/>' for d in reversed(days))
    return '<protocol><chartdata symbol="KPI200" timeframe="day">' + rows + '</chartdata></protocol>'


def test_index_requires_correct_symbol_fresh_session_and_sound_prices():
    assert len(health.index_data(index_xml(), AT)) == 60
    for broken in (index_xml().replace('KPI200', 'KOSPI'),
                   index_xml().replace('20261016', '20261015'),
                   index_xml().replace('|100|102|99|101|', '|100|102|99|nan|'),
                   index_xml().replace('|100|102|99|101|', '|100|98|99|101|')):
        with pytest.raises(health.ProbeError):
            health.index_data(broken, AT)


def test_news_canary_checks_are_separate_from_empty_stock_news():
    from src.collectors import _parse_company_news_api
    assert _parse_company_news_api('[]') == []
    with pytest.raises(health.ProbeError):
        health.news_data('[]', AT)


def test_index_expected_session_distinguishes_premarket_and_1600_ic():
    from datetime import date
    assert health.expected_session(AT.replace(hour=8, minute=30)) == date(2026, 10, 16)
    assert health.expected_session(AT.replace(hour=16, minute=0)) == date(2026, 10, 19)
    assert health.expected_session(AT.replace(day=17, hour=16)) == date(2026, 10, 16)


@pytest.mark.parametrize('url', ['https://STOCK.NAVER.COM/api', 'https://stock.naver.com:443/api'])
def test_runtime_naver_redirect_policy_matches_manifest_variants(monkeypatch, url):
    from src import collectors
    calls = []
    def get(*args, **kwargs):
        calls.append(kwargs)
        return SimpleNamespace(status_code=302)
    monkeypatch.setattr(collectors.requests, 'get', get)
    assert collectors._fetch_with_retries(url, lambda t: t) is None
    assert len(calls) == 1 and calls[0]['allow_redirects'] is False


def test_member_pagination_must_be_complete(monkeypatch):
    pages = iter([
        {"items": [{"itemCode": "005930"}], "hasNext": True, "cursor": "next"},
        {"items": [{"itemCode": "000660"}], "hasNext": False},
    ])
    monkeypatch.setattr(health, "request", lambda *a, **k: json.dumps(next(pages)))
    assert health.fetch_members(routes.default_manifest(), {"307": {"count": 2}}) == {"307": ["000660", "005930"]}
    monkeypatch.setattr(health, "request", lambda *a, **k: '{"items":[],"hasNext":false}')
    with pytest.raises(health.ProbeError, match="incomplete"):
        health.fetch_members(routes.default_manifest(), {"307": {"count": 2}})


def good_report(*, full=True):
    return {"checked_at": AT.isoformat(), "manifest": routes.digest(routes.default_manifest()), "full": full,
            "sources": {name: {"status": "ok", "data": {}} for name in routes.DEFAULT_URLS}}


def test_no_baseline_or_changed_semantics_cannot_activate():
    report = good_report()
    with pytest.raises(ValueError, match="no comparable"):
        health.validate_compatibility({}, report, {"index"})
    old = {"index": {"status": "ok", "data": health.index_data(index_xml(), AT)}}
    report["sources"]["index"]["data"] = copy.deepcopy(old["index"]["data"])
    health.validate_compatibility(old, report, {"index"})
    report["sources"]["index"]["data"]["2026-10-16"]["close"] = 200
    with pytest.raises(ValueError, match="history changed"):
        health.validate_compatibility(old, report, {"index"})


def test_failed_enqueue_is_retried_and_same_day_recurrence_is_a_new_incident(monkeypatch):
    state, sent = {"incidents": {}}, []
    monkeypatch.setattr(notify, "send_telegram_alert", lambda m: False)
    repair.report(state, "index", True, "failed")
    assert state["incidents"]["index"]["sent_problem"] is False
    monkeypatch.setattr(notify, "send_telegram_alert", lambda m: sent.append(m) or True)
    repair.report(state, "index", True, "failed")
    repair.report(state, "index", True, "failed")
    assert len(sent) == 1
    repair.report(state, "index", False, "recovered")
    repair.report(state, "index", True, "failed again")
    assert len(sent) == 3


def test_failed_daily_alert_enqueue_does_not_consume_marker(monkeypatch):
    monkeypatch.setattr(notify, "send_telegram_alert", lambda m: False)
    assert notify.alert_once_per_day("failed_write", "message") is False
    assert not (notify.ALERT_MARKER_DIR / "failed_write.txt").exists()
    monkeypatch.setattr(notify, "send_telegram_alert", lambda m: True)
    assert notify.alert_once_per_day("failed_write", "message") is True


def test_preanalysis_does_not_require_periodic_probe_but_incomplete_deployment_blocks(monkeypatch):
    monkeypatch.setenv("SIMA_COLLECTOR_AUTOREPAIR", "1")
    assert repair.preanalysis_problem(AT) is None
    value = good_report()
    value["checked_at"] = (AT - timedelta(days=3)).isoformat()
    value["manifest"] = "old-version"
    value["sources"]["index"]["status"] = "stale"
    repair.atomic_json(repair.STATE_DIR / "latest.json", value)
    assert repair.preanalysis_problem(AT) is None
    repair.atomic_json(repair.STATE_DIR / "transaction.json", {"status": "publishing"})
    assert repair.preanalysis_problem(AT) == "collector_deployment_incomplete"


def test_optional_news_failure_is_visible_without_mandatory_block(monkeypatch):
    monkeypatch.setenv("SIMA_COLLECTOR_AUTOREPAIR", "1")
    value = good_report()
    value["sources"]["stock_news"]["status"] = "structure"
    repair.atomic_json(repair.STATE_DIR / "latest.json", value)
    assert repair.preanalysis_problem(AT) is None


@pytest.mark.parametrize("hour,minute,allowed", [(0, 0, True), (6, 39, True), (6, 40, False), (6, 55, False), (7, 0, False), (8, 14, False), (9, 0, False), (15, 30, False), (16, 9, False), (16, 10, True), (23, 59, True)])
def test_repair_deadline(hour, minute, allowed):
    assert repair.repair_window(AT.replace(hour=hour, minute=minute)) is allowed


def test_no_repair_after_analysis_began(monkeypatch):
    monkeypatch.setattr(repair.run_history, "already_analyzed", lambda *a: True)
    with pytest.raises(RuntimeError, match="analysis already started"):
        repair.require_window()


def test_initial_failure_can_repair_without_clean_day_and_transients_never_invoke_model(monkeypatch):
    monkeypatch.setenv("SIMA_COLLECTOR_AUTOREPAIR", "1")
    value = good_report()
    value["sources"]["index"] = {"status": "structure", "error": "410"}
    monkeypatch.setattr(health, "probe", lambda *a, **k: value)
    monkeypatch.setattr(notify, "send_telegram_alert", lambda m: True)
    attempted = []
    def fail_repair(*args):
        attempted.append(True)
        raise RuntimeError("candidate still must pass validation")
    monkeypatch.setattr(repair, "repair", fail_repair)
    repair.check(allow_repair=True)
    state = repair.read_state()
    assert state["attempts"] == {}
    assert attempted == [True]
    assert state["shadow_days"] == []
    value["sources"]["index"]["status"] = "transient"
    repair.check(allow_repair=True)
    assert repair.read_state()["attempts"] == {}
    assert attempted == [True]


def test_watchdog_does_not_alert_about_intentionally_idle_worker(monkeypatch):
    monkeypatch.setattr(repair, "now", lambda: AT.replace(hour=6, minute=48))
    sent = []
    monkeypatch.setattr(notify, "send_telegram_alert", lambda m: sent.append(m) or True)
    state = repair.read_state()
    state["completed_at"] = AT.isoformat()
    repair.atomic_json(repair.STATE_DIR / "state.json", state)
    before = (repair.STATE_DIR / "state.json").read_bytes()
    repair.watchdog()
    assert sent == []
    assert before == (repair.STATE_DIR / "state.json").read_bytes()


def test_daily_repair_budget_is_persisted_before_model(monkeypatch):
    state = repair.read_state()
    state["attempts"][AT.date().isoformat()] = repair.MAX_ATTEMPTS
    with pytest.raises(RuntimeError, match="budget"):
        repair.repair(state, routes.default_manifest(), good_report())


def test_proposal_cannot_change_a_healthy_route(monkeypatch):
    async def capacity():
        return SimpleNamespace(buy_judgment_allowed=True)
    monkeypatch.setattr(repair.codex_plan, "read_capacity", capacity)
    monkeypatch.setattr(repair, "git", lambda *a, **k: "" if a[0] in {"status", "fetch"} else "base")
    candidate = routes.default_manifest()
    candidate["routes"]["quotes"]["url"] = "https://stock.naver.com/api/quotes/{codes}"
    monkeypatch.setattr(repair, "model_call", lambda *a, **k: repair.Proposal(supported=True, manifest_json=json.dumps(candidate), reason="test"))
    report = good_report()
    report["sources"]["index"] = {"status": "structure", "error": "410"}
    with pytest.raises(RuntimeError, match="healthy or unrelated"):
        repair.repair(repair.read_state(), routes.default_manifest(), report)
    assert repair.read_state()["attempts"][repair.attempt_window(AT)] == 1


def test_identical_aggregate_news_cannot_replace_ticker_feeds():
    feeds = {f"news-{i}": {"title": f"headline-{i}"} for i in range(10)}
    old = {"stock_news": {"status": "ok", "data": {"005930": feeds, "000660": feeds}}}
    candidate = good_report()
    candidate["sources"]["stock_news"] = old["stock_news"]
    with pytest.raises(ValueError, match="identical feeds"):
        health.validate_compatibility(old, candidate, {"stock_news"})


@pytest.fixture
def transaction_fixture(monkeypatch, tmp_path):
    current = routes.default_manifest()
    candidate = copy.deepcopy(current)
    candidate["routes"]["stock_news"]["url"] = 'https://m.stock.naver.com/api/v2/news/stock/{ticker}'
    checked = good_report()
    checked["manifest"] = routes.digest(candidate)
    checked["sources"]["stock_news"]["data"] = {
        code: {f"{code}-{i}": {"title": f"title-{i}", "published_at": AT.isoformat()} for i in range(5)}
        for code in health.CANARIES}
    state = repair.read_state()
    state["baseline"] = copy.deepcopy(checked["sources"])
    failed = good_report()
    failed["sources"]["stock_news"] = {"status": "structure", "error": "410"}
    actions = []
    refs = {"remote": "base", "local": "base"}

    def git(*args, **kwargs):
        actions.append(args)
        if args[0] == 'rev-parse':
            return refs['remote'] if args[1] == 'origin/main' else refs['local']
        if args[0] == 'push':
            refs['remote'] = args[2].split(':')[0]
        if args[0] == 'diff':
            return 'fixture diff'
        return ''

    async def capacity():
        return SimpleNamespace(buy_judgment_allowed=True)

    def model(prompt, data, response, **kwargs):
        if response is repair.Proposal:
            return repair.Proposal(supported=True, manifest_json=json.dumps(candidate), reason='published replacement')
        return repair.Review(approved=True, commit=data['commit'], manifest_sha256=data['manifest_sha256'], reason='reviewed')

    monkeypatch.setattr(repair, 'ROOT', tmp_path / 'repo')
    repair.ROOT.mkdir()
    monkeypatch.setattr(repair, 'git', git)
    monkeypatch.setattr(repair.codex_plan, 'read_capacity', capacity)
    monkeypatch.setattr(repair, 'model_call', model)
    monkeypatch.setattr(repair, 'build_commit', lambda *a, rollback=False: 'rollback' if rollback else 'candidate')
    monkeypatch.setattr(repair, 'run_tests', lambda *a: 'passed')
    monkeypatch.setattr(health, 'probe', lambda *a, **k: copy.deepcopy(checked))
    monkeypatch.setattr(notify, 'send_telegram_alert', lambda m: True)
    monkeypatch.setattr(repair, 'deploy', lambda commit: refs.update(local=commit))
    return state, current, failed, refs, actions


def test_repair_commit_review_deploy_transaction(monkeypatch, transaction_fixture):
    state, current, failed, refs, actions = transaction_fixture
    result = repair.repair(state, current, failed)
    assert refs == {'remote': 'candidate', 'local': 'candidate'}
    assert result['sources']['stock_news']['status'] == 'ok'
    assert repair.transaction_blocked() is False
    assert any(a == ('push', 'origin', 'candidate:refs/heads/main') for a in actions)


def test_review_hash_mismatch_prevents_any_push(monkeypatch, transaction_fixture):
    state, current, failed, refs, actions = transaction_fixture
    original = repair.model_call
    def model(prompt, data, response, **kwargs):
        result = original(prompt, data, response, **kwargs)
        if isinstance(result, repair.Review):
            result.commit = 'unreviewed'
        return result
    monkeypatch.setattr(repair, 'model_call', model)
    with pytest.raises(RuntimeError, match='review rejected'):
        repair.repair(state, current, failed)
    assert not any(a[0] == 'push' for a in actions)


def test_deployment_failure_restores_remote_and_local(monkeypatch, transaction_fixture):
    state, current, failed, refs, actions = transaction_fixture
    def deploy(commit):
        if commit == 'candidate':
            raise RuntimeError('deployment failed')
        refs['local'] = commit
    monkeypatch.setattr(repair, 'deploy', deploy)
    with pytest.raises(RuntimeError, match='deployment failed'):
        repair.repair(state, current, failed)
    assert refs == {'remote': 'rollback', 'local': 'rollback'}
    assert repair.transaction_blocked() is False


def test_lost_push_response_is_reconciled_before_rollback(monkeypatch, transaction_fixture):
    state, current, failed, refs, actions = transaction_fixture
    original = repair.git
    def git(*args, **kwargs):
        value = original(*args, **kwargs)
        if args[:3] == ('push', 'origin', 'candidate:refs/heads/main'):
            raise RuntimeError('push response lost')
        return value
    monkeypatch.setattr(repair, 'git', git)
    with pytest.raises(RuntimeError, match='push response lost'):
        repair.repair(state, current, failed)
    assert refs == {'remote': 'rollback', 'local': 'rollback'}
    assert repair.transaction_blocked() is False


def test_failed_rollback_leaves_durable_buy_block(monkeypatch, transaction_fixture):
    state, current, failed, refs, actions = transaction_fixture
    monkeypatch.setattr(repair, 'deploy', lambda commit: (_ for _ in ()).throw(RuntimeError('offline')))
    with pytest.raises(RuntimeError):
        repair.repair(state, current, failed)
    assert repair.transaction_blocked() is True
    monkeypatch.setenv('SIMA_COLLECTOR_AUTOREPAIR', '1')
    assert repair.preanalysis_problem(AT) == 'collector_deployment_incomplete'


def test_shadow_requires_same_revision_premarket_and_close(monkeypatch):
    value = good_report()
    monkeypatch.setattr(health, 'probe', lambda *a, **k: value)
    monkeypatch.setattr(notify, 'send_telegram_alert', lambda m: True)
    monkeypatch.setattr(repair, 'now', lambda: AT.replace(hour=16, minute=20))
    repair.check(full=True)
    assert repair.read_state()['shadow_days'] == []
    monkeypatch.setattr(repair, 'now', lambda: AT)
    repair.check(full=True)
    monkeypatch.setattr(repair, 'now', lambda: AT.replace(hour=16, minute=20))
    repair.check(full=True)
    assert repair.read_state()['shadow_days'] == ['2026-10-19']


def test_recent_completion_does_not_trigger_watchdog(monkeypatch):
    monkeypatch.setattr(repair, 'now', lambda: AT.replace(hour=6, minute=48))
    sent = []
    monkeypatch.setattr(notify, 'send_telegram_alert', lambda m: sent.append(m) or True)
    state = repair.read_state()
    state['completed_at'] = AT.replace(hour=6, minute=40, second=30).isoformat()
    repair.atomic_json(repair.STATE_DIR / 'state.json', state)
    repair.watchdog()
    assert sent == []


def test_actual_pipeline_index_failure_blocks_before_stock_analysis(monkeypatch):
    import asyncio
    from src import pipeline, collectors
    from src.schemas import DataCollectionUnavailable
    monkeypatch.setenv('SIMA_COLLECTOR_AUTOREPAIR', '1')
    monkeypatch.setattr(collectors, 'fetch_kospi200_index_bars', lambda *a: None)
    with pytest.raises(DataCollectionUnavailable, match='index_input_unavailable'):
        asyncio.run(pipeline.quant_prefilter([('005930', '반도체')]))


def test_daytime_queue_survives_restart_and_repairs_after_close(monkeypatch):
    monkeypatch.setenv('SIMA_COLLECTOR_AUTOREPAIR', '1')
    state = repair.read_state()
    state['shadow_days'] = ['2026-10-16']
    state['attempts'][repair.attempt_window(AT)] = 2
    repair.atomic_json(repair.STATE_DIR / 'state.json', state)
    broken = good_report()
    broken['sources']['stock_news'] = {'status': 'structure', 'error': 'HTTP 410'}
    monkeypatch.setattr(health, 'probe', lambda *a, **k: copy.deepcopy(broken))
    sent, calls = [], []
    monkeypatch.setattr(notify, 'send_telegram_alert', lambda m: sent.append(m) or True)
    monkeypatch.setattr(repair, 'repair', lambda *a: calls.append(a) or good_report())
    monkeypatch.setattr(repair.run_history, 'already_analyzed', lambda *a: True)
    for hour, minute in [(10, 0), (15, 30), (16, 9)]:
        monkeypatch.setattr(repair, 'now', lambda: AT.replace(hour=hour, minute=minute))
        repair.check(allow_repair=True, monitoring=True)
        assert repair.read_state()['pending_repairs']['stock_news']['status'] == 'waiting_close'
        assert calls == []
    assert any('16:10' in m for m in sent)
    monkeypatch.setattr(repair, 'now', lambda: AT.replace(hour=16, minute=10))
    repair.check(allow_repair=True, monitoring=True)
    assert len(calls) == 1
    assert repair.read_state()['pending_repairs'] == {}
    assert any('자동 수리·검증·배포를 시작' in m for m in sent)


def test_queued_incident_recovers_without_needless_repair(monkeypatch):
    monkeypatch.setattr(repair, 'now', lambda: AT.replace(hour=10))
    value = good_report()
    value['sources']['index'] = {'status': 'structure', 'error': 'HTTP 404'}
    monkeypatch.setattr(health, 'probe', lambda *a, **k: copy.deepcopy(value))
    sent = []
    monkeypatch.setattr(notify, 'send_telegram_alert', lambda m: sent.append(m) or True)
    repair.check(allow_repair=True, monitoring=True)
    value = good_report()
    repair.check(allow_repair=True, monitoring=True)
    assert repair.read_state()['pending_repairs'] == {}
    assert any('복구' in m for m in sent)
    # A recurrence on the same day must alert immediately again.
    size = len(sent)
    value['sources']['index'] = {'status': 'structure', 'error': 'HTTP 404'}
    repair.check(allow_repair=True, monitoring=True)
    assert len(sent) == size + 1


def test_failed_night_repair_cooldown_then_session_budget(monkeypatch):
    monkeypatch.setenv('SIMA_COLLECTOR_AUTOREPAIR', '1')
    state = repair.read_state()
    state['shadow_days'] = ['2026-10-16']
    repair.atomic_json(repair.STATE_DIR / 'state.json', state)
    value = good_report()
    value['sources']['index'] = {'status': 'structure', 'error': '410'}
    monkeypatch.setattr(health, 'probe', lambda *a, **k: copy.deepcopy(value))
    monkeypatch.setattr(notify, 'send_telegram_alert', lambda m: True)
    calls = []
    def failed(state, *args):
        slot = repair.attempt_window()
        state['attempts'][slot] = state['attempts'].get(slot, 0) + 1
        calls.append(slot)
        raise RuntimeError('candidate validation failed')
    monkeypatch.setattr(repair, 'repair', failed)
    for minute in (0, 5, 30, 35):
        monkeypatch.setattr(repair, 'now', lambda: AT.replace(hour=20, minute=minute))
        repair.check(allow_repair=True, monitoring=True)
    assert len(calls) == 2
    state = repair.read_state()
    assert state['pending_repairs']['index']['status'] == 'budget'
    assert state['incidents']['repair']['open'] is True


@pytest.mark.parametrize('day,hour,minute', [(19, 16, 10), (19, 23, 55), (17, 10, 0)])
def test_afterclose_and_weekend_commit_deploy_despite_morning_history(monkeypatch, transaction_fixture, day, hour, minute):
    monkeypatch.setattr(repair, 'now', lambda: AT.replace(day=day, hour=hour, minute=minute))
    monkeypatch.setattr(repair.run_history, 'already_analyzed', lambda *a: True)
    state, current, failed, refs, actions = transaction_fixture
    repair.repair(state, current, failed)
    assert refs == {'local': 'candidate', 'remote': 'candidate'}
    assert repair.budget(1) > 0


def test_rollback_reserve_closes_before_frozen_deployment_window(monkeypatch):
    monkeypatch.setattr(repair, 'now', lambda: AT.replace(hour=6, minute=47))
    with pytest.raises(RuntimeError):
        repair.require_publish_window()
    repair.require_publish_window(rollback=True)
    monkeypatch.setattr(repair, 'now', lambda: AT.replace(hour=6, minute=48))
    with pytest.raises(RuntimeError):
        repair.require_publish_window(rollback=True)


def test_interrupted_deployment_does_not_prevent_intraday_monitoring(monkeypatch):
    monkeypatch.setattr(repair, 'now', lambda: AT.replace(hour=10))
    repair.atomic_json(repair.STATE_DIR / 'transaction.json', {'status': 'publishing'})
    monkeypatch.setattr(repair, 'recover_transaction', lambda: pytest.fail('intraday mutation'))
    calls = []
    monkeypatch.setattr(health, 'probe', lambda *a, **k: calls.append(k) or good_report())
    monkeypatch.setattr(notify, 'send_telegram_alert', lambda m: True)
    repair.check(allow_repair=True, monitoring=True)
    assert len(calls) == 1
    assert repair.transaction_blocked()
    assert repair.read_state()['incidents']['deployment']['phase'] == 'waiting_close'


def test_intraday_monitor_removes_current_index_bar_but_actual_input_rejects_it():
    xml = index_xml().replace('</chartdata>', '<item data="20261019|100|102|99|101|2000"/></chartdata>')
    at = AT.replace(hour=10)
    assert len(health.index_data(xml, at, monitoring=True)) == 60
    with pytest.raises(health.ProbeError, match='last session'):
        health.index_data(xml, at)
    assert len(health.index_data(xml, AT.replace(hour=16))) == 61


def test_intraday_monitor_rejects_frozen_krx_quotes():
    at = AT.replace(hour=10)
    rows = [{'itemCode': code, 'stockExchangeType': {'code': 'KS'},
             'closePriceRaw': 100000, 'localTradedAt': at.isoformat()} for code in health.CANARIES]
    assert len(health.quotes_data(json.dumps({'datas': rows}), at, monitoring=True)) == 2
    rows[0]['localTradedAt'] = (at - timedelta(minutes=6)).isoformat()
    with pytest.raises(health.ProbeError, match='five-minute'):
        health.quotes_data(json.dumps({'datas': rows}), at, monitoring=True)


@pytest.mark.parametrize('running,age,problem', [(False, 11, False), (False, 5, False), (True, 19, False), (True, 21, True)])
def test_watchdog_works_at_night_with_bounded_worker_grace(monkeypatch, running, age, problem):
    at = AT.replace(day=17, hour=23)
    monkeypatch.setattr(repair, 'now', lambda: at)
    sent = []
    monkeypatch.setattr(notify, 'send_telegram_alert', lambda m: sent.append(m) or True)
    state = repair.read_state()
    state['completed_at'] = (at - timedelta(minutes=age + (5 if running else 0))).isoformat()
    if running:
        state['started_at'] = (at - timedelta(minutes=age)).isoformat()
    repair.atomic_json(repair.STATE_DIR / 'state.json', state)
    repair.watchdog()
    assert bool(sent) is problem


def test_phase_change_and_failed_enqueue_do_not_lose_notifications(monkeypatch):
    state = repair.read_state()
    sent = []
    monkeypatch.setattr(notify, 'send_telegram_alert', lambda m: sent.append(m) or True)
    repair.report(state, 'index', True, 'queued', phase='waiting_close')
    repair.report(state, 'index', True, 'queued', phase='waiting_close')
    assert len(sent) == 1
    monkeypatch.setattr(notify, 'send_telegram_alert', lambda m: False)
    repair.report(state, 'index', True, 'repairing', phase='ready')
    assert state['incidents']['index']['phase'] == 'waiting_close'
    monkeypatch.setattr(notify, 'send_telegram_alert', lambda m: sent.append(m) or True)
    repair.report(state, 'index', True, 'repairing', phase='ready')
    assert len(sent) == 2


def test_controller_error_recovery_and_recurrence_notifications(monkeypatch):
    sent = []
    monkeypatch.setattr(notify, 'send_telegram_alert', lambda m: sent.append(m) or True)
    for error in ('exception', 'exception', None, 'exception'):
        repair.controller_status('worker_process', error)
    assert len(sent) == 3


def test_empty_pending_queue_never_probes_models_or_sends_health_alerts(monkeypatch):
    monkeypatch.setattr(health, 'probe', lambda *a, **k: pytest.fail('unsolicited probe'))
    monkeypatch.setattr(notify, 'send_telegram_alert', lambda *a: pytest.fail('normal health alert'))
    # Old periodic monitoring state must not create artificial jobs or stale alerts.
    state = repair.read_state()
    state['completed_at'] = (AT - timedelta(days=3)).isoformat()
    state['pending_repairs'] = {'index': {'status': 'waiting_close'}}
    repair.atomic_json(repair.STATE_DIR / 'state.json', state)
    assert repair.process_pending() is None


def test_actual_failed_fetch_queues_exact_request_and_matching_success_closes_it(monkeypatch):
    from src import collectors
    monkeypatch.setenv('SIMA_COLLECTOR_AUTOREPAIR', '1')
    monkeypatch.setattr(collectors.time, 'sleep', lambda *a: None)
    sent = []
    monkeypatch.setattr(notify, 'send_telegram_alert', lambda m: sent.append(m) or True)
    monkeypatch.setattr(requests, 'get', lambda *a, **k: (_ for _ in ()).throw(requests.Timeout()))
    assert collectors.fetch_company_news('005930') is None
    entry = repair.read_workflow()['requests']['stock_news']
    assert list(entry['samples'].values())[0]['slots'] == {'ticker': '005930'}
    assert len(sent) == 1
    # A different ticker's success must not close this incident; empty news is valid.
    monkeypatch.setattr(requests, 'get', lambda *a, **k: SimpleNamespace(text='[]', raise_for_status=lambda: None))
    assert collectors.fetch_company_news('000660') == []
    assert 'stock_news' in repair.read_workflow()['requests']
    assert collectors.fetch_company_news('005930') == []
    assert repair.read_workflow()['requests'] == {}
    assert len(sent) == 2


def test_failed_quote_and_stale_sector_cache_refresh_are_recorded(monkeypatch):
    from src import collectors
    monkeypatch.setenv('SIMA_COLLECTOR_AUTOREPAIR', '1')
    monkeypatch.setattr(notify, 'send_telegram_alert', lambda m: True)
    monkeypatch.setattr(requests, 'get', lambda *a, **k: SimpleNamespace(json=lambda: {'unexpected': []}))
    assert REAL_NAVER_QUOTES(['005930']) == {}
    monkeypatch.setattr(collectors, '_read_sector_cache', lambda: ({'005930': '반도체'}, AT - timedelta(days=90)))
    monkeypatch.setattr(collectors, '_fetch_sector_map_live', lambda: None)
    assert collectors.fetch_kospi200_sector_map() == {'005930': '반도체'}
    assert set(repair.read_workflow()['requests']) == {'quotes', 'sector_groups'}


def test_queue_write_failure_does_not_break_collection_failure_contract(monkeypatch):
    from src import collectors
    monkeypatch.setenv('SIMA_COLLECTOR_AUTOREPAIR', '1')
    monkeypatch.setattr(repair, 'request_repair', lambda *a, **k: (_ for _ in ()).throw(OSError('disk full')))
    monkeypatch.setattr(requests, 'get', lambda *a, **k: (_ for _ in ()).throw(requests.Timeout()))
    monkeypatch.setattr(collectors.time, 'sleep', lambda *a: None)
    assert collectors.fetch_company_news('005930') is None


def test_real_success_refreshes_baseline_without_another_request(monkeypatch):
    from src import collectors
    monkeypatch.setenv('SIMA_COLLECTOR_AUTOREPAIR', '1')
    monkeypatch.setattr(notify, 'send_telegram_alert', lambda *a: pytest.fail('normal alert'))
    monkeypatch.setattr(health, 'probe', lambda *a, **k: pytest.fail('extra HTTP probe'))
    calls = []
    def get(*args, **kwargs):
        calls.append(args)
        return SimpleNamespace(text=index_xml(), raise_for_status=lambda: None)
    monkeypatch.setattr(requests, 'get', get)
    # Actual input validates with wall clock, fixed to this fixture here.
    monkeypatch.setattr(collectors, 'datetime', type('FixedTime', (datetime,), {'now': classmethod(lambda cls, tz=None: AT)}))
    assert len(collectors.fetch_kospi200_index_bars()) == 60
    baseline = repair.read_workflow()['baseline']['index']
    assert baseline['status'] == 'ok' and len(baseline['data']) == 60
    assert len(calls) == 1


def test_canary_success_does_not_claim_original_request_recovered_or_repeat_probe(monkeypatch):
    monkeypatch.setenv('SIMA_COLLECTOR_AUTOREPAIR', '1')
    monkeypatch.setattr(repair, 'now', lambda: AT.replace(hour=16, minute=10))
    monkeypatch.setattr(notify, 'send_telegram_alert', lambda m: True)
    state = repair.read_state()
    state['shadow_days'] = ['2026-10-16']
    repair.atomic_json(repair.STATE_DIR / 'state.json', state)
    repair.request_repair('stock_news', 'specific ticker failed', slots={'ticker': '123456'})
    calls = []
    monkeypatch.setattr(repair, 'check', lambda **k: calls.append(k) or good_report())
    repair.process_pending()
    assert repair.read_workflow()['requests']['stock_news']['diagnosed_generation']
    assert repair.read_workflow()['incidents']['stock_news']['open']
    repair.process_pending()
    assert len(calls) == 1
    repair.request_repair('stock_news', 'same ticker failed again', slots={'ticker': '123456'})
    repair.process_pending()
    assert len(calls) == 2


def test_diagnosis_preserves_new_failure_and_member_failure(monkeypatch):
    monkeypatch.setenv('SIMA_COLLECTOR_AUTOREPAIR', '1')
    monkeypatch.setattr(repair, 'now', lambda: AT.replace(hour=16, minute=10))
    monkeypatch.setattr(notify, 'send_telegram_alert', lambda m: True)
    state = repair.read_state()
    state['shadow_days'] = ['2026-10-16']
    repair.atomic_json(repair.STATE_DIR / 'state.json', state)
    repair.request_repair('sector_groups', 'map refresh failed')
    value = good_report()
    value['sources']['sector_members'] = {'status': 'structure', 'error': 'wrong members'}
    monkeypatch.setattr(repair, 'check', lambda **k: value)
    repair.process_pending()
    assert 'diagnosed_generation' not in repair.read_workflow()['requests']['sector_groups']
    def racing_check(**kwargs):
        repair.request_repair('sector_groups', 'new incident during diagnosis')
        return good_report()
    monkeypatch.setattr(repair, 'check', racing_check)
    repair.process_pending()
    assert 'diagnosed_generation' not in repair.read_workflow()['requests']['sector_groups']


@pytest.mark.parametrize('healthy_pre,healthy_close,failed_midday,ready', [(True, True, False, True), (False, True, False, False), (True, False, False, False), (True, True, True, False)])
def test_passive_shadow_requires_normal_workflow_and_close_without_extra_probes(monkeypatch, healthy_pre, healthy_close, failed_midday, ready):
    monkeypatch.setenv('SIMA_COLLECTOR_AUTOREPAIR', '1')
    monkeypatch.setattr(health, 'probe', lambda *a, **k: pytest.fail('extra shadow probe'))
    monkeypatch.setattr(notify, 'send_telegram_alert', lambda m: True)
    monkeypatch.setattr(repair, 'now', lambda: AT.replace(hour=8, minute=50))
    repair.workflow_finished('premarket', healthy=healthy_pre)
    if failed_midday:
        repair.request_repair('quotes', 'workflow failure')
    monkeypatch.setattr(repair, 'now', lambda: AT.replace(hour=16, minute=2))
    repair.workflow_finished('afterclose', healthy=healthy_close)
    assert repair.shadow_ready(repair.read_state(), AT.replace(day=20)) is ready
    assert not repair.shadow_ready(repair.read_state(), AT.replace(hour=20))


def test_passive_shadow_rejects_changed_manifest(monkeypatch):
    monkeypatch.setenv('SIMA_COLLECTOR_AUTOREPAIR', '1')
    monkeypatch.setattr(repair, 'now', lambda: AT.replace(hour=8, minute=50))
    repair.workflow_finished('premarket', healthy=True)
    candidate = routes.default_manifest()
    candidate['routes']['stock_news']['url'] = 'https://m.stock.naver.com/api/v2/news/stock/{ticker}'
    monkeypatch.setattr(routes, 'load', lambda: candidate)
    monkeypatch.setattr(repair, 'now', lambda: AT.replace(hour=16))
    repair.workflow_finished('afterclose', healthy=True)
    assert not repair.shadow_ready(repair.read_state(), AT.replace(day=20))


def test_same_problem_has_no_periodic_status_alerts(monkeypatch):
    state, sent = repair.read_state(), []
    monkeypatch.setattr(notify, 'send_telegram_alert', lambda m: sent.append(m) or True)
    for minute in (0, 5, 15, 45):
        repair.report(state, 'index', True, 'waiting', phase='waiting_close', at=AT + timedelta(minutes=minute))
    assert len(sent) == 1


@pytest.mark.parametrize('allowed', [True, False])
def test_codex_preparation_only_notifies_when_blocked(monkeypatch, allowed):
    import asyncio
    from scripts import check_codex_plan as script
    monkeypatch.setattr(script, 'datetime', type('FixedTime', (datetime,), {'now': classmethod(lambda cls, tz=None: AT)}))
    capacity = SimpleNamespace(buy_judgment_allowed=allowed, remaining_percent=50 if allowed else 1,
                               resets_at=AT, model_dump=lambda **k: {})
    async def health_ok():
        return SimpleNamespace(status='ok')
    async def capacity_ok():
        return capacity
    monkeypatch.setattr(script.codex_plan, 'check_health', health_ok)
    monkeypatch.setattr(script.codex_plan, 'read_capacity', capacity_ok)
    monkeypatch.setattr(script.codex_plan, 'save_capacity_status', lambda c: None)
    monkeypatch.setattr(notify, 'format_codex_capacity_alert', lambda c: 'blocked')
    sent = []
    monkeypatch.setattr(notify, 'send_telegram_alert', lambda m: sent.append(m) or True)
    asyncio.run(script.main())
    assert sent == ([] if allowed else ['blocked'])


def test_sector_member_validation_failure_is_queued_with_group(monkeypatch):
    from src import collectors
    monkeypatch.setenv('SIMA_COLLECTOR_AUTOREPAIR', '1')
    monkeypatch.setattr(notify, 'send_telegram_alert', lambda m: True)
    replies = iter([[{'no': '42', 'name': '반도체', 'type': 'upjong', 'totalCnt': 1}], {'items': [], 'hasNext': True}])
    monkeypatch.setattr(collectors, '_fetch_with_retries', lambda *a, **k: next(replies))
    assert collectors._fetch_sector_map_json() is None
    entry = repair.read_workflow()['requests']['sector_members']
    assert list(entry['samples'].values())[0]['slots'] == {'group': '42'}


def test_parallel_failure_writes_preserve_all_request_samples(monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    monkeypatch.setattr(notify, 'send_telegram_alert', lambda m: True)
    with ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(lambda n: repair.request_repair('stock_news', 'timeout', slots={'ticker': f'{n:06d}'}), range(12)))
    assert len(repair.read_workflow()['requests']['stock_news']['samples']) == 12


def test_failed_recovery_notice_retries_delivery_without_reprobing_healthy_source(monkeypatch):
    monkeypatch.setattr(notify, 'send_telegram_alert', lambda m: True)
    repair.request_repair('stock_news', 'failed', slots={'ticker': '123456'})
    monkeypatch.setattr(notify, 'send_telegram_alert', lambda m: False)
    repair.source_succeeded('stock_news', '[]', slots={'ticker': '123456'})
    assert repair.read_workflow()['requests']['stock_news']['samples'] == {}
    monkeypatch.setattr(repair, 'check', lambda **k: pytest.fail('healthy source re-probed'))
    repair.process_pending()
    assert repair.read_workflow()['requests']
    sent = []
    monkeypatch.setattr(notify, 'send_telegram_alert', lambda m: sent.append(m) or True)
    repair.process_pending()
    assert len(sent) == 1 and '복구' in sent[0]
    assert repair.read_workflow()['requests'] == {}


@pytest.mark.parametrize('hour,minute,recorded', [(6, 59, False), (7, 0, True), (7, 45, True), (8, 55, True), (9, 0, False)])
def test_passive_completion_observes_earlier_analysis(monkeypatch, hour, minute, recorded):
    monkeypatch.setenv('SIMA_COLLECTOR_AUTOREPAIR', '1')
    monkeypatch.setattr(repair, 'now', lambda: AT.replace(hour=hour, minute=minute))
    repair.workflow_finished('premarket', healthy=True)
    assert ('premarket' in repair.read_workflow()['days'][AT.date().isoformat()]) is recorded


def test_pending_incident_runs_repair_chain_and_requires_original_request_success(monkeypatch, transaction_fixture):
    state, current, failed, refs, actions = transaction_fixture
    monkeypatch.setenv('SIMA_COLLECTOR_AUTOREPAIR', '1')
    monkeypatch.setattr(repair, 'now', lambda: AT.replace(hour=16, minute=10))
    repair.atomic_json(repair.STATE_DIR / 'state.json', state)
    healthy_probe = health.probe
    probes = []
    def probe(*args, **kwargs):
        probes.append(1)
        return copy.deepcopy(failed) if len(probes) == 1 else healthy_probe(*args, **kwargs)
    monkeypatch.setattr(health, 'probe', probe)
    repair.request_repair('stock_news', 'HTTP 410', slots={'ticker': '005930'})
    repair.process_pending()
    assert len(probes) == 3  # initial incident, candidate, deployed candidate
    assert refs == {'remote': 'candidate', 'local': 'candidate'}
    assert repair.read_workflow()['incidents']['stock_news']['open'] is True
    assert repair.read_workflow()['requests']['stock_news']['diagnosed_generation']
    repair.process_pending()
    assert len(probes) == 3  # no repeated analysis or probe of diagnosed generation
    repair.source_succeeded('stock_news', slots={'ticker': '005930'})
    assert repair.read_workflow()['requests'] == {}
    assert repair.read_workflow()['incidents']['stock_news']['open'] is False


def test_upstream_mismatch_prevents_automatic_repair_publish(monkeypatch, transaction_fixture):
    state, current, failed, refs, actions = transaction_fixture
    refs['remote'] = 'documentation_commit'
    monkeypatch.setattr(repair, 'model_call', lambda *a, **k: pytest.fail('model must not run on stale code'))
    with pytest.raises(RuntimeError, match='upstream moved'):
        repair.repair(state, current, failed)
    assert not any(a[0] == 'push' for a in actions)
