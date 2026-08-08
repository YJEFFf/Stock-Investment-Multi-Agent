"""차트 분석가로 실제 20영업일 신호 발생률을 측정하는 옵트인 전용 테스트.

마일스톤 1 더미 기준선(10%)과 비교하기 위한 것. 실제 네트워크(네이버 스크래핑)와
유료 Claude API를 쓰므로 기본적으로 스킵된다:

    SIMA_LIVE_TEST=1 .venv/bin/pytest tests/test_live_signal_rate.py -v -s

종목당 스크래핑은 한 번만 하고(80일치), 그 안에서 최근 20거래일을 하루씩 슬라이스해
차트 분석가를 호출한다 — 20종목 × 20일 = 최대 400회 LLM 호출.
"""

import asyncio
import os
from datetime import datetime, timezone

import pytest

pytestmark = pytest.mark.skipif(
    not os.getenv("SIMA_LIVE_TEST"),
    reason="네트워크·과금 API 사용 — SIMA_LIVE_TEST=1일 때만 실행",
)

from src import collectors, pipeline
from src.analysts import chart_analyst
from src.schemas import MarketContext, PortfolioState, RiskGateConfig

# 실제 KOSPI200 구성 종목 중 유동성 높은 대형주로 구성한 20개 테스트 유니버스.
REAL_UNIVERSE = [
    ("005930", "반도체"),
    ("000660", "반도체"),
    ("035420", "IT"),
    ("035720", "IT"),
    ("005380", "자동차"),
    ("000270", "자동차"),
    ("051910", "화학"),
    ("006400", "화학"),
    ("373220", "화학"),
    ("105560", "금융"),
    ("055550", "금융"),
    ("086790", "금융"),
    ("207940", "바이오"),
    ("068270", "바이오"),
    ("005490", "철강조선"),
    ("010140", "철강조선"),
    ("097950", "소비재"),
    ("051900", "소비재"),
    ("017670", "통신"),
    ("030200", "통신"),
]

LOOKBACK_DAYS = 80
EVAL_DAYS = 20
LLM_CONCURRENCY = 5


def test_chart_analyst_signal_rate_over_real_history(tmp_path):
    log_path = tmp_path / "signal_rate.jsonl"

    histories = {}
    for ticker, _ in REAL_UNIVERSE:
        context = collectors.fetch_market_context(ticker, lookback_days=LOOKBACK_DAYS)
        if context is not None:
            histories[ticker] = context.bars
        else:
            print(f"[signal-rate] {ticker} 데이터 수집 실패 — 이번 측정에서 제외")

    eval_dates = sorted({bar.date for bars in histories.values() for bar in bars})[-EVAL_DAYS:]
    semaphore = asyncio.Semaphore(LLM_CONCURRENCY)

    def make_analyst_fn(eval_date):
        async def _fn(ticker: str, sector: str, day: datetime) -> list:
            bars = [b for b in histories.get(ticker, []) if b.date <= eval_date]
            if not bars:
                return []
            context = MarketContext(
                ticker=ticker,
                as_of=day,
                bars=bars,
                indicators=collectors.compute_indicators(bars),
            )
            async with semaphore:
                opinion = await chart_analyst(context)
            return [opinion] if opinion is not None else []

        return _fn

    async def _run():
        portfolio = PortfolioState()
        config = RiskGateConfig()
        for eval_date in eval_dates:
            day = datetime(eval_date.year, eval_date.month, eval_date.day, tzinfo=timezone.utc)
            portfolio, results = await pipeline.run_day(
                REAL_UNIVERSE,
                day,
                portfolio,
                config,
                make_analyst_fn(eval_date),
                pipeline.propose_decision,
                pipeline.execute_simulated,
                log_path=log_path,
            )
            buys = [d.ticker for d, g in results if d.action == "BUY" and g.approved]
            print(f"[signal-rate] {eval_date}: {len(results)}건 판단, 승인된 매수={buys}")
        return portfolio

    asyncio.run(_run())

    summary = pipeline.summarize_log(log_path, total_days=len(eval_dates))
    print(f"\n[signal-rate] 요약: {summary}")
    print("[signal-rate] 마일스톤 1 더미 기준선은 10% (2/20일) 였음")
