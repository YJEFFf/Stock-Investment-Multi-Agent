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
from src.portfolio_store import (  # noqa: E402
    PortfolioLockBusy,
    load_portfolio,
    portfolio_lock,
    save_portfolio,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("check_stop_loss")

KST = ZoneInfo("Asia/Seoul")


async def main() -> None:
    today_kst = datetime.now(KST).date()
    if not is_krx_trading_day(today_kst):
        return  # 주말/공휴일 — 로그도 안 남긴다(매분 도는 잡이라 휴장일에 조용한 게 정상)

    day = datetime.now(timezone.utc)

    # 앞 회차가 아직 돌고 있으면 이번 분은 건너뛴다 — 기다리면 KIS 장애 때
    # 매분 새 프로세스가 쌓인다(portfolio_lock docstring). 앞 회차가 같은 평가를
    # 하고 있으므로 그 경우엔 건너뛰어도 놓치는 게 없다.
    #
    # **락을 쥔 게 execute_open(09:01)일 때는 그 전제가 성립하지 않는다** —
    # 그 스크립트는 주문만 집행하고 손절/익절은 평가하지 않아서, 09:01 회차가
    # 통째로 사라졌다. 2026-08-27에 192820의 당일 저가(09:01, 271,000)가 트레일
    # 라인 275,745 아래였는데 매도가 나가지 않은 원인이다. 그래서 execute_open이
    # 락을 놓기 전에 같은 평가를 한 번 돌리도록 고쳤다(execute_open.main 주석).
    # 여기서 락을 기다리게 만들지 않은 이유는 위와 같다 — 장애 때 프로세스가 쌓인다.
    try:
        with portfolio_lock(blocking=False):
            portfolio = load_portfolio()
            if not portfolio.positions:
                return

            portfolio = await pipeline.evaluate_holdings(portfolio, day, sell.execute_sell_order)
            save_portfolio(portfolio)
    except PortfolioLockBusy:
        logger.info("check_stop_loss_skipped reason=previous_run_still_holding_lock")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except Exception as exc:
        logger.exception("check_stop_loss_failed")
        # 하루 첫 실패에만 알린다(notify.alert_once_per_day) — 매분 도는 잡이라
        # 지속 장애면 텔레그램이 하루 수백 번 울린다. 사유별로 따로 세므로
        # 이 알림이 나간 뒤에도 "시세 전부 실패" 알림은 따로 울린다.
        notify.alert_once_per_day(
            "check_stop_loss_failed",
            notify.format_error_alert(
                "check_stop_loss 실패 (이번 1분 체크만 스킵, 이후 반복 실패는 로그만)", repr(exc)
            ),
        )
        raise
