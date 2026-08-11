"""장중(09:00-15:30 KST) 매분 도는 결정론적 손절/익절 체크 — LLM 없음, 코드만.

pipeline.evaluate_holdings에 analyst_fn/judge_sell_fn을 안 넣으면 결정론적
안전장치(src/sell.py의 손절 -10%/트레일링 익절 +20%)만 도는 모드가 이미
구현되어 있다(src/pipeline.py:622-639) — 이 스크립트는 그 모드를 1분 간격으로
부르기만 한다. 추가 LLM 비용 없음(사용자 확인) — KIS 시세 조회만 나간다.

이전 프로젝트는 이 체크를 1분 간격으로 돌렸다: 하루 한 번만 체크하면 그 사이
(최대 24시간) 손실이 -10% 문턱을 훨씬 넘어갈 수 있어 안전장치 취지가 무너진다.

포지션이 없으면 evaluate_holdings가 바로 반환하므로 가볍다.

실행: uv run python scripts/check_stop_loss.py (레포 루트에서)
"""

import asyncio
import logging
import sys
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src import notify, pipeline, sell  # noqa: E402
from src.market_calendar import is_krx_trading_day  # noqa: E402
from src.portfolio_store import load_portfolio, portfolio_lock, save_portfolio  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("check_stop_loss")

KST = ZoneInfo("Asia/Seoul")
# 매분 도는 잡이라 지속 장애 시 텔레그램이 하루 수백 번 울릴 수 있다 — 하루 첫
# 실패에만 알리고 그 이후는 로그(logs/cron.log)로만 남긴다.
LAST_ALERT_MARKER_PATH = Path("logs/check_stop_loss_last_alert.txt")


def _alert_once_per_day(context: str, error: str) -> None:
    today = datetime.now(KST).date().isoformat()
    if LAST_ALERT_MARKER_PATH.exists() and LAST_ALERT_MARKER_PATH.read_text().strip() == today:
        return
    notify.send_telegram_alert(notify.format_error_alert(context, error))
    LAST_ALERT_MARKER_PATH.parent.mkdir(parents=True, exist_ok=True)
    LAST_ALERT_MARKER_PATH.write_text(today)


async def main() -> None:
    today_kst = datetime.now(KST).date()
    if not is_krx_trading_day(today_kst):
        return  # 주말/공휴일 — 로그도 안 남긴다(매분 도는 잡이라 휴장일에 조용한 게 정상)

    day = datetime.now(timezone.utc)

    with portfolio_lock():
        portfolio = load_portfolio()
        if not portfolio.positions:
            return

        portfolio = await pipeline.evaluate_holdings(portfolio, day, sell.execute_sell_order)
        save_portfolio(portfolio)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except Exception as exc:
        logger.exception("check_stop_loss_failed")
        _alert_once_per_day("check_stop_loss 실패 (이번 1분 체크만 스킵, 이후 반복 실패는 로그만)", repr(exc))
        raise
