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
    result = await codex_plan.check_health()
    logger.info("codex_plan_health_check_ok day=%s status=%s", today.isoformat(), result.status)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except Exception as exc:
        logger.exception("codex_plan_health_check_failed")
        notify.send_telegram_alert(
            notify.format_error_alert("Codex 플랜 연결 실패 — 신규 매수는 계속 정지 상태", repr(exc))
        )
        raise
