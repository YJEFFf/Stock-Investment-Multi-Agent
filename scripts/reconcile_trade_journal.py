"""매매일지(logs/trade_journal.jsonl)의 매수 진입가를 브로커 체결 원본과 대조·교정한다.

    uv run python scripts/reconcile_trade_journal.py          # 대조만 (드라이런)
    uv run python scripts/reconcile_trade_journal.py --apply  # 브로커 기준으로 교정

`reconcile_portfolio.py`와 짝이다. 저쪽은 **상태 파일**(손절·익절이 실제로 쓰는 값)을,
이쪽은 **매매일지**(사람이 읽고 나중에 성과를 되짚는 기록)를 맞춘다. 둘이 갈라진 채로
있었다는 게 2026-09-01 점검에서 드러났다: 매도 행은 8/15·8/18에 체결 원본으로 소급
교정됐는데 매수 행은 안 됐고, 그래서 같은 파일 안에서 192820의 buy 행(210,000)과
sell 행(232,000)이 서로 안 맞았다. 노션 매매일지도 그 값을 그대로 싣고 있었다.

**매매가 이 값을 쓰지는 않는다.** 손절·익절은 상태 파일의 진입가로 판정하고 그쪽은
이미 브로커와 일치한다. 여기서 고치는 건 기록이지 판단 기준이 아니다.

교정하지 않고 건너뛰는 경우 — 브로커 집계를 이 행 하나에 귀속시킬 수 없을 때:

- 같은 종목을 같은 날 두 번 매수한 행이 있으면(추가매수) 일별 집계가 두 건을 합산해
  버려서 어느 쪽이 얼마였는지 나눌 수 없다.
- 행의 수량과 브로커 당일 체결 수량이 다르면 같은 이유로 귀속이 안 된다.

둘 다 "값을 모른다"이지 "값이 같다"가 아니므로, 지어내지 않고 건너뛴다고 보고한다.
(kis.fetch_daily_fill_totals docstring의 같은 한계다.)
"""

import json
import logging
import sys
from collections import Counter
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src import kis  # noqa: E402
from src.pipeline import DEFAULT_TRADE_JOURNAL_LOG_PATH, _kst_today  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("reconcile_trade_journal")

APPLY = "--apply" in sys.argv

# 브로커와 이만큼 넘게 벌어지면 교정 대상. reconcile.py와 같은 수준 — 부동소수
# 표현 오차만 걸러내고 실제 어긋남은 전부 잡는다(가장 작았던 게 +0.04%).
RELATIVE_TOLERANCE = 1e-6


def _broker_average(ticker: str, day: date, quantity: int) -> tuple[float | None, str | None]:
    """그날 그 종목 매수의 브로커 평균 체결가. 못 구하거나 이 행에 귀속할 수
    없으면 (None, 사유)."""
    totals = kis.fetch_daily_fill_totals(ticker, day, "buy")
    if totals is None:
        return None, "브로커 체결 조회 실패"
    broker_qty, broker_amount = totals[0], totals[1]
    if broker_qty <= 0 or broker_amount <= 0:
        return None, "브로커 체결 내역 없음"
    if broker_qty != quantity:
        return None, f"수량 불일치(일지 {quantity}주 / 브로커 {broker_qty}주) — 이 행에 귀속 불가"
    return broker_amount / broker_qty, None


def main() -> int:
    path = DEFAULT_TRADE_JOURNAL_LOG_PATH
    if not path.exists():
        print(f"{path} 가 없습니다.")
        return 1

    entries = [json.loads(line) for line in path.read_text().splitlines() if line.strip()]

    # 같은 (종목, 날짜)에 매수 행이 둘 이상이면 일별 집계로는 나눌 수 없다.
    buy_groups = Counter((e["ticker"], e["day"]) for e in entries if e.get("event") == "buy")

    corrected = 0
    skipped = 0
    unchanged = 0
    print(f"매수 행 {sum(buy_groups.values())}건을 브로커 체결 원본과 대조합니다.\n")

    for entry in entries:
        if entry.get("event") != "buy":
            continue

        ticker, day_str = entry["ticker"], entry["day"]
        recorded = entry.get("entry_price")
        quantity = entry.get("quantity")
        if not recorded or not quantity:
            print(f"  [-] {day_str} {ticker}: 진입가·수량이 없어 대조 불가")
            skipped += 1
            continue

        if buy_groups[(ticker, day_str)] > 1:
            print(f"  [-] {day_str} {ticker}: 같은 날 매수 행이 2건 이상 — 일별 집계를 나눌 수 없어 건너뜀")
            skipped += 1
            continue

        broker_price, reason = _broker_average(ticker, date.fromisoformat(day_str), quantity)
        if broker_price is None:
            print(f"  [-] {day_str} {ticker}: {reason}")
            skipped += 1
            continue

        if abs(broker_price / recorded - 1) <= RELATIVE_TOLERANCE:
            unchanged += 1
            continue

        diff = broker_price / recorded - 1
        print(f"  {day_str} {ticker} entry_price: {recorded} -> {broker_price} ({diff:+.2%})")
        corrected += 1

        if APPLY:
            was = entry.get("entry_price_source")
            entry["entry_price"] = broker_price
            entry["entry_price_source"] = "reconciled"
            # 값만 조용히 바뀌면 나중에 원본과 대조할 수 없다. 교정 전 값과, 그 값이
            # 애초에 어디서 왔는지를 같이 남긴다.
            origin = f" (교정 전 출처: {was})" if was else " (교정 전에는 출처 필드가 없던 시절의 행)"
            entry["correction_note"] = (
                f"{_kst_today().isoformat()} 교정: {recorded:,.2f}원 → {broker_price:,.2f}원"
                f"{origin}. 브로커 당일 매수 체결 집계(총액÷수량)를 원본으로 삼았다. "
                "손절·익절 판정은 상태 파일 값으로 돌았고 그쪽은 이미 브로커와 일치했다 "
                "— 이 교정은 기록을 맞춘 것이지 판단 기준을 바꾼 게 아니다."
            )

    print(f"\n대조 완료 — 어긋남 {corrected}건 / 일치 {unchanged}건 / 건너뜀 {skipped}건")

    if not corrected:
        return 0
    if not APPLY:
        print("(드라이런입니다. 실제로 교정하려면 --apply)")
        return 0

    backup = path.with_name(f"{path.name}.bak-{_kst_today().strftime('%Y%m%d')}-entryfix")
    backup.write_text(path.read_text())
    path.write_text("".join(json.dumps(e, ensure_ascii=False) + "\n" for e in entries))
    print(f"교정 완료 — {corrected}개 행을 고쳤습니다. 백업: {backup}")
    print("노션은 다음 동기화(09:01 / 15:35)에서 지문이 바뀐 행만 갱신됩니다.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
