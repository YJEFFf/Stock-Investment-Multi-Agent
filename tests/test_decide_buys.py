"""scripts/decide_buys.py — 08:30 판단 전용 진입점. pipeline.run_daily 내부
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
    result2 = asyncio.run(record_fn(rejected, GateResult(approved=False, rejected_by="position_limit"), result1, "반도체", 0.08))
    result3 = asyncio.run(record_fn(hold, GateResult(approved=False, rejected_by=None), result2, "화학", 0.08))

    # 승인분은 반환 포트폴리오에 얹힌다 — run_day가 이걸 다음 종목 게이트에 넘기므로,
    # 안 얹으면 그날 후보 전부가 같은 스냅샷을 상대로 독립 판정된다(2026-09-01 발견 1).
    # 2026-09-01 전까지 이 테스트는 "포트폴리오는 절대 안 바뀐다"고 단언하며 그 버그를
    # 지키고 있었다.
    assert [p.ticker for p in result1.positions] == ["005930"]
    assert result1.cash_weight == pytest.approx(0.92)

    # 거부된 BUY와 HOLD는 아무것도 안 바꾼다.
    assert result2 == result1
    assert result3 == result1

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


def test_run_daily_day_uses_kst_date_not_utc_date_at_0830_cron_time(monkeypatch, tmp_path):
    """08:30 KST cron 시각은 UTC로는 전날 23:30이다 — pipeline.run_daily에 넘기는
    day가 UTC 기준이면 pipeline.jsonl에 전날 날짜로 기록되고, notion_sync의
    같은 날 필터링과 하루씩 어긋난다("판단 로그가 없다"로 매일 잘못 표시된 버그,
    2026-08-13). day.date()가 KST 거래일과 같아야 한다."""
    import datetime as _dt

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(db, "is_krx_trading_day", lambda day: True)

    class _FixedDatetime(_dt.datetime):
        @classmethod
        def now(cls, tz=None):
            # 2026-08-13 08:30 KST == 2026-08-12 23:30 UTC
            return _dt.datetime(2026, 8, 13, 8, 30, tzinfo=tz)

    monkeypatch.setattr(db, "datetime", _FixedDatetime)

    captured = {}

    async def fake_run_daily(day, portfolio, config, analyst_fn, judge_fn, execute_fn, total_expected_analysts):
        captured["day"] = day
        return portfolio, []

    monkeypatch.setattr(db.pipeline, "run_daily", fake_run_daily)

    asyncio.run(db.main())

    assert captured["day"].date().isoformat() == "2026-08-13"


def test_recording_execute_fn_lets_the_gate_see_the_running_total():
    """하루에 여러 건이 승인되면 게이트가 그 누적을 봐야 한다.

    2026-09-01 이전에는 안 봤다. `_record`가 받은 포트폴리오를 그대로 돌려줘서
    그날 후보 전부가 08:30 스냅샷 하나를 상대로 독립 판정됐고, N건이 각자 한도
    안이라는 이유로 다 통과했다 — 합쳐서 넘는 건 아무도 안 봤다.

    여기서는 콜백을 run_day처럼 체인으로 부르면서, 총 노출이 실제로 쌓이는지만 본다
    (게이트 판정 자체는 tests/test_gate.py가 본다).
    """
    recorded: list[dict] = []
    record_fn = db._make_recording_execute_fn(recorded)
    portfolio = PortfolioState(cash_weight=1.0)

    for i in range(5):
        portfolio = asyncio.run(
            record_fn(
                _decision(f"00000{i}", "BUY"),
                GateResult(approved=True, rejected_by=None),
                portfolio,
                "반도체와반도체장비",
                0.08,
            )
        )

    assert len(recorded) == 5
    assert len(portfolio.positions) == 5
    # 투자 비중 0.40이 다음 후보의 total_exposure 판정 입력이 된다.
    assert 1.0 - portfolio.cash_weight == pytest.approx(0.40)


def test_recording_execute_fn_never_touches_the_broker(monkeypatch):
    """게이트 산수용 가상 포지션이지 실제 주문이 아니다 — execute()이지
    execute_buy_order()가 아니다(규칙 7)."""

    def fail(*a, **k):
        raise AssertionError("판단 단계에서 KIS를 부르면 안 된다")

    monkeypatch.setattr(db.pipeline.kis, "place_market_buy_order", fail)
    monkeypatch.setattr(db.pipeline.kis, "fetch_account_balance", fail)
    monkeypatch.setattr(db.pipeline.kis, "fetch_current_price", fail)

    recorded: list[dict] = []
    record_fn = db._make_recording_execute_fn(recorded)
    result = asyncio.run(
        record_fn(
            _decision("005930", "BUY"),
            GateResult(approved=True, rejected_by=None),
            PortfolioState(cash_weight=1.0),
            "반도체",
            0.08,
        )
    )

    # 가상 포지션이라 진입가가 없다 — 실제 체결가는 09:01 execute_open이 채운다.
    assert result.positions[0].entry_price is None
