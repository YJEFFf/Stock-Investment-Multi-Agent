"""매매일지 매수 진입가를 브로커 체결 원본과 대조·교정하는 도구.

핵심은 "고친다"가 아니라 **귀속할 수 없으면 안 고친다**이다 — 일별 체결 집계는
같은 날 같은 종목의 여러 주문을 합산하므로, 이 행 하나의 값이라고 말할 수 없는
경우가 있다. 그때 집계 평균을 그냥 박으면 틀린 값이 "브로커 원본"이라는 라벨을
달고 들어간다(2026-08-15 fill_allocated 건이 정확히 그 부류의 사후 배분이었다).
"""

import json
from datetime import date

import pytest

from scripts import reconcile_trade_journal as rtj


@pytest.fixture(autouse=True)
def _no_real_broker(monkeypatch):
    monkeypatch.setattr(rtj.kis, "fetch_daily_fill_totals", lambda ticker, day, side: None)


def _totals(quantity, amount, fee=0.0):
    return (quantity, amount, fee)


def test_broker_average_divides_amount_by_quantity(monkeypatch):
    # 2026-08-12 009240 실측: 189주 / 8,015,150원 -> 42,408.2원
    monkeypatch.setattr(
        rtj.kis, "fetch_daily_fill_totals", lambda t, d, s: _totals(189, 8_015_150.0)
    )

    price, reason = rtj._broker_average("009240", date(2026, 8, 12), 189)

    assert price == pytest.approx(42_408.2)
    assert reason is None


def test_broker_average_refuses_when_quantity_does_not_match(monkeypatch):
    """수량이 다르면 집계가 이 주문 말고 다른 것도 포함한다는 뜻이다 — 평균을
    이 행에 귀속시키면 안 된다."""
    monkeypatch.setattr(
        rtj.kis, "fetch_daily_fill_totals", lambda t, d, s: _totals(200, 8_400_000.0)
    )

    price, reason = rtj._broker_average("009240", date(2026, 8, 12), 189)

    assert price is None
    assert "수량 불일치" in reason


def test_broker_average_refuses_when_lookup_fails(monkeypatch):
    price, reason = rtj._broker_average("009240", date(2026, 8, 12), 189)
    assert price is None
    assert reason == "브로커 체결 조회 실패"


def test_broker_average_refuses_on_empty_fill(monkeypatch):
    monkeypatch.setattr(rtj.kis, "fetch_daily_fill_totals", lambda t, d, s: _totals(0, 0.0))
    price, reason = rtj._broker_average("009240", date(2026, 8, 12), 189)
    assert price is None
    assert reason == "브로커 체결 내역 없음"


def _run(monkeypatch, tmp_path, rows, broker, argv_apply):
    path = tmp_path / "trade_journal.jsonl"
    path.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows))
    monkeypatch.setattr(rtj, "DEFAULT_TRADE_JOURNAL_LOG_PATH", path)
    monkeypatch.setattr(rtj, "APPLY", argv_apply)
    monkeypatch.setattr(rtj.kis, "fetch_daily_fill_totals", broker)
    assert rtj.main() == 0
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def _buy_row(**overrides):
    row = {
        "event": "buy",
        "day": "2026-08-12",
        "ticker": "192820",
        "quantity": 38,
        "entry_price": 210_000.0,
    }
    row.update(overrides)
    return row


def test_apply_rewrites_price_and_leaves_a_correction_note(monkeypatch, tmp_path):
    # 2026-08-12 192820 실측: 38주 / 8,816,000원 -> 232,000원 (일지엔 호가 210,000)
    rows = _run(
        monkeypatch,
        tmp_path,
        [_buy_row()],
        lambda t, d, s: _totals(38, 8_816_000.0),
        argv_apply=True,
    )

    assert rows[0]["entry_price"] == pytest.approx(232_000.0)
    assert rows[0]["entry_price_source"] == "reconciled"
    # 값만 바뀌면 나중에 원본과 대조할 수 없다 — 교정 전 값이 본문에 남아야 한다.
    assert "210,000.00원 → 232,000.00원" in rows[0]["correction_note"]
    assert rows[0]["quantity"] == 38  # 수량은 안 건드린다


def test_dry_run_changes_nothing(monkeypatch, tmp_path):
    rows = _run(
        monkeypatch,
        tmp_path,
        [_buy_row()],
        lambda t, d, s: _totals(38, 8_816_000.0),
        argv_apply=False,
    )

    assert rows[0]["entry_price"] == pytest.approx(210_000.0)
    assert "entry_price_source" not in rows[0]
    assert "correction_note" not in rows[0]


def test_apply_creates_a_backup_before_rewriting(monkeypatch, tmp_path):
    path = tmp_path / "trade_journal.jsonl"
    _run(monkeypatch, tmp_path, [_buy_row()], lambda t, d, s: _totals(38, 8_816_000.0), True)

    backups = list(path.parent.glob("trade_journal.jsonl.bak-*-entryfix"))
    assert len(backups) == 1
    original = json.loads(backups[0].read_text().strip())
    assert original["entry_price"] == pytest.approx(210_000.0)


def test_same_ticker_twice_in_one_day_is_skipped_not_guessed(monkeypatch, tmp_path):
    """추가매수가 있으면 일별 집계가 두 건을 합산한다 — 나눌 수 없으므로 안 고친다."""
    rows = _run(
        monkeypatch,
        tmp_path,
        [_buy_row(entry_price=210_000.0), _buy_row(entry_price=220_000.0)],
        lambda t, d, s: _totals(76, 17_000_000.0),
        argv_apply=True,
    )

    assert [r["entry_price"] for r in rows] == [210_000.0, 220_000.0]
    assert all("correction_note" not in r for r in rows)


def test_matching_row_is_left_completely_untouched(monkeypatch, tmp_path):
    """이미 맞는 행에는 라벨도 주석도 안 붙인다 — 정상 건마다 주석이 붙으면
    진짜 교정 건이 묻힌다(_entry_source_note와 같은 원칙)."""
    rows = _run(
        monkeypatch,
        tmp_path,
        [_buy_row(ticker="036570", quantity=31, entry_price=255_500.0)],
        lambda t, d, s: _totals(31, 7_920_500.0),
        argv_apply=True,
    )

    assert rows[0] == _buy_row(ticker="036570", quantity=31, entry_price=255_500.0)


def test_sell_rows_are_never_touched(monkeypatch, tmp_path):
    """매도 행은 이 도구의 대상이 아니다 — 이미 체결 원본으로 기록된다."""
    sell = {"event": "sell", "day": "2026-08-12", "ticker": "192820", "exit_price": 251_898.14}
    rows = _run(monkeypatch, tmp_path, [sell], lambda t, d, s: _totals(38, 8_816_000.0), True)
    assert rows[0] == sell
