"""scripts/decide_llm_sell.py — 장 마감 후(15:35) LLM 재량 매도 "판단만" 하는
진입점. 판단 자체(judgment.judge_sell)는 tests/test_judgment.py가 이미
검증하므로, 여기서는 이 스크립트가 실제로 집행하지 않고(포트폴리오/브로커를
안 건드림) 결과를 logs/pending_sells.json에 정확히 남기는지만 본다.
"""

import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest

import scripts.decide_llm_sell as dls
from src import kis, portfolio_store
from src.schemas import AnalystOpinion, PortfolioState, Position, SellAction


def _position(**overrides) -> Position:
    defaults = dict(ticker="005930", sector="반도체", weight=0.10, entry_price=100.0, peak_price=100.0)
    defaults.update(overrides)
    return Position(**defaults)


@pytest.fixture(autouse=True)
def _no_real_notify_or_name_lookup(monkeypatch):
    # main()이 이제 판단 결과 요약(0건 포함)을 텔레그램으로 보낸다 — 목킹 안 하면
    # 테스트가 실제 텔레그램 메시지를 보내고 실제 네이버를 긁는다.
    monkeypatch.setattr(dls.notify, "send_telegram_alert", lambda message: True)
    monkeypatch.setattr(dls.pipeline, "display_name", lambda ticker: ticker)


def test_noop_when_not_trading_day(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(dls, "is_krx_trading_day", lambda day: False)

    def fail(*a, **k):
        raise AssertionError("휴장일엔 KIS를 호출하면 안 된다")

    monkeypatch.setattr(kis, "fetch_current_price", fail)

    asyncio.run(dls.main())
    assert not dls.PENDING_SELLS_PATH.exists()


def test_writes_empty_actions_when_no_positions(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(dls, "is_krx_trading_day", lambda day: True)
    portfolio_store.save_portfolio(PortfolioState(cash_weight=1.0))

    asyncio.run(dls.main())

    payload = json.loads(dls.PENDING_SELLS_PATH.read_text())
    assert payload["actions"] == []


def test_decides_sell_without_executing(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(dls, "is_krx_trading_day", lambda day: True)

    portfolio = PortfolioState(cash_weight=0.90, positions=[_position()])
    portfolio_store.save_portfolio(portfolio)

    monkeypatch.setattr(kis, "fetch_current_price", lambda ticker: 105.0)

    async def fake_analyst_fn(ticker, sector, day):
        return [AnalystOpinion(agent="chart", ticker=ticker, score=-0.6, confidence=0.7, evidence=["e"], as_of=day)]

    monkeypatch.setattr(dls.pipeline, "make_combined_analyst_fn", lambda fns: fake_analyst_fn)

    captured = {}

    async def fake_judge_sell(ticker, opinions, unrealized_pct):
        captured["called_with"] = (ticker, len(opinions), round(unrealized_pct, 4))
        return SellAction(ticker=ticker, reason="llm_discretionary", sell_fraction=1.0, reasoning="근거 약화")

    monkeypatch.setattr(dls.judgment, "judge_sell", fake_judge_sell)

    asyncio.run(dls.main())

    # 포트폴리오는 절대 안 바뀐다 — 판단만 하고 집행은 다음날 execute_open.py 몫이다.
    assert portfolio_store.load_portfolio() == portfolio

    assert captured["called_with"] == ("005930", 1, 0.05)

    payload = json.loads(dls.PENDING_SELLS_PATH.read_text())
    assert len(payload["actions"]) == 1
    assert payload["actions"][0]["ticker"] == "005930"
    assert payload["actions"][0]["reason"] == "llm_discretionary"
    assert payload["actions"][0]["reasoning"] == "근거 약화"


def test_skips_position_when_price_unavailable(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(dls, "is_krx_trading_day", lambda day: True)
    portfolio_store.save_portfolio(PortfolioState(cash_weight=0.90, positions=[_position()]))

    monkeypatch.setattr(kis, "fetch_current_price", lambda ticker: None)

    def fail(*a, **k):
        raise AssertionError("시세 조회가 실패했으면 분석가/judge_sell을 부르면 안 된다")

    monkeypatch.setattr(dls.pipeline, "make_combined_analyst_fn", lambda fns: fail)
    monkeypatch.setattr(dls.judgment, "judge_sell", fail)

    asyncio.run(dls.main())

    payload = json.loads(dls.PENDING_SELLS_PATH.read_text())
    assert payload["actions"] == []


def test_hold_when_judge_sell_returns_none(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(dls, "is_krx_trading_day", lambda day: True)
    portfolio_store.save_portfolio(PortfolioState(cash_weight=0.90, positions=[_position()]))

    monkeypatch.setattr(kis, "fetch_current_price", lambda ticker: 105.0)

    async def fake_analyst_fn(ticker, sector, day):
        return []

    monkeypatch.setattr(dls.pipeline, "make_combined_analyst_fn", lambda fns: fake_analyst_fn)

    async def fake_judge_sell(ticker, opinions, unrealized_pct):
        return None

    monkeypatch.setattr(dls.judgment, "judge_sell", fake_judge_sell)

    asyncio.run(dls.main())

    payload = json.loads(dls.PENDING_SELLS_PATH.read_text())
    assert payload["actions"] == []
