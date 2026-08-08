import json
from datetime import date

import pytest

from src import evaluation
from src.schemas import MarketContext, OHLCVBar


def _bars(closes: list[float], start: date = date(2026, 1, 1)) -> list[OHLCVBar]:
    return [
        OHLCVBar(date=date(start.year, start.month, start.day + i), open=c, high=c, low=c, close=c, volume=1000)
        for i, c in enumerate(closes)
    ]


def test_information_coefficient_perfect_positive_correlation():
    pairs = [(1.0, 10.0), (2.0, 20.0), (3.0, 30.0), (4.0, 40.0)]
    assert evaluation.information_coefficient(pairs) == pytest.approx(1.0)


def test_information_coefficient_perfect_negative_correlation():
    pairs = [(1.0, 40.0), (2.0, 30.0), (3.0, 20.0), (4.0, 10.0)]
    assert evaluation.information_coefficient(pairs) == pytest.approx(-1.0)


def test_information_coefficient_handles_ties():
    pairs = [(1.0, 5.0), (1.0, 5.0), (2.0, 10.0), (3.0, 15.0)]
    ic = evaluation.information_coefficient(pairs)
    assert ic is not None
    assert ic == pytest.approx(1.0)


def test_information_coefficient_returns_none_for_too_few_samples():
    assert evaluation.information_coefficient([]) is None
    assert evaluation.information_coefficient([(1.0, 2.0)]) is None


def test_information_coefficient_returns_none_when_predicted_has_no_variance():
    pairs = [(1.0, 5.0), (1.0, 10.0), (1.0, 15.0)]
    assert evaluation.information_coefficient(pairs) is None


def test_compute_forward_return_success(monkeypatch):
    bars = _bars([100.0, 102.0, 104.0, 106.0, 108.0, 110.0])
    context = MarketContext(ticker="005930", as_of=bars[-1].date, bars=bars, indicators={})
    monkeypatch.setattr(evaluation.collectors, "fetch_market_context", lambda *a, **k: context)

    ret = evaluation.compute_forward_return("005930", as_of=bars[0].date, forward_days=3)

    assert ret == pytest.approx((106.0 - 100.0) / 100.0)


def test_compute_forward_return_none_when_not_enough_future_data(monkeypatch):
    bars = _bars([100.0, 102.0])
    context = MarketContext(ticker="005930", as_of=bars[-1].date, bars=bars, indicators={})
    monkeypatch.setattr(evaluation.collectors, "fetch_market_context", lambda *a, **k: context)

    ret = evaluation.compute_forward_return("005930", as_of=bars[0].date, forward_days=5)

    assert ret is None


def test_compute_forward_return_none_when_collector_fails(monkeypatch):
    monkeypatch.setattr(evaluation.collectors, "fetch_market_context", lambda *a, **k: None)

    ret = evaluation.compute_forward_return("005930", as_of=date(2026, 1, 1), forward_days=5)

    assert ret is None


def test_summarize_ic_aggregates_daily_ic(tmp_path, monkeypatch):
    log_path = tmp_path / "pipeline.jsonl"
    entries = [
        {"day": "2026-01-05", "ticker": "005930", "avg_score": 0.5},
        {"day": "2026-01-05", "ticker": "000660", "avg_score": -0.2},
        {"day": "2026-01-06", "ticker": "005930", "avg_score": 0.1},
        {"day": "2026-01-06", "ticker": "000660", "avg_score": 0.9},
        {"day": "2026-01-06", "ticker": "035420", "avg_score": None},  # avg_score 없음 — 스킵
    ]
    with log_path.open("w") as f:
        for e in entries:
            f.write(json.dumps(e) + "\n")

    # ticker별 forward_return을 고정값으로 모킹 — 실제 순위상관 계산 로직만 검증한다.
    fake_returns = {
        ("005930", "2026-01-05"): 0.03,
        ("000660", "2026-01-05"): -0.01,
        ("005930", "2026-01-06"): None,  # 아직 데이터 없음 — skipped 카운트에 반영
        ("000660", "2026-01-06"): 0.02,
    }

    def fake_forward_return(ticker, as_of, forward_days, lookback_days=250):
        return fake_returns.get((ticker, as_of.isoformat()))

    monkeypatch.setattr(evaluation, "compute_forward_return", fake_forward_return)

    summary = evaluation.summarize_ic(log_path, forward_days=5)

    # 2026-01-05: (0.5, 0.03), (-0.2, -0.01) → 완전 양의 순위상관 → IC=1.0
    # 2026-01-06: 005930은 forward_return 없음 → 표본 1개뿐 → IC 계산 불가(제외)
    assert summary["days_measured"] == 1
    assert summary["mean_ic"] == pytest.approx(1.0)
    assert summary["skipped_no_forward_data"] == 1
