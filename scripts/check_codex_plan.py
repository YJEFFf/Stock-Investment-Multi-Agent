"""08:05 KST — API 키 없이 ChatGPT 플랜의 Codex 비대화형 호출 상태를 확인한다.

주식 데이터·판단·주문은 전혀 다루지 않는다. 신규 매수는 계속 정지된 상태에서
EC2의 로그인, 플랜 한도, Sol 구조화 출력이 다음 단계에 쓸 수 있는지만 한 번 확인한다.
"""

import asyncio
import logging
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src import codex_plan, notify  # noqa: E402
from src.market_calendar import is_krx_trading_day  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("check_codex_plan")
KST = ZoneInfo("Asia/Seoul")


async def main() -> None:
    today = datetime.now(KST).date()
    if not is_krx_trading_day(today):
        return
    # 같은 날 수동 재실행이 뒤에서 실패해도 앞선 "허용" 파일이 남아 08:30에 쓰이지 않게 한다.
    codex_plan.DEFAULT_CAPACITY_STATE_PATH.unlink(missing_ok=True)
    result = await codex_plan.check_health()
    logger.info("codex_plan_health_check_ok day=%s status=%s", today.isoformat(), result.status)
    capacity = await codex_plan.read_capacity()
    codex_plan.save_capacity_status(capacity)
    notify.send_telegram_alert(notify.format_codex_capacity_alert(capacity.model_dump(mode="json")))
    logger.info(
        "codex_plan_capacity day=%s remaining=%.1f allowed=%s reset=%s",
        today.isoformat(), capacity.remaining_percent, capacity.buy_judgment_allowed, capacity.resets_at.isoformat(),
    )


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except Exception as exc:
        logger.exception("codex_plan_health_check_failed")
        notify.send_telegram_alert(
            notify.format_error_alert("Codex 플랜 연결·한도 점검 실패 — 오늘 신규 매수 판단 중지", repr(exc))
        )
        raise
