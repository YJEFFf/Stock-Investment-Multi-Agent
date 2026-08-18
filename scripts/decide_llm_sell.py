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
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src import judgment, kis, notify, notion_sync, pipeline  # noqa: E402
from src.market_calendar import is_krx_trading_day  # noqa: E402
from src.portfolio_store import load_portfolio  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("decide_llm_sell")

KST = ZoneInfo("Asia/Seoul")
PENDING_SELLS_PATH = Path("logs/pending_sells.json")


async def _sync_daily_report(today_kst, portfolio) -> None:
    """오늘 하루(판단·매수·매도·최종 보유 종목)를 노션 일일 리포트로 남긴다.
    여기서 부르는 이유: check_stop_loss.py가 09:00-15:30 매분 결정론적
    손절/익절로 보유 종목을 바꿀 수 있어서, 이 스크립트(15:35, 장마감 후)가
    도는 시점의 portfolio가 그날의 진짜 "장마감 기준" 스냅샷이다 — 이
    스크립트 자신은 상태를 안 바꾸므로(판단만 하고 집행은 다음날 아침) 그
    스냅샷이 여기서 흔들리지 않는다.

    워크스페이스가 아직 설정 안 됐으면(scripts/setup_notion_workspace.py
    미실행) 조용히 건너뛴다 — 노션 연동은 부가 기능이라 이것 때문에
    decide_llm_sell 자체가 실패하면 안 된다.

    "총정리"(투자한 금액/남은 현금/총 금액)에 쓸 절대 원화 금액은
    PortfolioState(비중만 있음)로는 못 만든다 — kis.fetch_account_balance()로
    브로커의 실제 총평가금액을 조회해서 넘긴다. 조회 실패해도(None) 리포트
    자체는 만든다 — 그 절만 생략된다(notion_sync.sync_daily_report 참고)."""
    daily_report_db_id = os.environ.get("NOTION_DAILY_REPORT_DB_ID")
    if not daily_report_db_id:
        logger.info("notion_daily_report_sync_skipped reason=not_configured")
        return

    total_value = kis.fetch_account_balance()

    try:
        created = await notion_sync.sync_daily_report(
            today_kst.isoformat(), portfolio, daily_report_db_id, total_value=total_value
        )
        logger.info("notion_daily_report_synced=%s", created)
    except Exception as exc:
        logger.exception("notion_daily_report_sync_failed")
        notify.send_telegram_alert(notify.format_error_alert("노션 일일 리포트 동기화 실패 (매매 자체엔 영향 없음)", repr(exc)))


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

    names = [pipeline.display_name(a["ticker"]) for a in actions]
    notify.send_telegram_alert(notify.format_sell_decision_alert(today_kst.isoformat(), names))

    logger.info(
        "decide_llm_sell_done day=%s decided=%d tickers=%s",
        today_kst.isoformat(),
        len(actions),
        [a["ticker"] for a in actions],
    )

    # 매매일지를 일일 리포트보다 먼저 올린다. 장중 매도(check_stop_loss는 1분마다
    # 돈다)는 09:00 동기화 이후에 나므로, 여기서 한 번 더 돌려야 그날 안에 보인다.
    # 안 그러면 오늘 판 종목이 내일 아침에야 매매일지에 나타난다(2026-08-18 실측).
    await notion_sync.sync_trade_journal_if_configured(pipeline.DEFAULT_TRADE_JOURNAL_LOG_PATH)
    await _sync_daily_report(today_kst, portfolio)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except Exception as exc:
        logger.exception("decide_llm_sell_failed")
        notify.send_telegram_alert(
            notify.format_error_alert("decide_llm_sell 전체 실패, 오늘 LLM 재량 매도 판단이 안 됨", repr(exc))
        )
        raise
