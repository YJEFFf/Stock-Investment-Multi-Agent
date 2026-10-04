"""2026-10-05 감사에서 확인한 네 체결 묶음만 교정하는 일회성 도구.

기본은 보관한 감사 증거로 계획을 검증하는 dry-run. --apply는 모의 브로커를 다시
조회하고, 운영 파일이 감사 원본과 같을 때 백업·락 아래 교정한다. 주문 호출 없음.
--sync-notion은 교정한 일지만 갱신하고 합쳐진 매도 두 페이지를 archive한다.
"""

import argparse
import asyncio
import copy
import hashlib
import json
import os
import shutil
import sys
from collections import defaultdict
from datetime import date, datetime, time
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src import kis, notion_sync  # noqa: E402
from src.market_calendar import is_krx_trading_day  # noqa: E402
from src.portfolio_store import portfolio_lock  # noqa: E402
from src.schemas import PortfolioState  # noqa: E402

FILES = ("trade_journal.jsonl", "sell.jsonl", "portfolio_state.json")
CORRECTION = "2026-10-05-broker-reconciliation"
TARGETS = {("2026-09-16", "004020", "buy"), ("2026-09-22", "007660", "sell"),
           ("2026-09-22", "282330", "sell"), ("2026-09-29", "051900", "sell"),
           ("2026-09-29", "004020", "sell"), ("2026-09-28", "007660", "sell")}
KST = ZoneInfo("Asia/Seoul")


def read_rows(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def _sell_values(row: dict, totals: list, entry_fee: float) -> None:
    quantity, amount, fee = totals
    basis = row["entry_price"] * quantity
    tax = round(amount * kis.SELL_TAX_RATE, 2)
    net = amount - basis - fee - tax - entry_fee
    row.update(shares_sold=quantity, exit_price=amount / quantity, exit_price_source="fill",
               sell_amount=amount, sell_amount_source="fill", fee_amount=fee, fee_source="fill",
               tax_amount=tax, tax_source="estimated_rate", entry_fee_amount=round(entry_fee, 2),
               entry_fee_source="broker_buy_fee_pro_rata", net_pnl_amount=round(net, 2),
               net_pnl_pct=net / basis, realized_pnl_pct=amount / basis - 1,
               correction=CORRECTION)


def build_plan(audit_dir: Path) -> tuple[dict[str, bytes], dict]:
    evidence = json.loads((audit_dir / "broker_fills.json").read_text())
    snapshot = json.loads((audit_dir / "broker_snapshot.json").read_text())
    if evidence["mode"] != "paper" or snapshot["mode"] != "paper":
        raise ValueError("paper evidence required")
    fills = {(g["day"], g["ticker"], g["side"]): g["totals"] for g in evidence["groups"]}
    rows = copy.deepcopy(read_rows(audit_dir / "logs" / FILES[0]))
    before_count = len(rows)
    isu = [r for r in rows if (r["day"], r["ticker"], r["event"]) == ("2026-09-22", "007660", "sell")]
    if len(isu) != 3 or sorted(r["shares_sold"] for r in isu) != [0, 0, 2]:
        raise ValueError("unexpected Isu source rows")
    if fills[("2026-09-22", "007660", "sell")] != [4, 448800.0, 60.0]:
        raise ValueError("unexpected audited Isu fill")
    aggregate = copy.deepcopy(isu[0])
    aggregate.update(aggregation="broker_daily", shares_before=7, shares_after=3,
                     sell_fraction=4 / 7, position_fraction_sold=4 / 7, position_fraction_remaining=3 / 7,
                     portfolio_weight_before=0.08 * 7 / 76, portfolio_weight_sold=0.08 * 4 / 76,
                     portfolio_weight_after=0.08 * 3 / 76, decision_price=None,
                     peak_price=None, take_profit_stage=None,
                     correction_note="브로커 일별 원장으로 4주·448,800원 매도를 확인해 합산 교정. "
                                     "응답 유실 주문 중 어느 주문이 체결됐는지와 정확한 체결 시각은 확인 불가. "
                                     "기존 0주 매도 2행과 2주 매도 1행의 원본은 감사 백업에 보존.")
    inserted = False
    rebuilt = []
    for row in rows:
        if row in isu:
            if not inserted:
                rebuilt.append(aggregate)
                inserted = True
        else:
            rebuilt.append(row)
    rows = rebuilt
    buy_groups = {g["ticker"]: g["totals"] for g in evidence["groups"] if g["side"] == "buy"}
    for row in rows:
        key = row["day"], row["ticker"], row["event"]
        if key not in TARGETS:
            continue
        totals = fills[key]
        if row["event"] == "buy":
            row.update(entry_price=totals[1] / totals[0], fee_amount=totals[2],
                       entry_price_source="fill", correction=CORRECTION)
            continue
        buy = buy_groups[row["ticker"]]
        if row["ticker"] == "004020":
            row["entry_price"] = buy[1] / buy[0]
        _sell_values(row, totals, buy[2] * totals[0] / buy[0])
        if key == ("2026-09-28", "007660", "sell"):
            row.update(shares_before=3, shares_after=2,
                       position_fraction_sold=1 / 3, position_fraction_remaining=2 / 3,
                       portfolio_weight_before=0.08 * 3 / 76, portfolio_weight_after=0.08 * 2 / 76,
                       portfolio_weight_sold=0.08 / 76)

    # 일자·종목·매수/매도별 38개 집계를 전부 대조한다. 다른 부분의 불일치도 막는다.
    sums = defaultdict(lambda: [0, 0.0])
    remaining = defaultdict(int)
    for row in rows:
        if row["event"] not in ("buy", "sell"):
            continue
        is_buy = row["event"] == "buy"
        quantity = row["quantity"] if is_buy else row["shares_sold"]
        amount = quantity * row["entry_price"] if is_buy else row["sell_amount"]
        key = row["day"], row["ticker"], row["event"]
        sums[key][0] += quantity
        sums[key][1] += amount
        remaining[row["ticker"]] += quantity if is_buy else -quantity
    if set(sums) != set(fills):
        raise ValueError("journal and broker group sets differ")
    for key, (quantity, amount) in sums.items():
        if quantity != fills[key][0] or abs(amount - fills[key][1]) > 0.01:
            raise ValueError(f"unreconciled group: {key}")
    holdings = {h["pdno"]: int(h["hldg_qty"]) for h in snapshot["holdings"]}
    if {t: q for t, q in remaining.items() if q} != holdings:
        raise ValueError("reconstructed holdings differ from broker")

    state = PortfolioState.model_validate_json((audit_dir / "logs" / FILES[2]).read_text())
    position = next(p for p in state.positions if p.ticker == "007660")
    if position.quantity != 4 or holdings["007660"] != 2:
        raise ValueError("unexpected state quantity")
    old_weight = position.weight
    position.quantity = 2
    position.weight = old_weight / 2
    state.cash_weight += old_weight - position.weight
    if {p.ticker: p.quantity for p in state.positions} != holdings:
        raise ValueError("corrected portfolio differs from broker")

    sales = read_rows(audit_dir / "logs" / FILES[1])
    corrected_sales = []
    aggregate_written = False
    for row in sales:
        key = row["day"], row["ticker"], "sell"
        if key in TARGETS:
            if key == ("2026-09-22", "007660", "sell"):
                if aggregate_written:
                    continue
                aggregate_written = True
                row.update(aggregation="broker_daily", sell_fraction=4 / 7,
                           correction_note=aggregate["correction_note"])
            row.update(price=fills[key][1] / fills[key][0], correction=CORRECTION)
        corrected_sales.append(row)
    encode = lambda rs: ("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rs)).encode()
    files = {FILES[0]: encode(rows), FILES[1]: encode(corrected_sales),
             FILES[2]: state.model_dump_json(indent=2).encode()}
    return files, {"correction": CORRECTION, "journal_rows_before": before_count,
                   "journal_rows_after": len(rows), "broker_groups_matched": len(fills),
                   "holdings_matched": len(holdings), "isu_quantity": [4, 2],
                   "cash_weight": state.cash_weight,
                   "output_sha256": {n: hashlib.sha256(b).hexdigest() for n, b in files.items()}}


def atomic_write(path: Path, data: bytes) -> None:
    tmp = path.with_suffix(path.suffix + ".repair.tmp")
    with tmp.open("wb") as f:
        f.write(data)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


async def sync_notion(state_dir: Path, backup: Path) -> dict:
    database_id = os.environ["NOTION_TRADE_JOURNAL_DB_ID"]
    path = state_dir / "notion_sync_state.json"
    if not (backup / path.name).exists():
        shutil.copy2(path, backup / path.name)
    state = notion_sync._load_sync_state(path)
    for suffix in (":1", ":2"):
        key = "sell:007660:2026-09-22" + suffix
        record = state.get(key)
        if record:
            page_id = record.get("page_id")
            if not page_id or notion_sync._notion_request("PATCH", f"/pages/{page_id}", {"archived": True}) is None:
                raise RuntimeError(f"could not archive duplicate: {key}")
            del state[key]
            notion_sync._save_sync_state(path, state)
    result = await notion_sync.sync_trade_journal(state_dir / "trade_journal.jsonl", database_id, path)
    if result["failed"]:
        raise RuntimeError(f"Notion corrections incomplete: {result}")
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--audit-dir", type=Path, required=True)
    parser.add_argument("--state-dir", type=Path, default=Path("logs"))
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--sync-notion", action="store_true")
    args = parser.parse_args()
    files, report = build_plan(args.audit_dir)
    if not args.apply:
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return
    now = datetime.now(KST)
    if is_krx_trading_day(now.date()) and time(8, 25) <= now.time() < time(16, 10):
        raise RuntimeError("no operational repair during cron trading window")
    if "openapivts." not in kis.BASE_URL:
        raise RuntimeError("paper broker required")
    evidence = json.loads((args.audit_dir / "broker_fills.json").read_text())
    for g in evidence["groups"]:
        if (g["day"], g["ticker"], g["side"]) in TARGETS:
            actual = kis.fetch_daily_fill_totals(g["ticker"], date.fromisoformat(g["day"]), g["side"])
            if actual is None or list(actual) != g["totals"]:
                raise RuntimeError(f"fresh broker evidence differs: {g['day']} {g['ticker']}")
    snapshot = json.loads((args.audit_dir / "broker_snapshot.json").read_text())
    expected_holdings = {h["pdno"]: int(h["hldg_qty"]) for h in snapshot["holdings"]}
    holdings = kis.fetch_holdings()
    if holdings is None or {t: h[0] for t, h in holdings.items()} != expected_holdings:
        raise RuntimeError("fresh broker holdings differ")
    with portfolio_lock(args.state_dir / "portfolio_state.lock"):
        originals = {n: (args.state_dir / n).read_bytes() for n in FILES}
        audited = {n: (args.audit_dir / "logs" / n).read_bytes() for n in FILES}
        if originals != audited:
            # 강제 종료로 일부만 바뀌어도 사전 manifest와 원본/교정본 일치를 확인하고 재개한다.
            if any(originals[n] not in (audited[n], files[n]) for n in FILES):
                raise RuntimeError("operational files differ from both audited and corrected versions")
            manifests = sorted((args.state_dir / "repairs").glob(CORRECTION + "-*/manifest.json"))
            matching = [p for p in manifests if json.loads(p.read_text()).get("output_sha256") == report["output_sha256"]]
            if not matching:
                raise RuntimeError("repair backup manifest missing")
            backup = matching[-1].parent
            if any((backup / n).read_bytes() != audited[n] for n in FILES):
                raise RuntimeError("repair backup does not match audited originals")
            report["resumed_at"] = now.isoformat()
        else:
            backup = args.state_dir / "repairs" / (CORRECTION + "-" + now.strftime("%H%M%S%f"))
            backup.mkdir(parents=True, exist_ok=False)
            for n, b in originals.items():
                atomic_write(backup / n, b)
            manifest = {**report, "source_sha256": {n: hashlib.sha256(b).hexdigest() for n, b in audited.items()}}
            atomic_write(backup / "manifest.json", json.dumps(manifest, ensure_ascii=False, indent=2).encode())
        try:
            for n, b in files.items():
                atomic_write(args.state_dir / n, b)
            if any((args.state_dir / n).read_bytes() != b for n, b in files.items()):
                raise RuntimeError("post-write verification failed")
        except BaseException:
            for n, b in audited.items():
                atomic_write(args.state_dir / n, b)
            raise
        report["applied_at"] = now.isoformat()
        report["backup"] = str(backup)
        atomic_write(backup / "result.json", json.dumps(report, ensure_ascii=False, indent=2).encode())
    if args.sync_notion:
        report["notion"] = asyncio.run(sync_notion(args.state_dir, backup))
        (backup / "result.json").write_text(json.dumps(report, ensure_ascii=False, indent=2))
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
