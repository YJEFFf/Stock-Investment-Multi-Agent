"""장 마감 후(15:35 KST cron) 보유 종목 LLM 재량 매도 판단만 하는 진입점 —
집행은 안 한다.

개장 직후엔 변동성이 커서 재평가 기준으로 나쁘다는 사용자 판단에 따라, 하루
가격 흐름이 정리된 장 마감 후로 옮겼다. 장이 닫힌 뒤엔 KIS가 어차피 주문을
거부하므로(오늘 실측한 "모의투자 장종료" 응답과 같은 카테고리) 지금 집행을
시도할 이유가 없다 — 판단 결과를 logs/pending_sells.json에 남기고, 실제 매도
주문은 다음 거래일 장 시작 직후 scripts/execute_open.py가 낸다(신규 매수보다
먼저).

결정론적 손절/익절은 여기서 다시 체크하지 않는다 — scripts/check_stop_loss.py가
장중 매분 이미 돌았으므로(마지막 실행 15:30) 트리거될 조건이었다면 이미
처리됐다. 이 스크립트는 순수하게 LLM 재량 재평가만 담당한다.

portfolio_state.json은 원자적으로 쓰이므로(os.replace) 읽기만 할 때는 락이
필요 없다 — 이 스크립트는 상태를 안 바꾼다.

실행: uv run python scripts/decide_llm_sell.py (레포 루트에서)
"""

import asyncio
import json
import logging
import sys
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src import judgment, kis, notify, pipeline  # noqa: E402
from src.market_calendar import is_krx_trading_day  # noqa: E402
from src.portfolio_store import load_portfolio  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("decide_llm_sell")

KST = ZoneInfo("Asia/Seoul")
PENDING_SELLS_PATH = Path("logs/pending_sells.json")


async def main() -> None:
    today_kst = datetime.now(KST).date()
    if not is_krx_trading_day(today_kst):
        logger.info("not_a_trading_day day=%s — skip", today_kst.isoformat())
        return

    day = datetime.now(timezone.utc)
    portfolio = load_portfolio()

    actions: list[dict] = []

    if portfolio.positions:
        analyst_fn = pipeline.make_combined_analyst_fn(
            [
                pipeline.make_chart_analyst_fn(),
                pipeline.make_news_analyst_fn(),
                pipeline.make_disclosure_analyst_fn(),
            ]
        )

        for position in portfolio.positions:
            current_price = await asyncio.to_thread(kis.fetch_current_price, position.ticker)
            if current_price is None:
                logger.warning("decide_llm_sell_skipped ticker=%s reason=price_unavailable", position.ticker)
                continue

            try:
                opinions = await analyst_fn(position.ticker, position.sector, day)
            except Exception as exc:  # noqa: BLE001 - 재평가 실패는 그 종목만 스킵
                logger.warning("decide_llm_sell_analysis_failed ticker=%s error=%s", position.ticker, exc)
                continue

            unrealized_pct = (
                (current_price - position.entry_price) / position.entry_price if position.entry_price else 0.0
            )
            action = await judgment.judge_sell(position.ticker, opinions, unrealized_pct)
            if action is not None:
                actions.append(action.model_dump(mode="json"))

    PENDING_SELLS_PATH.parent.mkdir(parents=True, exist_ok=True)
    PENDING_SELLS_PATH.write_text(
        json.dumps({"decided_on": today_kst.isoformat(), "actions": actions}, ensure_ascii=False, indent=2)
    )

    logger.info(
        "decide_llm_sell_done day=%s decided=%d tickers=%s",
        today_kst.isoformat(),
        len(actions),
        [a["ticker"] for a in actions],
    )


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except Exception as exc:
        logger.exception("decide_llm_sell_failed")
        notify.send_telegram_alert(
            notify.format_error_alert("decide_llm_sell 전체 실패, 오늘 LLM 재량 매도 판단이 안 됨", repr(exc))
        )
        raise
