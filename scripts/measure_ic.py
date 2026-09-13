"""매 거래일 16:00 KST — 매수 신호의 예측력(IC)을 재고 기록한다. 판단에 되먹이지 않는다.

이 스크립트가 생긴 이유: 마일스톤 3(IC 측정)이 목표인 시스템이 2026-08-12부터
09-11까지 22거래일을 **IC를 한 번도 재지 않고** 돌았다(src/evaluation.py docstring).
첫 1개월 평가는 신호가 모든 지평에서 음의 IC(−0.07~−0.11)라는 것을 사후에야 밝혔다.
앞으로는 매일 숫자가 남고, 60거래일이 쌓이는 시점(2026-11 중순)에 이 숫자로
실거래 전환 여부를 판단한다 — IC ≥ 0.03, t ≥ 2가 조건이고, 정직하게 음이면 시스템을
멈추는 것이 결론이다(docs/evaluations/2026-09-13-first-month.md §7).

읽기 전용이다: pipeline.jsonl을 읽고, 가격을 받아 logs/price_history/에 누적하고,
logs/ic_summary.json과 cron.log·텔레그램에 결과를 남긴다. 매매 상태는 건드리지 않는다.

실행: uv run python scripts/measure_ic.py (레포 루트에서)
"""

import json
import logging
import sys
import time
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src import collectors, evaluation, kis, notify, pipeline  # noqa: E402
from src.market_calendar import is_krx_trading_day  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("measure_ic")

KST = ZoneInfo("Asia/Seoul")
FETCH_LOOKBACK = 100  # KIS가 주는 최대치. 누적은 PriceHistory가 맡는다
FETCH_PAUSE_S = 0.15  # 초당 거래건수 제한(EGW00201)에 안 걸리게 종목 사이를 띄운다


def main() -> int:
    today = datetime.now(KST).date()
    if not is_krx_trading_day(today):
        logger.info("not_a_trading_day day=%s — skip", today.isoformat())
        return 0

    entries = evaluation.load_decision_entries(pipeline.DEFAULT_LOG_PATH)
    tickers = sorted({e["ticker"] for e in entries})
    logger.info("measure_ic_start day=%s decisions=%d tickers=%d", today.isoformat(), len(entries), len(tickers))

    history = evaluation.PriceHistory(evaluation.DEFAULT_PRICE_HISTORY_DIR)

    def _fetch_stock(ticker: str):
        bars = kis.fetch_daily_ohlcv(ticker, FETCH_LOOKBACK)
        time.sleep(FETCH_PAUSE_S)
        return bars

    added = history.refresh(tickers, _fetch_stock)
    index_bars = collectors.fetch_kospi200_index_bars(FETCH_LOOKBACK)
    if index_bars:
        history.merge(evaluation.INDEX_KEY, index_bars)
    logger.info(
        "price_history_refreshed tickers_ok=%d tickers_failed=%d new_bars=%d index=%s",
        len(added),
        len(tickers) - len(added),
        sum(added.values()),
        "ok" if index_bars else "failed",
    )

    prices = {t: history.load(t) for t in tickers}
    index = history.load(evaluation.INDEX_KEY)

    segments: dict[str, dict] = {}
    for segment, since in evaluation.SEGMENTS.items():
        segment_entries = evaluation.entries_since(entries, since)
        segments[segment] = {"since": since, "decisions": len(segment_entries), "horizons": {}}
        for k in evaluation.HORIZONS:
            summary = evaluation.summarize_ic(segment_entries, prices, index, k)
            segments[segment]["horizons"][str(k)] = summary
            _log_summary(segment, k, summary)

    evaluation.DEFAULT_IC_SUMMARY_PATH.parent.mkdir(parents=True, exist_ok=True)
    evaluation.DEFAULT_IC_SUMMARY_PATH.write_text(
        json.dumps({"as_of": today.isoformat(), "segments": segments}, ensure_ascii=False, indent=2)
    )
    notify.send_telegram_alert(notify.format_ic_alert(today.isoformat(), segments))
    logger.info("measure_ic_done day=%s", today.isoformat())
    return 0


def _log_summary(segment: str, k: int, summary: dict) -> None:
    logger.info(
        "monitoring_ic segment=%s k=%d days=%d pairs=%d mean_ic=%s std=%s t=%s positive_days=%d "
        "rho_score_mom20=%s quintile_ic=%s skipped_forward=%d skipped_price=%d",
        segment,
        k,
        summary["days_measured"],
        summary["n_pairs"],
        _fmt(summary["mean_ic"]),
        _fmt(summary["std_ic"]),
        _fmt(summary["t_stat"]),
        summary["positive_days"],
        _fmt(summary["rho_score_momentum_20d"]),
        [_fmt(x) for x in summary["momentum_quintile_ic"]],
        summary["skipped_no_forward_data"],
        summary["skipped_no_price"],
    )


def _fmt(value) -> str:
    return "none" if value is None else f"{value:.3f}"


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except SystemExit:
        raise
    except Exception as exc:  # noqa: BLE001 - 측정 실패는 알리되 매매에는 영향 없음
        logger.exception("measure_ic_failed")
        notify.send_telegram_alert(notify.format_error_alert("IC 측정 실패 (매매에는 영향 없음)", repr(exc)))
        raise
