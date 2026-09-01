"""장 시작 직후(09:01 KST cron) 집행 전담 진입점 — 매도 먼저, 그다음 매수.

두 개의 "판단은 끝났지만 아직 집행 안 된" 결과를 실제 KIS 모의투자 주문으로
집행한다:
1. logs/pending_sells.json — 전날 장 마감 후(scripts/decide_llm_sell.py, 15:35)
   LLM이 재량으로 정한 매도. 장이 닫혀 있을 때는 주문을 넣을 수 없어 하루
   미뤄둔 것이다. 매도는 갭 체크 없이 그대로 집행한다(사용자 확정 — 매도는
   망설이지 않는다, src/sell.py 상단 주석과 같은 원칙).
2. logs/pending_buys.json — 오늘 아침(scripts/decide_buys.py, 08:30) 판단해둔
   신규 매수. pipeline.execute_buy_order의 갭 체크(±3%)가 전일 데이터 기준
   판단과 오늘 시가 사이 시차를 처리한다.

매도를 먼저 집행해서 생긴 현금 여유는 이미 08:30에 확정된 매수 결정 자체를
바꾸지 않는다(게이트 재판정이 아니라 이미 승인된 결정의 순차 집행) — 단지 매도
주문이 매수 주문보다 먼저 나갈 뿐이다.

집행이 끝나면 락을 놓기 전에 손절/익절을 한 번 평가한다(3). 이 스크립트가 락을
쥔 1분 동안 check_stop_loss가 스스로 건너뛰기 때문에, 이게 없으면 매 거래일
09:01이 안전장치의 사각지대가 된다 — main() 안 주석 참고.

logs/portfolio_state.json은 이 스크립트 실행 동안 계속 락을 쥐고 있는다 —
매도·매수 둘 다 시장가 주문이라 오래 걸리지 않고(초 단위), 뒤에 붙은 손절/익절
평가도 FAST_FAIL_POLICY라 5종목 기준 최악 약 38초다. 정상일 때는 몇 초로 끝나
09:02 회차를 밀지 않는다. KIS가 그만큼 느린 날이면 09:02도 어차피 시세를 못 받는
회차라, 락 때문에 잃는 게 따로 없다.

실행: uv run python scripts/execute_open.py (레포 루트에서)
"""

import asyncio
import json
import logging
import sys
from datetime import datetime
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

    # 집행하지 못한 매도 중 **다시 시도하면 될 수도 있는 것**만 남긴다.
    # 규칙 4가 금지하는 "결과가 마음에 안 들어서 재분석"이 아니다 — 판단은 이미
    # 끝났고 바뀌지 않는다(같은 SellAction을 그대로 다시 낸다). 시세 조회라는
    # 데이터 수집이 실패한 것이라 재시도 대상이 맞다.
    carried_over: list[dict] = []

    for raw_action in payload["actions"]:
        action = SellAction.model_validate(raw_action)
        position = next((p for p in portfolio.positions if p.ticker == action.ticker), None)
        if position is None:
            # 이미 안 들고 있다(장중 손절이 먼저 털었거나 전량 청산됨) — 팔 것이
            # 없으므로 남겨봐야 영원히 못 판다. 이건 버린다.
            logger.warning("pending_sell_skipped ticker=%s reason=position_not_found", action.ticker)
            continue

        current_price = await asyncio.to_thread(kis.fetch_current_price, action.ticker)
        if current_price is None:
            # 아직 들고 있는데 시세만 못 받았다. 여기서 버리면 LLM이 "나가라"고
            # 한 포지션을 아무도 모르게 계속 들고 있게 된다 — 매도는 망설이면
            # 손실을 들고 있는 쪽이라 침묵이 안전한 방향이 아니다(src/sell.py 상단).
            logger.warning("pending_sell_carried_over ticker=%s reason=price_unavailable", action.ticker)
            carried_over.append(raw_action)
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

    if carried_over:
        # decided_on은 원래 값을 그대로 둔다 — 그래야 STALE_PENDING_SELLS_DAYS가
        # 계속 세어져서 무한정 재시도되지 않고 4일 뒤엔 스스로 만료된다.
        PENDING_SELLS_PATH.write_text(
            json.dumps(
                {"decided_on": payload["decided_on"], "actions": carried_over},
                ensure_ascii=False,
                indent=2,
            )
        )
        notify.send_telegram_alert(
            notify.format_error_alert(
                "매도 집행 실패 — 시세 조회 불가, 다음 거래일 09:01에 재시도합니다",
                f"{len(carried_over)}건 이월: {', '.join(pipeline.display_name(a['ticker']) for a in carried_over)}",
            )
        )
    else:
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

    # KST다(UTC 아님). 이 값의 .date()가 매매일지의 `day`와 보유기간 계산으로
    # 그대로 들어가므로, 하루의 경계는 장이 도는 시간대여야 한다 — pipeline._kst_today
    # docstring의 832fb8b와 같은 버그다. 09:01 KST는 00:01 UTC라 지금까지는 우연히
    # 같은 날짜였지만, 크론이 09:00보다 조금이라도 앞당겨지는 순간 하루가 밀린다.
    day = datetime.now(KST)

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

        # 락을 놓기 전에 손절/익절을 한 번 평가한다. 이게 없으면 **매 거래일 09:01이
        # 통째로 안전장치의 사각지대**다: check_stop_loss는 매분 돌지만 이 스크립트가
        # 그 1분 내내 포트폴리오 락을 쥐고 있어 09:01 회차가 스스로 건너뛴다
        # (check_stop_loss.py의 PortfolioLockBusy 경로). 그 스킵이 무해하다는 근거는
        # "도는 회차가 같은 평가를 하므로"였는데, 락을 쥔 게 이 스크립트일 때는
        # 그 전제가 성립하지 않는다 — execute_open은 손절/익절을 평가하지 않는다.
        #
        # 하필 09:01은 하루 중 변동이 가장 큰 1분이고, 이 구멍은 운이 아니라 크론
        # 구조상 **매일 같은 자리에** 생긴다. 2026-08-27에 실제로 대가를 치렀다:
        # 192820의 그날 저가 271,000이 09:01 분봉에 찍혔고(트레일 라인 275,745),
        # 그 회차가 스킵돼 트레일링 익절이 나가지 않았다.
        #
        # 방금 산 종목도 같이 평가되지만 진입가=고점이라 문턱에 닿을 수 없다.
        portfolio = await pipeline.evaluate_holdings(portfolio, day, sell.execute_sell_order)

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
