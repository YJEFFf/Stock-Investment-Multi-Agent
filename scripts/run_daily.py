"""cron으로 매일 실행되는 진입점 — 보유 종목 매도 평가 후 신규 매수 판단까지
하루치 전체 파이프라인을 실제 KIS 모의투자 주문·실제 Claude API로 돌린다.

포트폴리오 상태(보유 종목·현금 비중)는 logs/portfolio_state.json에 영속화한다 —
각 실행이 이전 실행의 포지션을 이어받아야 하므로, 프로세스가 매번 새로 뜨는
cron 환경에서는 파일이 유일한 상태 저장소다.

실행: uv run python scripts/run_daily.py (레포 루트에서)
"""

import asyncio
import logging
from datetime import datetime, timezone
from pathlib import Path

from src import judgment, pipeline, sell
from src.schemas import PortfolioState, RiskGateConfig

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("run_daily")

PORTFOLIO_STATE_PATH = Path("logs/portfolio_state.json")


def _load_portfolio() -> PortfolioState:
    if PORTFOLIO_STATE_PATH.exists():
        return PortfolioState.model_validate_json(PORTFOLIO_STATE_PATH.read_text())
    return PortfolioState()


def _save_portfolio(portfolio: PortfolioState) -> None:
    PORTFOLIO_STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    PORTFOLIO_STATE_PATH.write_text(portfolio.model_dump_json(indent=2))


async def main() -> None:
    day = datetime.now(timezone.utc)
    config = RiskGateConfig()
    portfolio = _load_portfolio()

    logger.info(
        "run_daily_start day=%s cash_weight=%.3f positions=%d",
        day.date().isoformat(),
        portfolio.cash_weight,
        len(portfolio.positions),
    )

    analyst_fn = pipeline.make_combined_analyst_fn(
        [
            pipeline.make_chart_analyst_fn(),
            pipeline.make_news_analyst_fn(),
            pipeline.make_disclosure_analyst_fn(),
        ]
    )

    # 1. 보유 종목 매도 평가 (결정론적 안전장치 + LLM 재량) 먼저 — 매수보다 앞서
    # 처리해서 그날 리스크를 줄이고 현금 여유를 먼저 만든다.
    try:
        portfolio = await pipeline.evaluate_holdings(
            portfolio,
            day,
            sell.execute_sell_order,
            analyst_fn=analyst_fn,
            judge_sell_fn=judgment.judge_sell,
        )
        _save_portfolio(portfolio)
        logger.info("evaluate_holdings_done positions=%d", len(portfolio.positions))
    except Exception:
        logger.exception("evaluate_holdings_failed — 매수 판단은 계속 진행한다")

    # 2. 신규 매수 판단 (유니버스 구성 -> 정량 필터 -> 분석가 -> 토론+매니저 -> 게이트 -> 실주문)
    portfolio, results = await pipeline.run_daily(
        day,
        portfolio,
        config,
        analyst_fn,
        judgment.judge,
        pipeline.execute_buy_order,
        total_expected_analysts=3,
    )
    _save_portfolio(portfolio)

    buys = [d.ticker for d, g in results if d.action == "BUY" and g.approved]
    logger.info(
        "run_daily_done decisions=%d buys=%s cash_weight=%.3f positions=%d",
        len(results),
        buys,
        portfolio.cash_weight,
        len(portfolio.positions),
    )


if __name__ == "__main__":
    asyncio.run(main())
