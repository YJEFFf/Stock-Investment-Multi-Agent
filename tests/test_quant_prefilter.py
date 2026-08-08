import asyncio
from datetime import date, datetime, timezone

from src import collectors, pipeline
from src.schemas import MarketContext, OHLCVBar, PortfolioState, RiskGateConfig


def _bars(closes: list[float]) -> list[OHLCVBar]:
    return [
        OHLCVBar(date=date(2026, 1, 1 + i), open=c, high=c, low=c, close=c, volume=1000)
        for i, c in enumerate(closes)
    ]


# --- passes_quant_filter (순수 함수) ---


def test_passes_when_volume_surges():
    assert pipeline.passes_quant_filter({"volume_vs_20d_avg_ratio": 2.5}, index_return_5d_pct=None)


def test_fails_when_volume_below_threshold():
    assert not pipeline.passes_quant_filter({"volume_vs_20d_avg_ratio": 1.2}, index_return_5d_pct=None)


def test_passes_when_rsi_overbought():
    assert pipeline.passes_quant_filter({"rsi14": 75.0}, index_return_5d_pct=None)


def test_passes_when_rsi_oversold():
    assert pipeline.passes_quant_filter({"rsi14": 25.0}, index_return_5d_pct=None)


def test_fails_when_rsi_neutral():
    assert not pipeline.passes_quant_filter({"rsi14": 50.0}, index_return_5d_pct=None)


def test_passes_when_excess_return_z_over_threshold():
    # 종목 +8%, 지수 +1% -> 초과 7%. 자기 일별 변동성 1% -> 5일 기대변동폭 ~2.24%.
    # z = 7 / 2.24 ≈ 3.13 >= 2.0
    indicators = {"return_5d_pct": 8.0, "daily_return_stdev_20d": 1.0}
    assert pipeline.passes_quant_filter(indicators, index_return_5d_pct=1.0)


def test_fails_when_same_excess_return_is_normal_for_a_volatile_stock():
    """2026-08-08 실측: 지수가 크게 움직인 주엔 고정 %p 문턱이 거의 다 걸렸다
    (199종목 중 188종목 통과). 자기 변동성으로 정규화하면 원래 변동성이 큰
    종목에게는 같은 초과수익률이 평범한 움직임으로 취급돼 걸리지 않아야 한다."""
    indicators = {"return_5d_pct": 8.0, "daily_return_stdev_20d": 5.0}  # 초과 7%는 동일
    assert not pipeline.passes_quant_filter(indicators, index_return_5d_pct=1.0)


def test_excess_return_condition_skipped_when_daily_vol_missing():
    indicators = {"return_5d_pct": 20.0}  # daily_return_stdev_20d 없음
    assert not pipeline.passes_quant_filter(indicators, index_return_5d_pct=1.0)


def test_excess_return_condition_skipped_when_index_return_missing():
    indicators = {"return_5d_pct": 20.0, "daily_return_stdev_20d": 1.0}
    assert not pipeline.passes_quant_filter(indicators, index_return_5d_pct=None)


def test_fails_on_empty_indicators():
    assert not pipeline.passes_quant_filter({}, index_return_5d_pct=None)


# --- quant_prefilter (비동기, collectors 목킹) ---


def _context(ticker: str, indicators: dict[str, float]) -> MarketContext:
    return MarketContext(ticker=ticker, as_of=datetime.now(timezone.utc), bars=[], indicators=indicators)


def test_quant_prefilter_keeps_only_tickers_that_pass(monkeypatch):
    monkeypatch.setattr(collectors, "fetch_kospi200_index_bars", lambda lookback_days: None)

    contexts = {
        "005930": _context("005930", {"volume_vs_20d_avg_ratio": 3.0}),  # 통과
        "000660": _context("000660", {"volume_vs_20d_avg_ratio": 1.0, "rsi14": 50.0}),  # 탈락
    }
    monkeypatch.setattr(collectors, "fetch_market_context", lambda ticker, lookback_days: contexts[ticker])

    universe = [("005930", "반도체"), ("000660", "반도체")]
    result = asyncio.run(pipeline.quant_prefilter(universe))

    assert result == [("005930", "반도체")]


def test_quant_prefilter_excludes_ticker_when_market_context_fetch_fails(monkeypatch):
    monkeypatch.setattr(collectors, "fetch_kospi200_index_bars", lambda lookback_days: None)

    def fake_fetch(ticker, lookback_days):
        return None if ticker == "999999" else _context(ticker, {"volume_vs_20d_avg_ratio": 5.0})

    monkeypatch.setattr(collectors, "fetch_market_context", fake_fetch)

    universe = [("005930", "반도체"), ("999999", "알수없음")]
    result = asyncio.run(pipeline.quant_prefilter(universe))

    assert result == [("005930", "반도체")]


def test_quant_prefilter_survives_partial_exceptions(monkeypatch):
    monkeypatch.setattr(collectors, "fetch_kospi200_index_bars", lambda lookback_days: None)

    def fake_fetch(ticker, lookback_days):
        if ticker == "000660":
            raise RuntimeError("boom")
        return _context(ticker, {"volume_vs_20d_avg_ratio": 5.0})

    monkeypatch.setattr(collectors, "fetch_market_context", fake_fetch)

    universe = [("005930", "반도체"), ("000660", "반도체")]
    result = asyncio.run(pipeline.quant_prefilter(universe))

    assert result == [("005930", "반도체")]


def test_quant_prefilter_returns_empty_when_nothing_passes(monkeypatch):
    """후보 0개인 날이 정상이다 (규칙 1·3) — top-N으로 채우지 않는다."""
    monkeypatch.setattr(collectors, "fetch_kospi200_index_bars", lambda lookback_days: None)
    monkeypatch.setattr(
        collectors,
        "fetch_market_context",
        lambda ticker, lookback_days: _context(ticker, {"volume_vs_20d_avg_ratio": 1.0, "rsi14": 50.0}),
    )

    result = asyncio.run(pipeline.quant_prefilter([("005930", "반도체")]))

    assert result == []


def test_quant_prefilter_uses_real_index_bars_for_excess_return(monkeypatch):
    """지수 시계열이 실제로 조회되면 초과수익률 조건에 반영되는지 종단 확인."""
    index_bars = _bars([100.0] * 20 + [101.0])  # 지수는 거의 안 움직임 (+1%)
    monkeypatch.setattr(collectors, "fetch_kospi200_index_bars", lambda lookback_days: index_bars)

    stock_context = _context(
        "005930", {"return_5d_pct": 9.0, "daily_return_stdev_20d": 1.0}
    )  # 초과수익률 ~8%, z ≈ 3.6
    monkeypatch.setattr(collectors, "fetch_market_context", lambda ticker, lookback_days: stock_context)

    result = asyncio.run(pipeline.quant_prefilter([("005930", "반도체")]))

    assert result == [("005930", "반도체")]


# --- build_universe_with_sectors / run_daily (전체 배선) ---


def test_build_universe_with_sectors_merges_universe_and_sector_map(monkeypatch):
    monkeypatch.setattr(
        collectors, "fetch_kospi200_universe", lambda: [("005930", "삼성전자"), ("000660", "SK하이닉스")]
    )
    monkeypatch.setattr(collectors, "fetch_kospi200_sector_map", lambda: {"005930": "반도체와반도체장비"})

    universe = asyncio.run(pipeline.build_universe_with_sectors())

    # 매핑에 없는 종목(000660)은 빈 문자열로 대체된다.
    assert universe == [("005930", "반도체와반도체장비"), ("000660", "")]


def test_build_universe_with_sectors_returns_none_when_universe_fetch_fails(monkeypatch):
    monkeypatch.setattr(collectors, "fetch_kospi200_universe", lambda: None)
    monkeypatch.setattr(collectors, "fetch_kospi200_sector_map", lambda: {})

    assert asyncio.run(pipeline.build_universe_with_sectors()) is None


def test_build_universe_with_sectors_degrades_when_sector_map_unavailable(monkeypatch):
    """업종 맵이 완전히 실패해도(캐시조차 없음) 유니버스 자체는 살려서 진행한다 —
    업종을 쓰는 건 뉴스 분석가의 배경 뉴스뿐이라 이거 하나로 하루 전체를 막지 않는다."""
    monkeypatch.setattr(collectors, "fetch_kospi200_universe", lambda: [("005930", "삼성전자")])
    monkeypatch.setattr(collectors, "fetch_kospi200_sector_map", lambda: None)

    universe = asyncio.run(pipeline.build_universe_with_sectors())

    assert universe == [("005930", "")]


def test_run_daily_composes_universe_filter_and_run_day(monkeypatch, tmp_path):
    async def fake_build_universe():
        return [("005930", "반도체"), ("000660", "반도체")]

    async def fake_quant_prefilter(universe, lookback_days=60):
        return [t for t in universe if t[0] == "005930"]  # 하나만 통과시킴

    called_with = {}

    async def fake_analyst_fn(ticker, sector, day):
        called_with["ticker"] = ticker
        from src.schemas import AnalystOpinion

        return [AnalystOpinion(agent="chart", ticker=ticker, score=0.9, confidence=0.9, evidence=["e"], as_of=day)]

    monkeypatch.setattr(pipeline, "build_universe_with_sectors", fake_build_universe)
    monkeypatch.setattr(pipeline, "quant_prefilter", fake_quant_prefilter)

    day = datetime(2026, 1, 5, tzinfo=timezone.utc)
    _, results = asyncio.run(
        pipeline.run_daily(
            day,
            PortfolioState(),
            RiskGateConfig(),
            fake_analyst_fn,
            pipeline.propose_decision,
            total_expected_analysts=1,
            log_path=tmp_path / "log.jsonl",
        )
    )

    assert called_with["ticker"] == "005930"  # 필터를 통과 못 한 000660엔 analyst_fn이 안 불림
    assert len(results) == 1
    assert results[0][0].ticker == "005930"


def test_run_daily_returns_empty_when_universe_unavailable(monkeypatch, tmp_path):
    async def fake_build_universe():
        return None

    async def should_not_be_called(*a, **k):
        raise AssertionError("유니버스가 없으면 analyst_fn이 호출되면 안 된다")

    monkeypatch.setattr(pipeline, "build_universe_with_sectors", fake_build_universe)

    day = datetime(2026, 1, 5, tzinfo=timezone.utc)
    portfolio_in = PortfolioState()
    portfolio_out, results = asyncio.run(
        pipeline.run_daily(
            day,
            portfolio_in,
            RiskGateConfig(),
            should_not_be_called,
            pipeline.propose_decision,
            log_path=tmp_path / "log.jsonl",
        )
    )

    assert results == []
    assert portfolio_out == portfolio_in
