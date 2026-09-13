"""src/evaluation.py — 마일스톤 3 IC 측정.

측정 관례는 모듈 docstring에 고정돼 있다: 판단일 D 시가 진입 → D+k 거래일 종가,
같은 창의 코스피200을 뺀 초과수익. 이 테스트들은 그 관례가 흔들리지 않게 숫자로
박아둔다 — 첫 1개월 평가에서 관례 세 개가 섞여 결과가 1.2%p 움직였다.
"""

import json
from datetime import date, timedelta

import pytest

from src import evaluation
from src.schemas import OHLCVBar


def _series(start: date, rows: list[tuple[float, float]]) -> dict[date, OHLCVBar]:
    """(open, close) 행을 주말을 건너뛴 연속 거래일로 만든다."""
    out: dict[date, OHLCVBar] = {}
    d = start
    for o, c in rows:
        while d.weekday() >= 5:
            d += timedelta(days=1)
        out[d] = OHLCVBar(date=d, open=o, high=max(o, c), low=min(o, c), close=c, volume=1000)
        d += timedelta(days=1)
    return out


MON = date(2026, 9, 7)


# --- information_coefficient ---


def test_information_coefficient_perfect_positive_correlation():
    pairs = [(1.0, 10.0), (2.0, 20.0), (3.0, 30.0), (4.0, 40.0)]
    assert evaluation.information_coefficient(pairs) == pytest.approx(1.0)


def test_information_coefficient_perfect_negative_correlation():
    pairs = [(1.0, 40.0), (2.0, 30.0), (3.0, 20.0), (4.0, 10.0)]
    assert evaluation.information_coefficient(pairs) == pytest.approx(-1.0)


def test_information_coefficient_handles_ties():
    pairs = [(1.0, 5.0), (1.0, 5.0), (2.0, 10.0), (3.0, 15.0)]
    assert evaluation.information_coefficient(pairs) == pytest.approx(1.0)


def test_information_coefficient_returns_none_for_too_few_samples():
    assert evaluation.information_coefficient([]) is None
    assert evaluation.information_coefficient([(1.0, 2.0)]) is None


def test_information_coefficient_returns_none_when_predicted_has_no_variance():
    assert evaluation.information_coefficient([(1.0, 5.0), (1.0, 10.0), (1.0, 15.0)]) is None


# --- load_decision_entries ---


def test_duplicate_day_ticker_keeps_only_the_last_entry(tmp_path):
    """2026-08-12는 UTC 날짜 버그로 유니버스가 두 번 돌아 37건이 겹쳤다. 옛 summarize_ic는
    raw line을 읽어 같은 종목을 그날 IC에 두 번 넣었다."""
    log = tmp_path / "pipeline.jsonl"
    rows = [
        {"day": "2026-08-12", "ticker": "298050", "avg_score": 0.35, "action": "BUY"},
        {"day": "2026-08-12", "ticker": "298050", "avg_score": 0.31, "action": "HOLD"},
        {"day": "2026-08-12", "ticker": "282330", "avg_score": 0.40},
        {"day": "2026-08-13", "ticker": "282330", "avg_score": None},  # 점수 없음 — 제외
    ]
    log.write_text("\n".join(json.dumps(r) for r in rows) + "\n")

    entries = evaluation.load_decision_entries(log)

    assert [(e["day"], e["ticker"], e["avg_score"]) for e in entries] == [
        ("2026-08-12", "282330", 0.40),
        ("2026-08-12", "298050", 0.31),
    ]


# --- forward_excess_return: 관례 고정 ---


def test_entry_is_the_decision_days_open_and_exit_is_k_days_later_close():
    # D 시가 100 → D+2 종가 110 (+10%), 지수는 D 시가 1000 → D+2 종가 1020 (+2%) → 초과 +8%
    stock = _series(MON, [(100, 105), (106, 108), (109, 110)])
    index = _series(MON, [(1000, 1005), (1006, 1010), (1012, 1020)])

    assert evaluation.forward_excess_return(stock, index, MON, 2) == pytest.approx(0.10 - 0.02)


def test_not_the_previous_close_not_the_same_days_close():
    """전일 종가 진입이면 갭을 공짜로 먹고, 당일 종가 진입이면 첫날 움직임을 버린다.
    판단은 08:30, 집행은 09:01이다 — 시가가 실제로 살 수 있었던 가격이다."""
    stock = _series(MON, [(120, 130), (131, 132)])  # 전일 종가(가상) 100이었다고 해도 무관
    index = _series(MON, [(1000, 1000), (1000, 1000)])

    assert evaluation.forward_excess_return(stock, index, MON, 1) == pytest.approx(132 / 120 - 1)


def test_forward_return_is_none_until_k_trading_days_have_passed():
    stock = _series(MON, [(100, 100), (100, 101)])
    index = _series(MON, [(1000, 1000), (1000, 1000)])

    assert evaluation.forward_excess_return(stock, index, MON, 5) is None


def test_forward_return_is_none_when_the_index_is_missing_that_window():
    """지수 없이 절대수익으로 폴백하지 않는다 — 초과수익과 절대수익이 같은 모양으로 섞인다."""
    stock = _series(MON, [(100, 100), (100, 101), (101, 102)])
    index = _series(MON + timedelta(days=1), [(1000, 1000), (1000, 1000)])

    assert evaluation.forward_excess_return(stock, index, MON, 2) is None


def test_a_halted_decision_day_enters_on_the_next_trading_day():
    stock = _series(MON + timedelta(days=1), [(200, 210), (211, 220)])
    index = _series(MON, [(1000, 1000), (1000, 1000), (1000, 1000)])

    assert evaluation.forward_excess_return(stock, index, MON, 1) == pytest.approx(220 / 200 - 1)


# --- trailing_return ---


def test_trailing_return_excludes_the_decision_day_itself():
    """판단은 08:30에 전일 데이터로 내려진다 — D 당일 가격은 판단 시점에 없었다."""
    closes = [(100, 100 + i) for i in range(22)]  # 종가 100..121
    stock = _series(MON, closes)
    d = sorted(stock)[21]  # 22번째 거래일에 판단
    # 직전 거래일(21번째) 종가 120, 그보다 20거래일 전(1번째) 종가 100
    assert evaluation.trailing_return(stock, d, 20) == pytest.approx(120 / 100 - 1)


def test_trailing_return_is_none_without_enough_history():
    stock = _series(MON, [(100, 100)] * 5)
    assert evaluation.trailing_return(stock, sorted(stock)[4], 20) is None


# --- summarize_ic ---


def _flat_index(start: date, n: int) -> dict[date, OHLCVBar]:
    return _series(start, [(1000, 1000)] * n)


def test_summarize_ic_is_the_mean_of_daily_cross_sectional_ics():
    start = MON
    index = _flat_index(start, 10)
    days = sorted(index)
    # 이틀, 하루에 세 종목. 첫날은 점수 순서 = 수익 순서(IC +1), 둘째 날은 반대(IC −1)
    prices = {
        "A": _series(start, [(100, 100), (100, 103), (100, 100), (100, 97)] + [(100, 100)] * 6),
        "B": _series(start, [(100, 100), (100, 102), (100, 100), (100, 98)] + [(100, 100)] * 6),
        "C": _series(start, [(100, 100), (100, 101), (100, 100), (100, 99)] + [(100, 100)] * 6),
    }
    entries = [
        {"day": days[0].isoformat(), "ticker": "A", "avg_score": 0.9},
        {"day": days[0].isoformat(), "ticker": "B", "avg_score": 0.5},
        {"day": days[0].isoformat(), "ticker": "C", "avg_score": 0.1},
        {"day": days[2].isoformat(), "ticker": "A", "avg_score": 0.9},
        {"day": days[2].isoformat(), "ticker": "B", "avg_score": 0.5},
        {"day": days[2].isoformat(), "ticker": "C", "avg_score": 0.1},
    ]

    s = evaluation.summarize_ic(entries, prices, index, k=1)

    assert s["days_measured"] == 2
    assert s["mean_ic"] == pytest.approx(0.0)
    assert s["positive_days"] == 1
    assert s["n_pairs"] == 6
    assert s["convention"] == evaluation.ENTRY_CONVENTION


def test_summarize_ic_reports_what_it_could_not_measure():
    """측정 못 한 건수를 숫자로 남긴다 — IC가 10일치인데 판단이 50일치면 읽는 사람이 알아야 한다."""
    index = _flat_index(MON, 3)
    entries = [
        {"day": MON.isoformat(), "ticker": "A", "avg_score": 0.5},
        {"day": MON.isoformat(), "ticker": "NOPRICE", "avg_score": 0.5},
    ]
    prices = {"A": _series(MON, [(100, 100)] * 3)}

    s = evaluation.summarize_ic(entries, prices, index, k=5)

    assert s["mean_ic"] is None  # 0.0으로 채우지 않는다
    assert s["skipped_no_forward_data"] == 1
    assert s["skipped_no_price"] == 1


def test_summarize_ic_exposes_whether_the_score_is_just_momentum():
    """첫 1개월에 avg_score는 직전 20일 수익률과 ρ=+0.63이었다. 이 숫자를 IC 옆에 매일
    같이 내야 신호가 모멘텀 너머의 무언가를 재는지 알 수 있다."""
    n_days = 30
    index = _flat_index(MON, n_days)
    d = sorted(index)[22]
    prices, entries = {}, []
    for i, tkr in enumerate("ABCDEFGHIJ"):
        drift = 1 + 0.002 * i  # i가 클수록 과거 20일 수익률이 높다
        rows, px = [], 100.0
        for _ in range(n_days):
            rows.append((px, px * drift))
            px *= drift
        prices[tkr] = _series(MON, rows)
        entries.append({"day": d.isoformat(), "ticker": tkr, "avg_score": 0.1 * i})  # 점수 = 모멘텀 순서

    s = evaluation.summarize_ic(entries, prices, index, k=1)

    assert s["rho_score_momentum_20d"] == pytest.approx(1.0)
    assert len(s["momentum_quintile_ic"]) == evaluation.QUINTILES


# --- PriceHistory ---


def test_price_history_accumulates_beyond_the_brokers_100_day_window(tmp_path):
    """KIS 일봉은 최근 100거래일만 준다. 매일 합쳐두지 않으면 몇 달 뒤엔 옛 판단의
    선행수익률을 못 구한다."""
    history = evaluation.PriceHistory(tmp_path)
    old = list(_series(MON, [(100, 100)] * 3).values())
    new = list(_series(MON + timedelta(days=2), [(200, 200)] * 3).values())

    assert history.merge("A", old) == 3
    added = history.merge("A", new)

    bars = history.load("A")
    assert added == 2  # 겹친 하루는 새로 센 게 아니다
    assert len(bars) == 5
    assert bars[sorted(bars)[2]].close == 200  # 같은 날짜는 새 값으로 덮는다


def test_price_history_refresh_skips_failed_fetches(tmp_path):
    history = evaluation.PriceHistory(tmp_path)
    bars = list(_series(MON, [(100, 100)] * 2).values())

    added = history.refresh(["OK", "FAIL"], lambda key: bars if key == "OK" else None)

    assert added == {"OK": 2}
    assert history.load("FAIL") == {}


def test_score_momentum_correlation_includes_decisions_still_waiting_for_returns():
    """최근 판단은 선행수익률이 아직 없다. 그걸 빼고 ρ를 재면 표본의 최근 1/5이 늘 빠져
    첫 평가 값(+0.627, n=1,018)보다 낮게(+0.568, n=803) 나왔다."""
    n_days = 30
    index = _flat_index(MON, n_days)
    last = sorted(index)[-1]  # 마지막 날 판단 — k=5 선행수익률은 아직 없다
    prices, entries = {}, []
    for i, tkr in enumerate("ABCDE"):
        drift = 1 + 0.002 * i
        rows, px = [], 100.0
        for _ in range(n_days):
            rows.append((px, px * drift))
            px *= drift
        prices[tkr] = _series(MON, rows)
        entries.append({"day": last.isoformat(), "ticker": tkr, "avg_score": 0.1 * i})

    s = evaluation.summarize_ic(entries, prices, index, k=5)

    assert s["mean_ic"] is None and s["skipped_no_forward_data"] == 5
    assert s["rho_score_momentum_20d"] == pytest.approx(1.0)


def test_the_pre_registered_segment_starts_on_the_prompt_change_day():
    entries = [{"day": "2026-09-11", "ticker": "A"}, {"day": "2026-09-14", "ticker": "B"}]
    since = evaluation.SEGMENTS["post_chart_prompt_7a9654"]

    assert [e["ticker"] for e in evaluation.entries_since(entries, since)] == ["B"]
    assert evaluation.entries_since(entries, evaluation.SEGMENTS["all"]) == entries
