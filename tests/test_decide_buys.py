"""scripts/decide_buys.py — 07:00 판단 전용 진입점. pipeline.run_daily 내부
로직(유니버스/필터/분석가/토론)은 이미 각자 테스트에서 검증되므로, 여기서는
"판단 결과를 실행하지 않고 pending_buys.json에 정확히 남기는가"만 본다 —
pipeline.run_daily 자체를 가짜로 바꿔치기해서 그 안에 전달되는 execute_fn을
직접 호출해본다 (tests/test_run_daily.py와 같은 방식).
"""

import asyncio
import json
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest

import scripts.decide_buys as db
from src.schemas import AnalystOpinion, Decision, GateResult, PortfolioState


@pytest.fixture(autouse=True)
def _no_real_notify_or_name_lookup(monkeypatch):
    # main()이 이제 판단 결과 요약(0건 포함)을 텔레그램으로 보낸다 — 목킹 안 하면
    # 테스트가 실제 텔레그램 메시지를 보내고 실제 네이버를 긁는다(2026-08-09,
    # execute_buy_order 쪽에서 실수로 한 번 겪은 것과 같은 함정, 2026-08-11 재발).
    monkeypatch.setattr(db.notify, "send_telegram_alert", lambda message: True)
    monkeypatch.setattr(db.pipeline, "display_name", lambda ticker: ticker)


def _decision(ticker="005930", action="BUY") -> Decision:
    return Decision(
        ticker=ticker,
        action=action,
        reason="test",
        inputs=[AnalystOpinion(agent="chart", ticker=ticker, score=0.9, confidence=0.9, evidence=["e"], as_of=date(2026, 8, 11))],
        degraded=False,
    )


def test_recording_execute_fn_records_approved_buys_only():
    recorded: list[dict] = []
    record_fn = db._make_recording_execute_fn(recorded)
    portfolio = PortfolioState(cash_weight=1.0)

    approved = _decision("005930", "BUY")
    rejected = _decision("000660", "BUY")
    hold = _decision("035420", "HOLD")

    result1 = asyncio.run(record_fn(approved, GateResult(approved=True, rejected_by=None), portfolio, "반도체", 0.08))
    result2 = asyncio.run(record_fn(rejected, GateResult(approved=False, rejected_by="position_limit"), portfolio, "반도체", 0.08))
    result3 = asyncio.run(record_fn(hold, GateResult(approved=False, rejected_by=None), portfolio, "화학", 0.08))

    # 포트폴리오는 절대 안 바뀐다 — 이 단계는 집행이 아니라 판단만 한다.
    assert result1 == portfolio
    assert result2 == portfolio
    assert result3 == portfolio

    assert len(recorded) == 1
    assert recorded[0]["ticker"] == "005930"
    assert recorded[0]["sector"] == "반도체"
    assert recorded[0]["trade_weight"] == 0.08
    assert recorded[0]["decision"]["action"] == "BUY"
    assert recorded[0]["gate_result"]["approved"] is True


def test_main_writes_pending_buys_json_from_run_daily_results(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(db, "is_krx_trading_day", lambda day: True)

    decision, gate = _decision("005930", "BUY"), GateResult(approved=True, rejected_by=None)

    async def fake_run_daily(day, portfolio, config, analyst_fn, judge_fn, execute_fn, total_expected_analysts):
        # decide_buys.main()이 넘긴 execute_fn을 실제로 한 번 불러서 기록시킨다 —
        # run_day 내부 루프가 하는 일을 그대로 흉내낸다.
        await execute_fn(decision, gate, portfolio, "반도체", 0.08)
        return portfolio, [(decision, gate)]

    monkeypatch.setattr(db.pipeline, "run_daily", fake_run_daily)

    asyncio.run(db.main())

    payload = json.loads(db.PENDING_BUYS_PATH.read_text())
    assert payload["decisions"][0]["ticker"] == "005930"
    assert len(payload["decisions"]) == 1


def test_main_skips_on_non_trading_day(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(db, "is_krx_trading_day", lambda day: False)

    def fail(*a, **k):
        raise AssertionError("휴장일엔 run_daily가 호출되면 안 된다")

    monkeypatch.setattr(db.pipeline, "run_daily", fail)

    asyncio.run(db.main())

    assert not db.PENDING_BUYS_PATH.exists()


def test_main_writes_empty_decisions_when_nothing_approved(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(db, "is_krx_trading_day", lambda day: True)

    async def fake_run_daily(day, portfolio, config, analyst_fn, judge_fn, execute_fn, total_expected_analysts):
        return portfolio, []

    monkeypatch.setattr(db.pipeline, "run_daily", fake_run_daily)

    asyncio.run(db.main())

    payload = json.loads(db.PENDING_BUYS_PATH.read_text())
    assert payload["decisions"] == []
