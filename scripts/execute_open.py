"""장 시작 직후(09:00 KST cron) 집행 전담 진입점 — 매도 먼저, 그다음 매수.

두 개의 "판단은 끝났지만 아직 집행 안 된" 결과를 실제 KIS 모의투자 주문으로
집행한다:
1. logs/pending_sells.json — 전날 장 마감 후(scripts/decide_llm_sell.py, 15:35)
   LLM이 재량으로 정한 매도. 장이 닫혀 있을 때는 주문을 넣을 수 없어 하루
   미뤄둔 것이다. 매도는 갭 체크 없이 그대로 집행한다(사용자 확정 — 매도는
   망설이지 않는다, src/sell.py 상단 주석과 같은 원칙).
2. logs/pending_buys.json — 오늘 아침(scripts/decide_buys.py, 07:00) 판단해둔
   신규 매수. pipeline.execute_buy_order의 갭 체크(±3%)가 전일 데이터 기준
   판단과 오늘 시가 사이 시차를 처리한다.

매도를 먼저 집행해서 생긴 현금 여유는 이미 07:00에 확정된 매수 결정 자체를
바꾸지 않는다(게이트 재판정이 아니라 이미 승인된 결정의 순차 집행) — 단지 매도
주문이 매수 주문보다 먼저 나갈 뿐이다.

logs/portfolio_state.json은 이 스크립트 실행 동안 계속 락을 쥐고 있는다 —
매도·매수 둘 다 시장가 주문이라 오래 걸리지 않으므로(초 단위) 락을 오래
들고 있는 게 문제되지 않는다.

실행: uv run python scripts/execute_open.py (레포 루트에서)
"""

import asyncio
import json
import logging
import sys
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src import kis, notify, notion_sync, pipeline, sell  # noqa: E402
from src.market_calendar import is_krx_trading_day  # noqa: E402
from src.portfolio_store import load_portfolio, portfolio_lock, save_portfolio  # noqa: E402
from src.schemas import Decision, GateResult, SellAction  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("execute_open")

KST = ZoneInfo("Asia/Seoul")
PENDING_SELLS_PATH = Path("logs/pending_sells.json")
PENDING_BUYS_PATH = Path("logs/pending_buys.json")

# pending_sells.json이 이보다 오래됐으면(예: decide_llm_sell.py가 며칠째 실패)
# 근거가 너무 낡았다고 보고 실행하지 않는다 — 주말+짧은 연휴를 감안한 여유치.
STALE_PENDING_SELLS_DAYS = 4


async def _execute_pending_sells(portfolio, day, today_kst):
    if not PENDING_SELLS_PATH.exists():
        return portfolio

    payload = json.loads(PENDING_SELLS_PATH.read_text())
    decided_on = datetime.fromisoformat(payload["decided_on"]).date()
    if (today_kst - decided_on).days > STALE_PENDING_SELLS_DAYS:
        logger.warning("pending_sells_stale decided_on=%s today=%s — skip", decided_on, today_kst)
        notify.send_telegram_alert(
            notify.format_error_alert(
                "pending_sells.json이 너무 오래됨 (decide_llm_sell 실패 의심)",
                f"decided_on={decided_on}",
            )
        )
        PENDING_SELLS_PATH.unlink()
        return portfolio

    for raw_action in payload["actions"]:
        action = SellAction.model_validate(raw_action)
        position = next((p for p in portfolio.positions if p.ticker == action.ticker), None)
        if position is None:
            logger.warning("pending_sell_skipped ticker=%s reason=position_not_found", action.ticker)
            continue

        current_price = await asyncio.to_thread(kis.fetch_current_price, action.ticker)
        if current_price is None:
            logger.warning("pending_sell_skipped ticker=%s reason=price_unavailable", action.ticker)
            continue

        portfolio = await pipeline.finalize_sell(
            portfolio,
            action,
            position,
            current_price,
            day,
            sell.execute_sell_order,
            pipeline.DEFAULT_SELL_LOG_PATH,
            pipeline.DEFAULT_TRADE_JOURNAL_LOG_PATH,
        )

    PENDING_SELLS_PATH.unlink()
    return portfolio


async def _execute_pending_buys(portfolio, today_kst):
    if not PENDING_BUYS_PATH.exists():
        logger.info("pending_buys_missing — 오늘 신규 매수 없음(A가 안 돌았거나 승인된 후보가 0개)")
        return portfolio

    payload = json.loads(PENDING_BUYS_PATH.read_text())
    if payload["day"] != today_kst.isoformat():
        logger.warning("pending_buys_day_mismatch pending_day=%s today=%s — skip", payload["day"], today_kst)
        notify.send_telegram_alert(
            notify.format_error_alert(
                "pending_buys.json 날짜 불일치 (decide_buys가 오늘 아침 제시간에 못 끝났을 가능성)",
                f"pending_day={payload['day']} today={today_kst.isoformat()}",
            )
        )
        return portfolio

    for item in payload["decisions"]:
        decision = Decision.model_validate(item["decision"])
        gate_result = GateResult.model_validate(item["gate_result"])
        portfolio = await pipeline.execute_buy_order(
            decision, gate_result, portfolio, item["sector"], item["trade_weight"]
        )

    PENDING_BUYS_PATH.unlink()
    return portfolio


async def _sync_trade_journal() -> None:
    """방금 쌓인 매수·매도 이벤트를 노션 매매일지로 올린다.

    일일 리포트(장마감 기준 스냅샷)는 여기서 만들지 않는다 — 그날 보유 종목은
    09:00 이후 check_stop_loss.py가 장중에도 계속 바꿀 수 있어서, 이 시점
    스냅샷을 "장마감 기준"이라고 부르면 틀린 라벨이 된다. 일일 리포트는
    decide_llm_sell.py(15:35, 장마감 후)에서 만든다.
    """
    await notion_sync.sync_trade_journal_if_configured(pipeline.DEFAULT_TRADE_JOURNAL_LOG_PATH)


async def main() -> None:
    today_kst = datetime.now(KST).date()
    if not is_krx_trading_day(today_kst):
        logger.info("not_a_trading_day day=%s — skip", today_kst.isoformat())
        return

    day = datetime.now(timezone.utc)

    with portfolio_lock():
        portfolio = load_portfolio()
        logger.info(
            "execute_open_start day=%s cash_weight=%.3f positions=%d",
            today_kst.isoformat(),
            portfolio.cash_weight,
            len(portfolio.positions),
        )

        portfolio = await _execute_pending_sells(portfolio, day, today_kst)
        portfolio = await _execute_pending_buys(portfolio, today_kst)

        save_portfolio(portfolio)

    logger.info(
        "execute_open_done cash_weight=%.3f positions=%d",
        portfolio.cash_weight,
        len(portfolio.positions),
    )

    await _sync_trade_journal()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except Exception as exc:
        logger.exception("execute_open_failed")
        notify.send_telegram_alert(notify.format_error_alert("execute_open 전체 실패, 오늘 매도/매수 집행이 안 됨", repr(exc)))
        raise
