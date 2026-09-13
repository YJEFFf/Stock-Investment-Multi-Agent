"""scripts/measure_ic.py — 네트워크 없이 끝까지 돌려 기록·알림 모양을 확인한다."""

import json
import sys
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import scripts.measure_ic as mic
from src import evaluation, pipeline
from src.schemas import OHLCVBar


def _bars(n: int, start: date = date(2026, 8, 3)) -> list[OHLCVBar]:
    out, d = [], start
    while len(out) < n:
        if d.weekday() < 5:
            px = 100 + len(out)
            out.append(OHLCVBar(date=d, open=px, high=px, low=px, close=px, volume=1))
        d += timedelta(days=1)
    return out


def test_noop_on_a_non_trading_day(monkeypatch):
    monkeypatch.setattr(mic, "is_krx_trading_day", lambda day: False)
    monkeypatch.setattr(mic.kis, "fetch_daily_ohlcv", lambda *a, **k: (_ for _ in ()).throw(AssertionError))

    assert mic.main() == 0
    assert not evaluation.DEFAULT_IC_SUMMARY_PATH.exists()


def test_writes_both_segments_and_alerts_without_touching_trading_state(monkeypatch):
    monkeypatch.setattr(mic, "is_krx_trading_day", lambda day: True)
    monkeypatch.setattr(mic, "FETCH_PAUSE_S", 0.0)
    bars = _bars(40)
    rows = [
        {"day": bars[25].date.isoformat(), "ticker": t, "avg_score": s}
        for t, s in (("A", 0.9), ("B", 0.5), ("C", 0.1))
    ]
    pipeline.DEFAULT_LOG_PATH.write_text("\n".join(json.dumps(r) for r in rows) + "\n")
    monkeypatch.setattr(mic.kis, "fetch_daily_ohlcv", lambda ticker, n: bars)
    monkeypatch.setattr(mic.collectors, "fetch_kospi200_index_bars", lambda n: bars)
    alerts = []
    monkeypatch.setattr(mic.notify, "send_telegram_alert", lambda m: alerts.append(m) or True)

    assert mic.main() == 0

    summary = json.loads(evaluation.DEFAULT_IC_SUMMARY_PATH.read_text())
    assert set(summary["segments"]) == set(evaluation.SEGMENTS)
    assert set(summary["segments"]["all"]["horizons"]) == {str(k) for k in evaluation.HORIZONS}
    assert (evaluation.DEFAULT_PRICE_HISTORY_DIR / "A.json").exists()
    assert (evaluation.DEFAULT_PRICE_HISTORY_DIR / f"{evaluation.INDEX_KEY}.json").exists()
    assert len(alerts) == 1 and "신호 IC" in alerts[0]
