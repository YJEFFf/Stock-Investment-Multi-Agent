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
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

# `python scripts/run_daily.py`로 직접 실행하면 sys.path[0]이 scripts/ 자신이라
# `from src import ...`가 실패한다 (pytest는 conftest.py 덕에 루트가 자동으로
# 잡히지만, 스크립트 직접 실행은 그 메커니즘을 안 탄다) — 레포 루트를 명시적으로 추가.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src import judgment, kis, notify, notion_sync, pipeline, sell  # noqa: E402
from src.market_calendar import is_krx_trading_day  # noqa: E402
from src.portfolio_store import load_portfolio as _load_portfolio  # noqa: E402
from src.portfolio_store import save_portfolio as _save_portfolio  # noqa: E402
from src.schemas import PortfolioState, RiskGateConfig  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("run_daily")

KST = ZoneInfo("Asia/Seoul")

# AGENTS.md "감시 지표" 창. 20영업일 ≈ 달력일 30일(주말+공휴일 감안 여유치) — 이 숫자는
# 판단에 쓰이는 문턱이 아니라 리포트 조회창일 뿐이라 정밀할 필요가 없다.

# NOTE(2026-08-11): 이 스크립트는 더 이상 cron에 안 걸려 있다. 하루 파이프라인이
# A(decide_buys, 07:00) / B(execute_open, 09:00) / C(check_stop_loss, 1분마다) /
# D(decide_llm_sell, 15:35) 네 단계로 쪼개졌다(docs/PLAN.md 참고) — 매도가 먼저
# 자리잡아야 한다는 원칙(§ "매도 로직 일별 배선")과 이번 스케줄 분리는 다른
# 이유(시세 타이밍)로 갈라진 것뿐, 로직 자체는 그대로 재사용한다. 이 파일은
# 로컬/수동으로 전체 흐름을 한 번에 돌려보고 싶을 때 쓰는 개발용 진입점으로
# 남겨둔다.


async def main() -> None:
    from src import operating_mode
    if operating_mode.EXIT_ONLY:
        logger.info("exit_only: buy analysis disabled")
        return
    raise RuntimeError("run_daily is retired; use the scheduled decide_buys/execute_open/check_stop_loss entry points")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except Exception as exc:
        logger.exception("run_daily_failed")
        notify.send_telegram_alert(
            notify.format_error_alert("run_daily 전체 실패, 그날 매수/매도 판단이 안 돌았다", repr(exc))
        )
        raise
