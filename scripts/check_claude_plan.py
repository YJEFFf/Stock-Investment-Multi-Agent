"""06:55 KST — API 키 없이 Claude 구독의 Claude Code 비대화형 호출 상태를 확인한다.

주식 데이터·판단·주문은 전혀 다루지 않는다. EC2의 구독 로그인, 동시 호출, 구조화
출력과 응답에 딸려 오는 5시간·7일 한도가 07:00 판단에 쓸 수 있는지만 확인한다.
2026-11-23 Codex 구독 종료로 check_codex_plan.py를 대체했다.
"""

import asyncio
import logging
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src import claude_plan, notify  # noqa: E402
from src.market_calendar import is_krx_trading_day  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("check_claude_plan")
KST = ZoneInfo("Asia/Seoul")


async def main() -> None:
    from src import operating_mode
    if operating_mode.EXIT_ONLY:
        return
    today = datetime.now(KST).date()
    if not is_krx_trading_day(today):
        return
    # 같은 날 수동 재실행이 뒤에서 실패해도 앞선 "허용" 파일이 남아 07:00에 쓰이지 않게 한다.
    claude_plan.DEFAULT_CAPACITY_STATE_PATH.unlink(missing_ok=True)
    # 단일 성공만으로 운영 동시 호출을 허용하면 10/21·22의429폭주 같은 상황을 놓친다.
    sinks = [[] for _ in range(claude_plan.MAX_CONCURRENT_CALLS)]
    results = await asyncio.gather(*(claude_plan.check_health(limits_sink=sink) for sink in sinks),
                                   return_exceptions=True)
    failures = [result for result in results if isinstance(result, BaseException)]
    if failures:
        raise claude_plan.PlanUnavailable(f"Concurrent Claude readiness failed: {failures[0]}")
    logger.info("claude_plan_health_check_ok day=%s concurrent=%d", today.isoformat(), len(results))
    capacity = claude_plan.capacity_from_limits(sinks[-1][-1] if sinks[-1] else None)
    claude_plan.save_capacity_status(capacity)
    if not capacity.buy_judgment_allowed:
        notify.send_telegram_alert(notify.format_plan_capacity_alert(capacity.model_dump(mode="json")))
    logger.info(
        "claude_plan_capacity day=%s remaining=%.1f five_hour_used=%.1f allowed=%s reset=%s",
        today.isoformat(), capacity.remaining_percent, capacity.five_hour_used_percent,
        capacity.buy_judgment_allowed, capacity.resets_at.isoformat(),
    )


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except Exception as exc:
        logger.exception("claude_plan_health_check_failed")
        notify.send_telegram_alert(
            notify.format_error_alert("Claude 구독 연결·한도 점검 실패 — 오늘 신규 매수 판단 중지", repr(exc))
        )
        raise
