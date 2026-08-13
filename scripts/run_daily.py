"""cron으로 매일 실행되는 진입점 — 보유 종목 매도 평가 후 신규 매수 판단까지
하루치 전체 파이프라인을 실제 KIS 모의투자 주문·실제 Claude API로 돌린다.

포트폴리오 상태(보유 종목·현금 비중)는 logs/portfolio_state.json에 영속화한다 —
각 실행이 이전 실행의 포지션을 이어받아야 하므로, 프로세스가 매번 새로 뜨는
cron 환경에서는 파일이 유일한 상태 저장소다.

실행: uv run python scripts/run_daily.py (레포 루트에서)
"""

import asyncio
import logging
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

# `python scripts/run_daily.py`로 직접 실행하면 sys.path[0]이 scripts/ 자신이라
# `from src import ...`가 실패한다 (pytest는 conftest.py 덕에 루트가 자동으로
# 잡히지만, 스크립트 직접 실행은 그 메커니즘을 안 탄다) — 레포 루트를 명시적으로 추가.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src import judgment, kis, llm, notify, notion_sync, pipeline, sell  # noqa: E402
from src.market_calendar import is_krx_trading_day  # noqa: E402
from src.portfolio_store import load_portfolio as _load_portfolio  # noqa: E402
from src.portfolio_store import save_portfolio as _save_portfolio  # noqa: E402
from src.schemas import PortfolioState, RiskGateConfig  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("run_daily")

KST = ZoneInfo("Asia/Seoul")

# CLAUDE.md "감시 지표" 창. 20영업일 ≈ 달력일 30일(주말+공휴일 감안 여유치) — 이 숫자는
# 판단에 쓰이는 문턱이 아니라 리포트 조회창일 뿐이라 정밀할 필요가 없다.
MONITORING_WINDOW_TRADING_DAYS = 20
MONITORING_WINDOW_CALENDAR_DAYS = 30

# NOTE(2026-08-11): 이 스크립트는 더 이상 cron에 안 걸려 있다. 하루 파이프라인이
# A(decide_buys, 07:00) / B(execute_open, 09:00) / C(check_stop_loss, 1분마다) /
# D(decide_llm_sell, 15:35) 네 단계로 쪼개졌다(docs/PLAN.md 참고) — 매도가 먼저
# 자리잡아야 한다는 원칙(§ "매도 로직 일별 배선")과 이번 스케줄 분리는 다른
# 이유(시세 타이밍)로 갈라진 것뿐, 로직 자체는 그대로 재사용한다. 이 파일은
# 로컬/수동으로 전체 흐름을 한 번에 돌려보고 싶을 때 쓰는 개발용 진입점으로
# 남겨둔다.


async def main() -> None:
    today_kst = datetime.now(KST).date()
    if not is_krx_trading_day(today_kst):
        logger.info("not_a_trading_day day=%s — skip (주말/공휴일, LLM 호출 없음)", today_kst.isoformat())
        return

    # KST로 만든다(UTC 아님) — scripts/decide_buys.py와 같은 이유(2026-08-13 발견):
    # 자정 근처 UTC 날짜와 KST 거래일이 어긋나면 pipeline.jsonl의 "day"가
    # notion_sync의 날짜 필터와 하루씩 안 맞게 된다.
    day = datetime.now(KST)
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
    except Exception as exc:
        logger.exception("evaluate_holdings_failed — 매수 판단은 계속 진행한다")
        notify.send_telegram_alert(
            notify.format_error_alert("evaluate_holdings 실패 (매수 판단은 계속 진행)", repr(exc))
        )

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

    _log_monitoring_summary()
    await _sync_notion(today_kst, portfolio)


async def _sync_notion(today_kst, portfolio: PortfolioState) -> None:
    """logs/trade_journal.jsonl의 새 이벤트를 노션 매매일지로 올리고, 오늘 하루를
    요약한 일일 리포트도 남긴다. 워크스페이스가 아직 설정 안 됐으면
    (scripts/setup_notion_workspace.py 미실행) 해당 부분만 조용히 건너뛴다 —
    노션 연동은 부가 기능이라 이것 때문에 run_daily 자체가 실패하면 안 된다."""
    trade_journal_db_id = os.environ.get("NOTION_TRADE_JOURNAL_DB_ID")
    daily_report_db_id = os.environ.get("NOTION_DAILY_REPORT_DB_ID")
    if not trade_journal_db_id and not daily_report_db_id:
        logger.info("notion_sync_skipped reason=not_configured")
        return

    try:
        if trade_journal_db_id:
            summary = await notion_sync.sync_trade_journal(pipeline.DEFAULT_TRADE_JOURNAL_LOG_PATH, trade_journal_db_id)
            logger.info("notion_sync summary=%s", summary)
        if daily_report_db_id:
            total_value = kis.fetch_account_balance()
            created = await notion_sync.sync_daily_report(
                today_kst.isoformat(), portfolio, daily_report_db_id, total_value=total_value
            )
            logger.info("notion_daily_report_synced=%s", created)
    except Exception as exc:
        logger.exception("notion_sync_failed")
        notify.send_telegram_alert(notify.format_error_alert("노션 동기화 실패 (매매 자체엔 영향 없음)", repr(exc)))


def _log_monitoring_summary() -> None:
    """CLAUDE.md "감시 지표"를 매일 cron 로그에 남긴다 — 읽기 전용 리포트, 판단 로직
    어디에도 이 결과를 되먹임하지 않는다(사람이 보라고 남기는 숫자다)."""
    signal_summary = pipeline.summarize_recent_trading_days(
        pipeline.DEFAULT_LOG_PATH, MONITORING_WINDOW_TRADING_DAYS
    )
    logger.info(
        "monitoring_signal_rate days=%d signal_days=%d signal_day_ratio=%.3f rejected_by=%s",
        signal_summary["total_days"],
        signal_summary["signal_days"],
        signal_summary["signal_day_ratio"],
        signal_summary["rejected_by_counts"],
    )

    since = datetime.now(timezone.utc) - timedelta(days=MONITORING_WINDOW_CALENDAR_DAYS)
    llm_summary = pipeline.summarize_llm_calls(llm.DEFAULT_LLM_CALL_LOG_PATH, since=since)
    for label, stats in sorted(llm_summary.items()):
        logger.info(
            "monitoring_llm_calls label=%s calls=%d failure_rate=%.3f input_tokens=%d output_tokens=%d",
            label,
            stats["calls"],
            stats["failure_rate"],
            stats["input_tokens"],
            stats["output_tokens"],
        )


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except Exception as exc:
        logger.exception("run_daily_failed")
        notify.send_telegram_alert(
            notify.format_error_alert("run_daily 전체 실패, 그날 매수/매도 판단이 안 돌았다", repr(exc))
        )
        raise
