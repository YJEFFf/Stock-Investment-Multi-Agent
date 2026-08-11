"""scripts/check_stop_loss.py — 1분마다 도는 결정론적 손절/익절 체크.

내부 판단 로직(손절 -10%, 트레일링 익절)은 tests/test_evaluate_holdings.py가
이미 충분히 검증한다 — 여기서는 이 스크립트가 evaluate_holdings를
analyst_fn/judge_sell_fn 없이(LLM 없이) 부르는지, 포지션이 없거나 휴장일이면
KIS를 아예 안 부르는지, 결과를 실제로 저장하는지만 본다.
"""

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import scripts.check_stop_loss as csl
from src import kis, portfolio_store
from src.schemas import PortfolioState, Position


def test_noop_when_not_trading_day(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(csl, "is_krx_trading_day", lambda day: False)

    def fail(*a, **k):
        raise AssertionError("휴장일엔 KIS를 호출하면 안 된다")

    monkeypatch.setattr(kis, "fetch_current_price", fail)

    asyncio.run(csl.main())  # 예외 없이 조용히 끝나야 함


def test_noop_when_no_positions(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(csl, "is_krx_trading_day", lambda day: True)
    portfolio_store.save_portfolio(PortfolioState(cash_weight=1.0))

    def fail(*a, **k):
        raise AssertionError("포지션이 없으면 KIS를 호출하면 안 된다")

    monkeypatch.setattr(kis, "fetch_current_price", fail)

    asyncio.run(csl.main())


def test_deterministic_only_no_llm_and_persists_result(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(csl, "is_krx_trading_day", lambda day: True)
    monkeypatch.setattr(csl.pipeline.notify, "send_telegram_alert", lambda message: True)
    monkeypatch.setattr(csl.pipeline, "_display_name", lambda ticker: ticker)

    monkeypatch.setattr(kis, "fetch_current_price", lambda ticker: 89.0)  # -11%, 손절 트리거

    position = Position(ticker="005930", sector="반도체", weight=0.10, entry_price=100.0, peak_price=100.0)
    portfolio_store.save_portfolio(PortfolioState(cash_weight=0.90, positions=[position]))

    def fail_analyst_builder(*a, **k):
        raise AssertionError("check_stop_loss는 LLM 재량 매도 계층을 건드리면 안 된다")

    monkeypatch.setattr(csl.pipeline, "make_combined_analyst_fn", fail_analyst_builder)

    asyncio.run(csl.main())

    saved = portfolio_store.load_portfolio()
    assert saved.positions == []  # 손절로 청산됨
    assert saved.cash_weight == 1.0
