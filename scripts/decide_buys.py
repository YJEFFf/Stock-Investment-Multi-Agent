"""장 시작 전(07:00 KST cron) 신규 매수 판단만 하는 진입점 — 집행은 안 한다.

전일 데이터 기준으로 유니버스 구성 -> 정량 필터 -> 분석가 -> 토론+매니저 ->
게이트까지 마치고, 승인된 BUY만 logs/pending_buys.json에 남긴다. 실제 주문은
장 시작 직후 scripts/execute_open.py가 낸다 — 판단(전일 종가 기준)과 집행(장
시작가) 사이 시차는 pipeline.execute_buy_order의 갭 체크(±3%)가 이미 상정하고
있는 것과 같은 종류다.

여기서 포트폴리오 상태를 쓰지 않는다(게이트 체크용으로 읽기만 함) — 이 시간대엔
다른 스크립트가 돌지 않으므로 락이 필요 없다.

실행: uv run python scripts/decide_buys.py (레포 루트에서)
"""

import asyncio
import json
import logging
import sys
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src import judgment, pipeline  # noqa: E402
from src.market_calendar import is_krx_trading_day  # noqa: E402
from src.portfolio_store import load_portfolio  # noqa: E402
from src.schemas import Decision, GateResult, PortfolioState, RiskGateConfig  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("decide_buys")

KST = ZoneInfo("Asia/Seoul")
PENDING_BUYS_PATH = Path("logs/pending_buys.json")


def _make_recording_execute_fn(recorded: list[dict]):
    """run_day가 기대하는 ExecuteFn 형태를 따르되, 포트폴리오는 안 건드리고
    승인된 BUY만 recorded 리스트에 남긴다. sector는 run_day 루프 안에서만
    보이는 값이라 이 콜백을 통해서만 꺼낼 수 있다."""

    async def _record(
        decision: Decision, gate_result: GateResult, portfolio: PortfolioState, sector: str, trade_weight: float
    ) -> PortfolioState:
        if decision.action == "BUY" and gate_result.approved:
            recorded.append(
                {
                    "ticker": decision.ticker,
                    "sector": sector,
                    "trade_weight": trade_weight,
                    "decision": decision.model_dump(mode="json"),
                    "gate_result": gate_result.model_dump(mode="json"),
                }
            )
        return portfolio

    return _record


async def main() -> None:
    today_kst = datetime.now(KST).date()
    if not is_krx_trading_day(today_kst):
        logger.info("not_a_trading_day day=%s — skip (주말/공휴일, LLM 호출 없음)", today_kst.isoformat())
        return

    day = datetime.now(timezone.utc)
    config = RiskGateConfig()
    portfolio = load_portfolio()

    logger.info(
        "decide_buys_start day=%s cash_weight=%.3f positions=%d",
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

    recorded: list[dict] = []
    await pipeline.run_daily(
        day,
        portfolio,
        config,
        analyst_fn,
        judgment.judge,
        _make_recording_execute_fn(recorded),
        total_expected_analysts=3,
    )

    PENDING_BUYS_PATH.parent.mkdir(parents=True, exist_ok=True)
    PENDING_BUYS_PATH.write_text(
        json.dumps({"day": today_kst.isoformat(), "decisions": recorded}, ensure_ascii=False, indent=2)
    )

    logger.info(
        "decide_buys_done day=%s approved=%d tickers=%s",
        today_kst.isoformat(),
        len(recorded),
        [d["ticker"] for d in recorded],
    )


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except Exception as exc:
        logger.exception("decide_buys_failed")
        from src import notify

        notify.send_telegram_alert(notify.format_error_alert("decide_buys 전체 실패, 오늘 신규 매수 판단이 안 됨", repr(exc)))
        raise
