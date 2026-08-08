"""실제 네이버 스크래핑 + 유료 Claude API를 쓰는 옵트인 전용 테스트.

기본적으로는 스킵된다. 배선이 실제로 동작하는지 직접 확인하고 싶을 때만:

    SIMA_LIVE_TEST=1 .venv/bin/pytest tests/test_live_smoke.py -v -s

.env에 ANTHROPIC_API_KEY가 채워져 있어야 한다 (공시 분석가까지 확인하려면 DART_API_KEY도).
"""

import asyncio
import os
from datetime import datetime, timezone

import pytest

pytestmark = pytest.mark.skipif(
    not os.getenv("SIMA_LIVE_TEST"),
    reason="네트워크·과금 API 사용 — SIMA_LIVE_TEST=1일 때만 실행",
)


def test_chart_analyst_end_to_end_live():
    from src import collectors
    from src.analysts import chart_analyst

    context = collectors.fetch_market_context("005930")
    assert context is not None, "네이버 스크래핑 실패 — 비공식 엔드포인트가 바뀌었을 수 있음"

    opinion = asyncio.run(chart_analyst(context))

    assert opinion is not None
    print(f"\n[live] ticker={opinion.ticker} score={opinion.score:.3f} confidence={opinion.confidence:.3f}")
    print(f"[live] evidence={opinion.evidence}")


def test_news_analyst_end_to_end_live():
    """종목 1개 기준 최소 확인용 — 대규모 측정은 여기서 하지 않는다 (비용 절제 피드백)."""
    from src import collectors
    from src.analysts import news_analyst
    from src.schemas import NewsContext

    ticker, sector = "005930", "반도체"
    company_news = collectors.fetch_company_news(ticker)
    sector_news = collectors.fetch_sector_news(sector)
    assert company_news is not None, "종목 뉴스 스크래핑 실패 — 페이지 구조가 바뀌었을 수 있음"
    assert sector_news is not None, "업종 뉴스 스크래핑 실패 — 페이지 구조가 바뀌었을 수 있음"

    context = NewsContext(
        ticker=ticker,
        sector=sector,
        as_of=datetime.now(timezone.utc),
        company_news=company_news,
        sector_news=sector_news,
    )
    opinion = asyncio.run(news_analyst(context))

    print(f"\n[live] company_news={len(company_news)}건 sector_news={len(sector_news)}건")
    if opinion is None:
        print("[live] 뉴스가 전혀 없어 판단 불가(None) — 정상 동작")
    else:
        print(f"[live] score={opinion.score:.3f} confidence={opinion.confidence:.3f} evidence={opinion.evidence}")


def test_disclosure_analyst_end_to_end_live():
    """종목 1개 기준 최소 확인용 — 대규모 측정은 여기서 하지 않는다 (비용 절제 피드백)."""
    from src import collectors
    from src.analysts import disclosure_analyst
    from src.schemas import DisclosureContext

    ticker = "005930"
    disclosures = collectors.fetch_disclosures(ticker)
    assert disclosures is not None, "DART 공시 수집 실패 — DART_API_KEY 미설정이거나 API 응답이 바뀌었을 수 있음"

    context = DisclosureContext(
        ticker=ticker,
        as_of=datetime.now(timezone.utc),
        disclosures=disclosures,
    )
    opinion = asyncio.run(disclosure_analyst(context))

    print(f"\n[live] disclosures={len(disclosures)}건")
    if opinion is None:
        print("[live] 공시가 전혀 없어 판단 불가(None) — 정상 동작")
    else:
        print(f"[live] score={opinion.score:.3f} confidence={opinion.confidence:.3f} evidence={opinion.evidence}")


def test_combined_analysts_end_to_end_live(tmp_path):
    """종목 1개로 차트+뉴스+공시 분석가와 실제 강세/약세 토론+포트폴리오 매니저까지
    전체 파이프라인(run_day)을 합쳐서 돌려본다.

    개별 분석가는 위 세 테스트로 이미 확인했으니, 여기서는 asyncio.gather 조합 +
    judgment.judge(토론+매니저) + 게이트까지 이어지는 배선 전체를 최소 비용으로
    확인한다. LLM 호출 총 6회: 차트·뉴스·공시·강세·약세·매니저.
    """
    from src import judgment, pipeline
    from src.schemas import PortfolioState, RiskGateConfig

    ticker, sector = "005930", "반도체"
    universe = [(ticker, sector)]
    day = datetime.now(timezone.utc)

    analyst_fn = pipeline.make_combined_analyst_fn(
        [
            pipeline.make_chart_analyst_fn(),
            pipeline.make_news_analyst_fn(),
            pipeline.make_disclosure_analyst_fn(),
        ]
    )

    _, results = asyncio.run(
        pipeline.run_day(
            universe,
            day,
            PortfolioState(),
            RiskGateConfig(),
            analyst_fn,
            judgment.judge,
            pipeline.execute_simulated,  # 판단 체인 확인이 목적 — 실주문은 안 낸다
            total_expected_analysts=3,
            log_path=tmp_path / "combined.jsonl",
        )
    )

    if not results:
        print("\n[live] 세 분석가 모두 의견 없음 — 관망 (정상)")
        return

    decision, gate_result = results[0]
    print(f"\n[live] action={decision.action} degraded={decision.degraded} inputs={len(decision.inputs)}건")
    print(f"[live] agents={[o.agent for o in decision.inputs]}")
    print(f"[live] reason={decision.reason}")
    for arg in decision.debate:
        print(f"[live] {arg.stance} (strength={arg.strength:.2f}): {arg.argument}")
    print(f"[live] gate approved={gate_result.approved} rejected_by={gate_result.rejected_by}")
