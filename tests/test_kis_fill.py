"""kis.fill_after_order — 주문 수량이 다 체결될 때까지 기다렸다 재는 부분.

이 파일이 있는 이유는 2026-08-18 실측 사고다. 036570 손절 31주가 여러 번에 나뉘어
체결되는 사이에 스냅샷이 찍혀 19주/4,364,500원만 잡혔고, 매매일지에서 12주·
2,756,500원이 통째로 빠졌다. 같은 날 4주짜리 익절은 한 번에 체결돼 멀쩡했다 —
작은 주문만 보고 있으면 안 드러나는 종류의 버그라 테스트로 박아둔다.
"""

from datetime import date

import pytest

from src import kis

DAY = date(2026, 8, 18)


def _totals_sequence(monkeypatch, values):
    """누적 체결 집계가 호출될 때마다 순서대로 나오게 하고, 마지막 값은 계속 유지한다.

    브로커는 조회를 더 한다고 값을 바꾸지 않으므로 마지막 값이 반복되는 게 실제에 가깝다.
    """
    calls = {"n": 0}

    def fake(ticker, day, side):
        i = min(calls["n"], len(values) - 1)
        calls["n"] += 1
        return values[i]

    monkeypatch.setattr(kis, "fetch_daily_fill_totals", fake)
    return calls


def test_returns_immediately_when_the_first_look_already_covers_the_order(monkeypatch):
    calls = _totals_sequence(monkeypatch, [(4, 928_000.0)])

    fill = kis.fill_after_order("192820", DAY, "sell", (0, 0.0), 4, timeout_s=5.0, poll_interval_s=0.0)

    assert fill is not None
    assert (fill.quantity, fill.amount) == (4, 928_000.0)
    assert fill.complete is True
    assert calls["n"] == 1  # 다 잡혔으면 더 안 기다린다


def test_waits_for_the_rest_of_a_partially_filled_order(monkeypatch):
    """036570 재현: 첫 조회에 19주만 잡히고, 기다리면 31주가 다 잡힌다."""
    _totals_sequence(monkeypatch, [(19, 4_364_500.0), (31, 7_121_000.0)])

    fill = kis.fill_after_order("036570", DAY, "sell", (0, 0.0), 31, timeout_s=5.0, poll_interval_s=0.0)

    assert fill is not None
    assert (fill.quantity, fill.amount) == (31, 7_121_000.0)
    assert fill.complete is True
    assert fill.price == pytest.approx(229_709.68, abs=0.01)


def test_marks_the_fill_incomplete_when_the_order_never_fully_lands(monkeypatch):
    """타임아웃까지 못 채우면 잡힌 만큼을 주되 complete=False로 표시한다 —
    체결량을 지어내지 않으면서 "덜 잡혔다"는 사실은 남긴다."""
    _totals_sequence(monkeypatch, [(19, 4_364_500.0)])

    fill = kis.fill_after_order("036570", DAY, "sell", (0, 0.0), 31, timeout_s=0.0, poll_interval_s=0.0)

    assert fill is not None
    assert fill.quantity == 19
    assert fill.complete is False


def test_subtracts_the_before_snapshot_so_same_day_repeat_sells_do_not_mix(monkeypatch):
    """같은 종목을 같은 날 두 번 매도해도 이번 주문분만 잡혀야 한다."""
    _totals_sequence(monkeypatch, [(20, 4_946_000.0)])

    fill = kis.fill_after_order("192820", DAY, "sell", (12, 3_024_000.0), 8, timeout_s=5.0, poll_interval_s=0.0)

    assert fill is not None
    assert (fill.quantity, fill.amount) == (8, 1_922_000.0)
    assert fill.complete is True


def test_gives_up_when_the_before_snapshot_is_missing(monkeypatch):
    """직전 집계를 못 구했으면 차를 낼 수 없다 — 체결을 지어내느니 None이다."""
    _totals_sequence(monkeypatch, [(31, 7_121_000.0)])

    assert kis.fill_after_order("036570", DAY, "sell", None, 31, timeout_s=5.0, poll_interval_s=0.0) is None


def test_returns_none_when_nothing_filled_at_all(monkeypatch):
    """수량이 안 늘었으면 '체결 안 됨'과 '실주문을 안 냄'을 같게 다룬다."""
    _totals_sequence(monkeypatch, [(0, 0.0)])

    assert kis.fill_after_order("036570", DAY, "sell", (0, 0.0), 31, timeout_s=0.0, poll_interval_s=0.0) is None
