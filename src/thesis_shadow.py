"""Frozen entry premises and daily holding reviews, with no order-writing path.

One durable claim before each model call prevents resampling after interruption.
All writes live under STATE_DIR. Production decisions, positions and pending orders
are read-only inputs. Failures here cannot fail the existing buy/sell workflow.
"""

import asyncio
import hashlib
import json
import os
import re
import tempfile
from collections import Counter
from datetime import date, datetime, time
from pathlib import Path
from zoneinfo import ZoneInfo

from src import codex_plan, run_history, thesis_sources
from src.market_calendar import is_krx_trading_day
from src.schemas import (
    Decision, EntryThesisRecord, EntryThesisResponse, HoldingThesisRecord,
    HoldingThesisResponse, PortfolioState, ThesisCitation,
)

STATE_DIR = Path("logs/thesis_shadow")
PROMPTS = Path(__file__).resolve().parent.parent / "prompts"
KST = ZoneInfo("Asia/Seoul")
MAX_PROMPT_CHARS = 180_000  # resource ceiling, not a candidate/claim quota
CAPTURE_DEADLINE = time(8, 55)
REVIEW_DEADLINE = time(17, 0)


def now_kst():
    return datetime.now(KST)


def digest(model):
    value = model.model_dump(mode="json") if hasattr(model, "model_dump") else model
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def _path(root, category, day, ticker):
    if not re.fullmatch(r"[0-9A-Z]{6}", ticker):
        raise ValueError("invalid_ticker")
    return root / category / f"{day.isoformat()}_{ticker}.json"


def _claim(path, record):
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with path.open("x") as file:
            file.write(record.model_dump_json(indent=2))
            file.flush()
            os.fsync(file.fileno())
    except FileExistsError:
        return False
    return True


def _finish(path, record):
    with tempfile.NamedTemporaryFile(mode="w", dir=path.parent, delete=False) as file:
        tmp = Path(file.name)
        try:
            file.write(record.model_dump_json(indent=2))
            file.flush()
            os.fsync(file.fileno())
        except BaseException:
            tmp.unlink(missing_ok=True)
            raise
    try:
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)


def _citations(citations: list[ThesisCitation], documents, *, full=False):
    sources = {d.source_id: d for d in documents}
    if not citations:
        raise ValueError("citations_missing")
    for citation in citations:
        doc = sources.get(citation.source_id)
        if doc is None or not citation.quote.strip() or citation.quote not in doc.content:
            raise ValueError("citation_not_in_supplied_source")
        if full and doc.scope not in {"full_text", "market_data"}:
            raise ValueError("headline_is_not_fact_confirmation")


def validate_entry(response, decision, documents):
    if response.status == "unavailable":
        if response.claims:
            raise ValueError("unavailable_with_claims")
        return
    if not response.claims:
        raise ValueError("documented_without_claims")
    reasoning = [decision.reason, *(o.reasoning or "" for o in decision.inputs)]
    for claim in response.claims:
        if not claim.statement.strip() or not claim.invalidation_condition.strip():
            raise ValueError("empty_claim")
        if not claim.reasoning_quote.strip() or not any(claim.reasoning_quote in x for x in reasoning):
            raise ValueError("premise_not_in_original_reasoning")
        _citations(claim.citations, documents)


def validate_review(response, thesis, documents):
    expected = list(range(len(thesis.claims)))
    if sorted(check.condition_index for check in response.checks) != expected:
        raise ValueError("missing_or_duplicate_condition")
    for check in response.checks:
        if not check.reasoning.strip():
            raise ValueError("empty_review_reason")
        if check.status != "unavailable" or check.citations:
            _citations(check.citations, documents, full=True)
    statuses = {check.status for check in response.checks}
    if "invalidated" in statuses:
        return "invalidated"
    return "unavailable" if "unavailable" in statuses else "intact"


def validate_new_documents(documents, thesis, now):
    cutoff = thesis.completed_at
    if cutoff is None:
        raise ValueError("entry_capture_incomplete")
    if len({d.source_id for d in documents}) != len(documents):
        raise ValueError("duplicate_source_id")
    original_ids = {d.source_id for d in thesis.documents}
    for document in documents:
        if (document.ticker != thesis.ticker or document.source_id in original_ids
                or document.observed_at.tzinfo is None or not cutoff < document.observed_at <= now
                or not document.content.strip()):
            raise ValueError("invalid_new_source_identity_or_time")
        if document.kind == "chart":
            if document.scope != "market_data" or document.published_on != now.astimezone(KST).date():
                raise ValueError("stale_new_chart")
        elif document.published_at is not None:
            if document.published_at.tzinfo is None or not cutoff < document.published_at <= now:
                raise ValueError("invalid_new_publication_time")
        elif document.published_on is None or not cutoff.astimezone(KST).date() < document.published_on <= now.astimezone(KST).date():
            raise ValueError("unproven_new_publication_date")


async def _call(template, data, response_model, *, label, root, deadline, clock):
    current = clock()
    capacity = codex_plan.load_capacity_status(current.astimezone(KST).date().isoformat())
    if not capacity.ordinary_usage_allowed or not capacity.buy_judgment_allowed:
        raise ValueError("codex_capacity_unavailable")
    if capacity.remaining_percent <= codex_plan.BUY_CAPACITY_FLOOR_PERCENT:
        raise ValueError("codex_capacity_low")
    user = template.format(data=json.dumps(data, ensure_ascii=False, sort_keys=True))
    if len(user) > MAX_PROMPT_CHARS:
        raise ValueError("input_budget_exceeded")
    seconds = (deadline - current).total_seconds()
    if seconds <= 0:
        raise ValueError("shadow_deadline_exceeded")
    response = await codex_plan.call_structured(
        system="", user=user, response_model=response_model, json_schema=response_model.model_json_schema(),
        label=label, log_path=root / "calls.jsonl", timeout_s=min(120.0, seconds),
    )
    if clock() >= deadline:
        raise ValueError("shadow_deadline_exceeded")
    return response


def _summary(root, phase, now, results):
    payload = {"day": now.date().isoformat(), "phase": phase, "shadow_only": True,
               "observed_at": now.isoformat(), "counts": dict(Counter(r["status"] for r in results)),
               "results": results}
    calls = root / "calls.jsonl"
    if calls.exists():
        today = [json.loads(line) for line in calls.read_text().splitlines() if line.strip()]
        today = [r for r in today if r.get("day") == now.date().isoformat()]
        payload["llm_stats_today"] = {
            label: {"calls": len(rows), "failed": sum(not r.get("success") for r in rows),
                    "input_tokens": sum(r.get("input_tokens", 0) for r in rows),
                    "output_tokens": sum(r.get("output_tokens", 0) for r in rows)}
            for label in sorted({r["label"] for r in today})
            if (rows := [r for r in today if r["label"] == label])
        }
    root.mkdir(parents=True, exist_ok=True)
    with (root / "runs.jsonl").open("a") as file:
        file.write(json.dumps(payload, ensure_ascii=False) + "\n")
    return payload


async def capture_entries(pending_path, *, root=None, clock=now_kst):
    root, now = root or STATE_DIR, clock().astimezone(KST)
    if not is_krx_trading_day(now.date()) or not time(7) <= now.time() < CAPTURE_DEADLINE:
        return {"status": "outside_capture_window"}
    if not pending_path.exists():
        return {"status": "no_pending"}
    payload = json.loads(pending_path.read_text())
    if payload.get("day") != now.date().isoformat() or payload.get("skipped"):
        return {"status": "no_current_approved_decisions"}
    template = (PROMPTS / "entry_thesis.md").read_text()
    version = "prompt:entry_thesis@" + hashlib.sha256(template.encode()).hexdigest()[:6]
    deadline = datetime.combine(now.date(), CAPTURE_DEADLINE, KST)
    results = []
    token = codex_plan.CALL_SESSION.set(codex_plan.CallSession())
    try:
        for raw in payload.get("decisions", []):
            decision = Decision.model_validate(raw["decision"])
            if (decision.action != "BUY" or raw.get("ticker") != decision.ticker
                    or raw.get("gate_result", {}).get("approved") is not True):
                results.append({"ticker": decision.ticker, "status": "unavailable", "reason": "invalid_approved_decision"})
                continue
            path = _path(root, "entries", now.date(), decision.ticker)
            record = EntryThesisRecord(
                day=now.date(), ticker=decision.ticker, started_at=clock(), decision=decision,
                decision_hash=digest(decision), prompt_version=version, model=codex_plan.DEFAULT_MODEL,
                git_commit=run_history.git_commit(),
            )
            if not _claim(path, record):
                results.append({"ticker": decision.ticker, "status": "already_attempted"})
                continue
            try:
                if decision.degraded:
                    raise ValueError("degraded_entry_decision")
                record.documents = thesis_sources.original_documents(decision)
                if any(d.observed_at.tzinfo is None or d.observed_at > record.started_at for d in record.documents):
                    raise ValueError("invalid_original_observation_time")
                _finish(path, record)  # preserve the exact input even if the process dies
                response = await _call(
                    template, {"decision": decision.model_dump(mode="json"),
                               "documents": [d.model_dump(mode="json") for d in record.documents]},
                    EntryThesisResponse, label="entry_thesis_shadow", root=root, deadline=deadline, clock=clock,
                )
                validate_entry(response, decision, record.documents)
                record.status, record.reason, record.claims = response.status, response.explanation, response.claims
            except Exception as exc:
                record.status, record.reason = "unavailable", type(exc).__name__ + ":" + str(exc)[:300]
            record.completed_at = clock()
            _finish(path, record)
            results.append({"ticker": decision.ticker, "status": record.status, "reason": record.reason})
    finally:
        codex_plan.CALL_SESSION.reset(token)
    return _summary(root, "capture", now, results)


def _entry_for(position, journal, root):
    if position.entry_day is None:
        raise ValueError("entry_day_missing")
    path = _path(root, "entries", position.entry_day, position.ticker)
    if not path.exists():
        raise ValueError("no_pre_entry_thesis")
    thesis = EntryThesisRecord.model_validate_json(path.read_text())
    if (thesis.status != "documented" or not thesis.claims or thesis.completed_at is None
            or thesis.completed_at.astimezone(KST).date() != position.entry_day
            or thesis.completed_at.astimezone(KST).time() >= CAPTURE_DEADLINE
            or thesis.ticker != position.ticker or thesis.day != position.entry_day):
        raise ValueError("entry_thesis_unavailable")
    buys = [r for r in journal if r.get("event") == "buy" and r.get("ticker") == position.ticker
            and r.get("day") == position.entry_day.isoformat()]
    if len(buys) != 1 or not buys[0].get("decision"):
        raise ValueError("original_entry_not_identifiable")
    decision = Decision.model_validate(buys[0]["decision"])
    if digest(decision) != thesis.decision_hash or digest(thesis.decision) != thesis.decision_hash:
        raise ValueError("entry_decision_mismatch")
    if [d.model_dump() for d in thesis_sources.original_documents(decision)] != [d.model_dump() for d in thesis.documents]:
        raise ValueError("entry_source_snapshot_mismatch")
    return thesis


async def review_holdings(portfolio_path, journal_path, *, fee_rate, tax_rate, root=None, clock=now_kst,
                          unconfirmed_path=None):
    root, now = root or STATE_DIR, clock().astimezone(KST)
    if not is_krx_trading_day(now.date()) or not time(16, 10) <= now.time() < REVIEW_DEADLINE:
        return {"status": "outside_review_window"}
    portfolio = PortfolioState.model_validate_json(portfolio_path.read_text())
    journal = [json.loads(line) for line in journal_path.read_text().splitlines() if line.strip()]
    unconfirmed = json.loads(unconfirmed_path.read_text()) if unconfirmed_path and unconfirmed_path.exists() else {}
    if not isinstance(unconfirmed, dict):
        raise ValueError("invalid_unconfirmed_order_state")
    template = (PROMPTS / "holding_thesis.md").read_text()
    version = "prompt:holding_thesis@" + hashlib.sha256(template.encode()).hexdigest()[:6]
    deadline = datetime.combine(now.date(), REVIEW_DEADLINE, KST)
    results = []
    token = codex_plan.CALL_SESSION.set(codex_plan.CallSession())
    try:
        for position in portfolio.positions:
            path = _path(root, "reviews", now.date(), position.ticker)
            record = HoldingThesisRecord(
                day=now.date(), ticker=position.ticker, started_at=clock(), position=position,
                prompt_version=version, model=codex_plan.DEFAULT_MODEL,
                git_commit=run_history.git_commit(),
                hypothetical_sell_fee_rate=fee_rate, hypothetical_sell_tax_rate=tax_rate,
                unconfirmed_order=position.ticker in unconfirmed,
            )
            if not _claim(path, record):
                results.append({"ticker": position.ticker, "status": "already_attempted"})
                continue
            try:
                if record.unconfirmed_order:
                    raise ValueError("unconfirmed_order")
                thesis = _entry_for(position, journal, root)
                record.thesis_hash = digest(thesis)
                remaining = (deadline - clock()).total_seconds()
                if remaining <= 0:
                    raise ValueError("shadow_deadline_exceeded")
                async with asyncio.timeout(remaining):
                    record.documents, record.source_failures = await asyncio.to_thread(
                        thesis_sources.collect_new_documents, thesis, clock(),
                    )
                _finish(path, record)
                if record.source_failures:
                    raise ValueError("source_collection_incomplete")
                validate_new_documents(record.documents, thesis, clock())
                if not record.documents:
                    record.status, record.reason = "no_new_evidence", "no_new_observed_documents"
                else:
                    response = await _call(
                        template, {"entry": thesis.model_dump(mode="json"),
                                   "new_documents": [d.model_dump(mode="json") for d in record.documents],
                                   "coverage": "Latest collected news/disclosure windows; not exhaustive."},
                        HoldingThesisResponse, label="holding_thesis_shadow", root=root, deadline=deadline, clock=clock,
                    )
                    record.status = validate_review(response, thesis, record.documents)
                    record.checks, record.reason = response.checks, "observed_evidence_only"
            except Exception as exc:
                record.status, record.reason = "unavailable", type(exc).__name__ + ":" + str(exc)[:300]
            record.completed_at = clock()
            _finish(path, record)
            results.append({"ticker": position.ticker, "status": record.status, "reason": record.reason})
    finally:
        codex_plan.CALL_SESSION.reset(token)
    return _summary(root, "review", now, results)
