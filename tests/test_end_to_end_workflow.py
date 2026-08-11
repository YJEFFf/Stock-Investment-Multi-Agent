"""cron이 매일 아침 실제로 도는 경로(scripts/run_daily.py의 main())를 처음부터
끝까지 한 번 통째로 돌려본다 — 유니버스 수집 → 정량 필터 → 분석가(차트만 실제
의견, 뉴스/공시는 데이터 없음으로 자연 스킵) → 토론+매니저 → 게이트 → 실주문 →
매매일지 로그 → 텔레그램 알림, 그리고 보유 종목 쪽은 결정론적 손절까지.

각 단계는 이미 자기 테스트 파일에서 개별적으로 충분히 검증돼 있다 — 여기서
보려는 건 "그 단계들이 실제 함수 그대로 서로 이어지는가"이지, 각 단계 내부
로직을 다시 검증하는 게 아니다. 그래서 네트워크 경계(kis.py/collectors.py의
개별 fetch 함수, llm.call_structured, notify.send_telegram_alert)만 목킹하고
그 사이(analysts.py, judgment.py, pipeline.py, sell.py, run_daily.py)는 전부
실제 코드를 그대로 실행시킨다.
"""

import asyncio
import json
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import scripts.run_daily as rd
from src import analysts, collectors, kis
from src.schemas import OHLCVBar, PortfolioState, Position

NEW_BUY_TICKER = "005930"
HELD_TICKER = "000660"


def _flat_bars(n: int = 60, close: float = 70000.0) -> list[OHLCVBar]:
    """마지막 날만 거래량이 5배 튀는 것 말고는 완전히 평평한 60일치 일봉 —
    정량 필터(거래량 서지)는 통과시키되, execute_buy_order의 갭 체크(전일 종가
    대비 현재가)는 0%로 걸리지 않게 한다."""
    bars = []
    for i in range(n):
        volume = 5000 if i == n - 1 else 1000
        bars.append(
            OHLCVBar(date=date(2026, 1, 1) + timedelta(days=i), open=close, high=close, low=close, close=close, volume=volume)
        )
    return bars


async def _fake_call_structured(*, system, user, response_model, json_schema, **kwargs):
    """llm.call_structured의 유일한 가짜 구현. 실제 analysts.py/judgment.py가
    만드는 프롬프트는 그대로 보내지고(포맷 로직도 실제로 검증됨), 나가는 자리만
    바꿔친다. response_model 클래스명으로 어떤 호출인지 구분한다."""
    name = response_model.__name__
    if name == "_ChartAnalysisResponse":
        return response_model(score=0.9, confidence=0.85, reasoning="거래량 급증 + 상승 추세")
    if name == "_DebateResponse":
        return response_model(argument="분석가 근거가 뚜렷하다", strength=0.75)
    if name == "_ManagerResponse":
        return response_model(action="BUY", reasoning="차트 분석가의 강한 매수 신호를 근거로 매수 승인")
    raise AssertionError(f"이 테스트 시나리오에서 예상 못 한 LLM 호출: {name}")


def test_full_daily_workflow_sell_then_buy_end_to_end(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)  # logs/*.jsonl, portfolio_state.json 전부 tmp_path 밑으로

    # --- 초기 포트폴리오: 손절선을 넘은 보유 종목 하나 ---
    held = Position(
        ticker=HELD_TICKER,
        sector="화학",
        weight=0.10,
        entry_price=100.0,
        peak_price=100.0,
        quantity=10,
        entry_day=date(2026, 1, 5),
    )
    rd._save_portfolio(PortfolioState(cash_weight=0.90, positions=[held]))

    # --- 게이트 통과/거래일 강제 ---
    monkeypatch.setattr(rd, "is_krx_trading_day", lambda day: True)

    # --- 네트워크 경계 목킹: collectors ---
    monkeypatch.setattr(collectors, "fetch_kospi200_universe", lambda: [(NEW_BUY_TICKER, "삼성전자")])
    monkeypatch.setattr(collectors, "fetch_kospi200_sector_map", lambda: {NEW_BUY_TICKER: "반도체"})
    monkeypatch.setattr(collectors, "fetch_kospi200_index_bars", lambda lookback_days=60: None)
    monkeypatch.setattr(collectors, "fetch_company_news", lambda ticker, limit=10: [])
    monkeypatch.setattr(collectors, "fetch_sector_news", lambda sector, limit=10: [])
    monkeypatch.setattr(collectors, "fetch_disclosures", lambda ticker, lookback_days=30, limit=10: [])

    # --- 네트워크 경계 목킹: kis ---
    fake_bars = _flat_bars()
    monkeypatch.setattr(kis, "fetch_daily_ohlcv", lambda ticker, lookback_days=60: fake_bars)

    def fake_current_price(ticker):
        return 89.0 if ticker == HELD_TICKER else 70000.0  # 보유종목은 -11%(손절), 신규후보는 무갭

    monkeypatch.setattr(kis, "fetch_current_price", fake_current_price)
    monkeypatch.setattr(kis, "fetch_account_balance", lambda: 100_000_000.0)
    monkeypatch.setattr(kis, "place_market_buy_order", lambda ticker, qty: "BUYORDER1")
    monkeypatch.setattr(kis, "fetch_fill_price", lambda ticker, order_date: 70000.0)
    monkeypatch.setattr(kis, "place_market_sell_order", lambda ticker, qty: "SELLORDER1")

    # --- 네트워크 경계 목킹: llm (analysts.py가 차트/뉴스/공시, judgment.py가 토론/매니저) ---
    monkeypatch.setattr(analysts.llm, "call_structured", _fake_call_structured)
    monkeypatch.setattr(rd.judgment.llm, "call_structured", _fake_call_structured)

    # --- 텔레그램/노션: 실제 호출 없이 캡처만 ---
    alerts = []
    monkeypatch.setattr(rd.notify, "send_telegram_alert", lambda message: alerts.append(message) or True)
    # 종목명 캐시(.kospi200_ticker_name_cache.json)는 레포 루트의 실제 파일이라
    # chdir로도 못 가린다 — 테스트를 그 파일 상태와 무관하게 만들려고 코드 그대로 쓰게 둔다.
    monkeypatch.setattr(rd.pipeline, "display_name", lambda ticker: ticker)
    monkeypatch.delenv("NOTION_TRADE_JOURNAL_DB_ID", raising=False)
    monkeypatch.delenv("NOTION_DAILY_REPORT_DB_ID", raising=False)

    asyncio.run(rd.main())

    # --- 결과 검증: 최종 포트폴리오 ---
    final_portfolio = rd._load_portfolio()
    tickers = {p.ticker for p in final_portfolio.positions}
    assert HELD_TICKER not in tickers  # 손절로 제거됨
    assert NEW_BUY_TICKER in tickers  # 신규 매수 편입됨

    # --- 결과 검증: 매매일지(trade_journal.jsonl)에 매도 1건 + 매수 1건 ---
    journal_path = tmp_path / "logs" / "trade_journal.jsonl"
    journal_entries = [json.loads(line) for line in journal_path.read_text().splitlines()]
    events = {e["event"]: e for e in journal_entries}
    assert set(events) == {"sell", "buy"}
    assert events["sell"]["ticker"] == HELD_TICKER
    assert events["sell"]["reason"] == "stop_loss"
    assert events["buy"]["ticker"] == NEW_BUY_TICKER
    assert events["buy"]["decision"]["reason"] == "차트 분석가의 강한 매수 신호를 근거로 매수 승인"

    # --- 결과 검증: 판단 로그(pipeline.jsonl)에도 남았는지 ---
    pipeline_path = tmp_path / "logs" / "pipeline.jsonl"
    pipeline_entries = [json.loads(line) for line in pipeline_path.read_text().splitlines()]
    assert any(e["ticker"] == NEW_BUY_TICKER and e["action"] == "BUY" and e["approved"] for e in pipeline_entries)

    # --- 결과 검증: 텔레그램 알림 2건(매도/매수), 통일된 양식 ---
    assert len(alerts) == 2
    sell_alert = next(a for a in alerts if "매도" in a)
    buy_alert = next(a for a in alerts if "매수" in a and "스킵" not in a)
    assert HELD_TICKER in sell_alert
    assert "손절" in sell_alert
    assert NEW_BUY_TICKER in buy_alert
    assert buy_alert.startswith("🟢 [SIMA] 매수")
