"""2026-09-14 P1-9: KIS 시세 실패 시 네이버 2차 시세."""

import asyncio
import logging
from datetime import date, datetime

import pytest

from src import collectors, kis, pipeline, sell
from src.schemas import PortfolioState, Position

_REAL_FETCH_NAVER = collectors.fetch_naver_quotes  # conftest가 테스트마다 막기 전에 잡아둔다
TODAY = date(2026, 9, 15)


class _Resp:
    def __init__(self, payload):
        self._payload = payload

    def json(self):
        return self._payload


def _row(code, price, high, low, traded_at="2026-09-15T10:31:02+09:00", change="-1000", market_status="OPEN"):
    return {
        "itemCode": code, "closePriceRaw": str(price), "highPriceRaw": str(high), "lowPriceRaw": str(low),
        "openPriceRaw": str(price), "compareToPreviousClosePriceRaw": change, "localTradedAt": traded_at,
        "marketStatus": market_status,
        "integratedPriceInfo": {"highPrice": "999,999", "lowPrice": "1"},  # NXT 섞인 값 — 쓰면 안 된다
    }


# --- fetch_naver_quotes ---


def test_parses_krx_fields_in_one_request(monkeypatch):
    calls = []
    payload = {"datas": [_row("282330", 145000, 147600, 137900), _row("007660", 109900, 111000, 108200)]}
    monkeypatch.setattr(collectors.requests, "get", lambda url, **kw: calls.append(url) or _Resp(payload))

    quotes = _REAL_FETCH_NAVER(["282330", "007660"], today=TODAY)

    assert len(calls) == 1 and calls[0].endswith("/282330,007660")
    assert quotes["282330"] == kis.Quote(price=145000, day_high=147600, day_low=137900, open_price=145000, prev_close=146000)
    assert quotes["007660"].day_low == 108200  # integratedPriceInfo의 1원이 아니라 KRX 저가


def test_a_quote_from_another_day_has_not_traded_today(monkeypatch):
    """개장 전에는 전일 15:30 체결이 온다. 그 값의 고가·저가로 판정하면 어제를 보고 판다."""
    payload = {"datas": [_row("282330", 145000, 147600, 137900, traded_at="2026-09-14T15:30:00+09:00")]}
    monkeypatch.setattr(collectors.requests, "get", lambda url, **kw: _Resp(payload))

    quote = _REAL_FETCH_NAVER(["282330"], today=TODAY)["282330"]

    assert quote.traded_today is False and quote.price == 145000


def test_outside_the_regular_session_the_day_range_is_not_trusted(monkeypatch):
    """장 전·후엔 넥스트레이드 거래가 섞일 수 있다 — 체결 시각이 오늘이어도 정규장이 아니면 기준가 취급."""
    payload = {"datas": [_row("282330", 145000, 147600, 137900, traded_at="2026-09-15T08:55:00+09:00", market_status="PREOPEN")]}
    monkeypatch.setattr(collectors.requests, "get", lambda url, **kw: _Resp(payload))

    assert _REAL_FETCH_NAVER(["282330"], today=TODAY)["282330"].traded_today is False


@pytest.mark.parametrize("payload", [[], None, "oops", {"datas": [None, 3]}, {"datas": "x"}])
def test_an_unexpected_payload_shape_returns_nothing_instead_of_raising(monkeypatch, payload):
    """독립 리뷰 재현: JSON 배열이 오면 .get에서 AttributeError가 났다."""
    monkeypatch.setattr(collectors.requests, "get", lambda url, **kw: _Resp(payload))
    assert _REAL_FETCH_NAVER(["282330"], today=TODAY) == {}


def test_network_failure_returns_nothing_instead_of_raising(monkeypatch):
    def boom(url, **kw):
        raise collectors.requests.ConnectionError("down")

    monkeypatch.setattr(collectors.requests, "get", boom)
    assert _REAL_FETCH_NAVER(["282330"], today=TODAY) == {}


# --- evaluate_holdings 통합 ---


DAY = datetime(2026, 9, 15, 10, 30)


def _portfolio():
    # 진입가 100, 손절 기본 −10% → 85는 손절 대상
    return PortfolioState(
        cash_weight=0.9,
        positions=[Position(ticker="005930", sector="반도체", weight=0.1, entry_price=100.0, peak_price=100.0, quantity=10)],
    )


def _kis_down_naver_up(monkeypatch, price=85.0):
    monkeypatch.setattr(kis, "fetch_quote", lambda ticker, policy=None: None)
    monkeypatch.setattr(
        collectors, "fetch_naver_quotes",
        lambda tickers, **kw: {t: kis.Quote(price=price, day_high=price, day_low=price) for t in tickers},
    )
    monkeypatch.setattr(pipeline.notify, "send_telegram_alert", lambda m: True)
    monkeypatch.setattr(pipeline, "display_name", lambda t: t)
    monkeypatch.setattr(pipeline, "_kst_today", lambda: TODAY)


def _capture_sells(monkeypatch):
    sold = []

    async def fake_finalize(pf, action, *a, **k):
        sold.append(action.reason)
        return pf

    monkeypatch.setattr(pipeline, "finalize_sell", fake_finalize)
    return sold


def test_shadow_mode_records_what_would_have_happened_but_does_not_sell(monkeypatch, caplog):
    _kis_down_naver_up(monkeypatch)
    sold = _capture_sells(monkeypatch)
    monkeypatch.setattr(pipeline, "SECONDARY_QUOTE_MODE", "shadow")

    with caplog.at_level(logging.INFO, logger="src.pipeline"):
        asyncio.run(pipeline.evaluate_holdings(_portfolio(), DAY, sell.execute_sell_simulated))

    assert sold == []
    shadow = [r.getMessage() for r in caplog.records if "secondary_quote_shadow" in r.getMessage()]
    assert shadow and "would_sell=stop_loss" in shadow[0]
    # 섀도에서는 눈을 감은 회차로 센다 — 판정을 안 했으니까
    assert any("all_prices_unavailable" in r.getMessage() for r in caplog.records)


def test_active_mode_judges_on_the_secondary_quote(monkeypatch, caplog):
    _kis_down_naver_up(monkeypatch)
    sold = _capture_sells(monkeypatch)
    monkeypatch.setattr(pipeline, "SECONDARY_QUOTE_MODE", "active")

    with caplog.at_level(logging.INFO, logger="src.pipeline"):
        asyncio.run(pipeline.evaluate_holdings(_portfolio(), DAY, sell.execute_sell_simulated))

    assert sold == ["stop_loss"]
    assert not any("all_prices_unavailable" in r.getMessage() for r in caplog.records)


def test_active_mode_still_refuses_a_pre_open_secondary_quote(monkeypatch):
    """2차 소스라고 개장 전 기준가 규약이 풀리지 않는다."""
    _kis_down_naver_up(monkeypatch)
    monkeypatch.setattr(
        collectors, "fetch_naver_quotes", lambda tickers, **kw: {t: kis.Quote(price=85.0) for t in tickers}
    )
    sold = _capture_sells(monkeypatch)
    monkeypatch.setattr(pipeline, "SECONDARY_QUOTE_MODE", "active")

    asyncio.run(pipeline.evaluate_holdings(_portfolio(), DAY, sell.execute_sell_simulated))

    assert sold == []


def test_the_secondary_source_is_not_called_when_kis_answered(monkeypatch):
    monkeypatch.setattr(kis, "fetch_quote", lambda ticker, policy=None: kis.Quote(price=99.0, day_high=99.0, day_low=99.0))
    monkeypatch.setattr(collectors, "fetch_naver_quotes", lambda tickers, **kw: (_ for _ in ()).throw(AssertionError("불필요한 2차 조회")))
    monkeypatch.setattr(pipeline, "_kst_today", lambda: TODAY)
    monkeypatch.setattr(pipeline, "SECONDARY_QUOTE_AGREEMENT_WINDOW", (datetime.min.time(), datetime.min.time()))

    asyncio.run(pipeline.evaluate_holdings(_portfolio(), DAY, sell.execute_sell_simulated))


def test_the_default_mode_is_shadow_until_the_soak_is_reviewed():
    """매매 경로 변경은 최소 1거래일 섀도로 돌린 뒤 켠다(docs/PLAN.md 매매 경로 변경 절차)."""
    assert pipeline.SECONDARY_QUOTE_MODE == "shadow"


def test_a_broken_secondary_source_never_stops_the_stop_loss_on_priced_holdings(monkeypatch):
    """독립 리뷰 재현(2026-09-14): A는 KIS가 손절선 아래 가격을 줬고 B는 KIS 실패, 네이버가 예외.
    예전 코드는 회차 전체가 죽어 A도 안 팔렸다. 보조 장치가 주 장치를 끄면 안 된다."""
    portfolio = PortfolioState(
        cash_weight=0.8,
        positions=[
            Position(ticker="A", sector="x", weight=0.1, entry_price=100.0, peak_price=100.0, quantity=10),
            Position(ticker="B", sector="x", weight=0.1, entry_price=100.0, peak_price=100.0, quantity=10),
        ],
    )
    monkeypatch.setattr(
        kis, "fetch_quote",
        lambda ticker, policy=None: kis.Quote(price=80.0, day_high=80.0, day_low=80.0) if ticker == "A" else None,
    )

    def broken(tickers, **kw):
        raise AttributeError("'list' object has no attribute 'get'")

    monkeypatch.setattr(collectors, "fetch_naver_quotes", broken)
    monkeypatch.setattr(pipeline, "_kst_today", lambda: TODAY)
    sold = _capture_sells(monkeypatch)

    asyncio.run(pipeline.evaluate_holdings(portfolio, DAY, sell.execute_sell_simulated))

    assert sold == ["stop_loss"]
