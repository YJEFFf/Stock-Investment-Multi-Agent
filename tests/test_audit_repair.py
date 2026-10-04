import json
import shutil

import pytest

from scripts.repair_20261005 import build_plan
from scripts import repair_20261005 as repair
from src.schemas import ExitPlan, PortfolioState, Position


def _audit(tmp_path):
    root = tmp_path / "audit"
    (root / "logs").mkdir(parents=True)
    rows, groups = [], []
    def buy(day, ticker, q, amount, fee):
        groups.append(dict(day=day, ticker=ticker, side="buy", totals=[q, amount, fee]))
        rows.append(dict(event="buy", day=day, ticker=ticker, quantity=q, entry_price=amount / q))
    def sell(day, ticker, q, amount, fee, entry):
        groups.append(dict(day=day, ticker=ticker, side="sell", totals=[q, amount, fee]))
        rows.append(dict(event="sell", day=day, ticker=ticker, shares_sold=q, sell_amount=amount,
                         entry_price=entry, reason="stop_loss", exit_plan=None))
    buy("2026-08-20", "007660", 76, 7804400., 1100.)
    sell("2026-09-21", "007660", 69, 6900000., 100., 7804400 / 76)
    buy("2026-09-16", "004020", 223, 7787900., 1100.)
    buy("2026-08-12", "282330", 53, 7965900., 1130.)
    buy("2026-08-13", "051900", 25, 7737500., 1090.)
    sell("2026-09-22", "007660", 4, 448800., 60., 7804400 / 76)
    rows[-1].update(shares_sold=2, sell_amount=224600.)
    rows.extend([{**rows[-1], "shares_sold": 0, "sell_amount": None} for _ in range(2)])
    sell("2026-09-28", "007660", 1, 112500., 10., 7804400 / 76)
    sell("2026-09-22", "282330", 53, 7186400., 1020., 150300.)
    sell("2026-09-29", "051900", 25, 6971000., 980., 309500.)
    sell("2026-09-29", "004020", 223, 6615200., 930., 34907.236842)
    plan = ExitPlan(stop_loss_pct=-.07, take_profit_pct=.14, take_profit_fraction=.35, trail_pct=-.06)
    position = Position(ticker="007660", sector="", weight=.08 * 4 / 76, quantity=4,
                        entry_price=7804400 / 76, peak_price=123700., peak_reset_day="2026-09-28",
                        take_profit_stage=8, exit_plan=plan)
    portfolio = PortfolioState(positions=[position], cash_weight=1-position.weight)
    (root / "logs/portfolio_state.json").write_text(portfolio.model_dump_json(indent=2))
    (root / "logs/trade_journal.jsonl").write_text("".join(json.dumps(r) + "\n" for r in rows))
    (root / "logs/sell.jsonl").write_text("".join(json.dumps({"day": r["day"], "ticker": r["ticker"], "price": 1.}) + "\n" for r in rows if r["event"] == "sell"))
    (root / "broker_fills.json").write_text(json.dumps({"mode": "paper", "groups": groups}))
    (root / "broker_snapshot.json").write_text(json.dumps({"mode": "paper", "holdings": [{"pdno": "007660", "hldg_qty": "2"}]}))
    return root, portfolio


def test_audit_repair_matches_every_broker_group_and_preserves_exit_state(tmp_path):
    root, original = _audit(tmp_path)
    files, report = build_plan(root)
    state = PortfolioState.model_validate_json(files["portfolio_state.json"])
    assert state.positions[0].quantity == 2
    assert state.positions[0].weight == pytest.approx(original.positions[0].weight / 2)
    for field in ("exit_plan", "peak_price", "peak_reset_day", "take_profit_stage", "entry_price"):
        assert getattr(state.positions[0], field) == getattr(original.positions[0], field)
    rows = [json.loads(l) for l in files["trade_journal.jsonl"].splitlines()]
    aggregate = next(r for r in rows if r.get("aggregation") == "broker_daily")
    assert aggregate["shares_sold"] == 4 and aggregate["sell_amount"] == 448800
    assert aggregate["decision_price"] is None and aggregate["take_profit_stage"] is None
    assert report["journal_rows_after"] == report["journal_rows_before"] - 2
    assert (root / "logs/portfolio_state.json").read_text() == original.model_dump_json(indent=2)


def test_audit_repair_stops_if_evidence_is_not_the_audited_fill(tmp_path):
    root, _ = _audit(tmp_path)
    path = root / "broker_fills.json"
    evidence = json.loads(path.read_text())
    next(g for g in evidence["groups"] if g["day"] == "2026-09-22" and g["ticker"] == "007660")["totals"][0] = 5
    path.write_text(json.dumps(evidence))
    with pytest.raises(ValueError, match="unexpected audited Isu fill"):
        build_plan(root)


def _mock_live(monkeypatch, root, state, *, notion=False):
    fills = json.loads((root / "broker_fills.json").read_text())["groups"]
    totals = {(g["day"], g["ticker"], g["side"]): tuple(g["totals"]) for g in fills}
    monkeypatch.setattr(repair.kis, "fetch_daily_fill_totals", lambda t, d, s: totals[d.isoformat(), t, s])
    monkeypatch.setattr(repair.kis, "fetch_holdings", lambda: {"007660": (2, 7804400/76)})
    monkeypatch.setattr(repair, "is_krx_trading_day", lambda d: False)
    monkeypatch.setattr(repair.sys, "argv", ["repair", "--audit-dir", str(root), "--state-dir", str(state), "--apply"] + (["--sync-notion"] if notion else []))


def test_notion_failure_can_resume_without_rewriting_original_backup(monkeypatch, tmp_path):
    root, _ = _audit(tmp_path)
    state = tmp_path / "state"
    shutil.copytree(root / "logs", state)
    _mock_live(monkeypatch, root, state, notion=True)
    calls = []
    async def sync(*a):
        calls.append(1)
        if len(calls) == 1:
            raise RuntimeError("temporary Notion failure")
        return {"updated": 6, "failed": 0}
    monkeypatch.setattr(repair, "sync_notion", sync)
    with pytest.raises(RuntimeError, match="temporary Notion"):
        repair.main()
    repair.main()
    assert calls == [1, 1]
    backups = list((state / "repairs").iterdir())
    assert len(backups) == 1
    assert (backups[0] / "portfolio_state.json").read_bytes() == (root / "logs/portfolio_state.json").read_bytes()


def test_partial_file_replacement_resumes_only_with_matching_manifest(monkeypatch, tmp_path):
    root, _ = _audit(tmp_path)
    state = tmp_path / "state"
    shutil.copytree(root / "logs", state)
    files, report = build_plan(root)
    backup = state / "repairs" / (repair.CORRECTION + "-interrupted")
    shutil.copytree(root / "logs", backup)
    (backup / "manifest.json").write_text(json.dumps(report))
    (state / "trade_journal.jsonl").write_bytes(files["trade_journal.jsonl"])
    _mock_live(monkeypatch, root, state)
    repair.main()
    assert all((state / n).read_bytes() == data for n, data in files.items())
