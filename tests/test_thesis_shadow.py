import asyncio
import json
from datetime import date, datetime, timedelta
from types import SimpleNamespace

import pytest

from src import analysts, judgment, thesis_shadow as shadow, thesis_sources as sources
from src.schemas import (
    AnalystOpinion, Decision, EntryThesisRecord, EntryThesisResponse, HoldingThesisCheck,
    HoldingThesisRecord, HoldingThesisResponse, MarketContext, NewsContext, NewsItem,
    OHLCVBar, PortfolioState, Position, ThesisCitation, ThesisClaim, ThesisDocument,
)

ENTRY = datetime(2026, 11, 5, 7, 30, tzinfo=shadow.KST)
REVIEW = datetime(2026, 11, 6, 16, 20, tzinfo=shadow.KST)
OLD_URL = "https://n.news.naver.com/mnews/article/001/12345"
NEW_URL = "https://n.news.naver.com/mnews/article/001/12346"


def decision():
    context = NewsContext(ticker="005930", sector="반도체", as_of=ENTRY - timedelta(minutes=10),
                          company_news=[NewsItem(title="대형 수주 계약 체결", press="공식", url=OLD_URL,
                                                 published_at=ENTRY - timedelta(days=1))], sector_news=[])
    opinion = AnalystOpinion(agent="news", ticker="005930", score=0.8, confidence=0.9,
                             evidence=["prompt:news@123456", OLD_URL], as_of=context.as_of,
                             reasoning="수주 계약 체결이 실적 개선의 근거다.", source_context=context)
    return Decision(ticker="005930", action="BUY", reason="수주 계약을 근거로 매수한다.",
                    inputs=[opinion], degraded=False)


def claim():
    return ThesisClaim(statement="수주 계약이 유지된다", invalidation_condition="해당 계약 해지가 공식 확인됨",
                       reasoning_quote="수주 계약 체결", citations=[ThesisCitation(source_id=OLD_URL, quote="대형 수주 계약 체결")])


def fresh_document(**update):
    return ThesisDocument(source_id=NEW_URL, ticker="005930", kind="news", scope="full_text",
                          content="회사는 해당 수주 계약 해지를 공시했다.", observed_at=REVIEW,
                          published_at=REVIEW - timedelta(hours=2)).model_copy(update=update)


def response(status="invalidated", **update):
    check = HoldingThesisCheck(condition_index=0, status=status, reasoning="해당 계약의 해지가 확인됐다.",
                              citations=[ThesisCitation(source_id=NEW_URL, quote="해당 수주 계약 해지")])
    return HoldingThesisResponse(checks=[check.model_copy(update=update)])


@pytest.fixture
def setup(tmp_path, monkeypatch):
    pending = tmp_path / "pending_buys.json"
    pending.write_text(json.dumps({"day": ENTRY.date().isoformat(), "decisions": [{
        "ticker": "005930", "gate_result": {"approved": True, "rejected_by": None},
        "decision": decision().model_dump(mode="json"),
    }]}))
    portfolio, journal = tmp_path / "portfolio.json", tmp_path / "journal.jsonl"
    position = Position(ticker="005930", sector="반도체", weight=0.08, quantity=30,
                        entry_day=ENTRY.date(), entry_price=100, peak_price=100)
    portfolio.write_text(PortfolioState(positions=[position], cash_weight=0.92).model_dump_json())
    journal.write_text(json.dumps({"event": "buy", "day": ENTRY.date().isoformat(), "ticker": "005930",
                                  "quantity": 30, "decision": decision().model_dump(mode="json")}) + "\n")
    monkeypatch.setattr(shadow.codex_plan, "load_capacity_status", lambda day: SimpleNamespace(
        ordinary_usage_allowed=True, buy_judgment_allowed=True, remaining_percent=50))
    calls = []

    async def model(**kwargs):
        calls.append(kwargs)
        await asyncio.sleep(0)
        if kwargs["label"] == "entry_thesis_shadow":
            return EntryThesisResponse(status="documented", explanation="원래 근거와 출처 확인", claims=[claim()])
        return response()

    monkeypatch.setattr(shadow.codex_plan, "call_structured", model)
    monkeypatch.setattr(sources, "collect_new_documents", lambda thesis, now: ([fresh_document()], []))
    return SimpleNamespace(root=tmp_path / "shadow", pending=pending, portfolio=portfolio,
                           journal=journal, position=position, calls=calls)


def capture(s, clock=lambda: ENTRY):
    return asyncio.run(shadow.capture_entries(s.pending, root=s.root, clock=clock))


def review(s, clock=lambda: REVIEW):
    return asyncio.run(shadow.review_holdings(s.portfolio, s.journal, root=s.root, clock=clock,
                                            fee_rate=0.000142, tax_rate=0.002))


def entry_record(s):
    return EntryThesisRecord.model_validate_json(next((s.root / "entries").glob("*.json")).read_text())


def review_record(s):
    return HoldingThesisRecord.model_validate_json(next((s.root / "reviews").glob("*.json")).read_text())


def test_capture_preserves_reasoning_context_and_frozen_condition(setup):
    s = setup
    before = s.pending.read_bytes()
    result = capture(s)
    assert result["counts"] == {"documented": 1}
    saved = entry_record(s)
    assert saved.claims == [claim()]
    assert saved.decision == decision()
    assert saved.documents[0].scope == "headline"
    assert s.pending.read_bytes() == before
    assert s.calls[0]["log_path"] == s.root / "calls.jsonl"
    assert s.calls[0]["json_schema"]["additionalProperties"] is False


def test_review_uses_entry_and_new_bodies_without_mutating_trade_files(setup):
    s = setup
    capture(s)
    files = [s.pending, s.portfolio, s.journal, *list((s.root / "entries").glob("*.json"))]
    before = {f: f.read_bytes() for f in files}
    assert review(s)["counts"] == {"invalidated": 1}
    row = review_record(s)
    assert row.thesis_hash == shadow.digest(entry_record(s))
    assert row.shadow_only is True
    assert "action" not in row.model_dump() and "sell_fraction" not in row.model_dump()
    assert "해당 계약 해지가 공식 확인됨" in s.calls[-1]["user"]
    assert NEW_URL in s.calls[-1]["user"] and OLD_URL in s.calls[-1]["user"]
    assert before == {f: f.read_bytes() for f in files}


def test_legacy_holding_is_unavailable_not_reconstructed(setup):
    s = setup
    assert review(s)["counts"] == {"unavailable": 1}
    assert "no_pre_entry_thesis" in review_record(s).reason
    assert not s.calls


def test_degraded_approved_candidate_does_not_abort_later_capture(setup):
    payload = json.loads(setup.pending.read_text())
    degraded = decision().model_copy(update={"ticker": "000660", "degraded": True})
    payload["decisions"].insert(0, {"ticker": "000660", "decision": degraded.model_dump(mode="json"),
                                     "gate_result": {"approved": True}})
    setup.pending.write_text(json.dumps(payload))
    assert capture(setup)["counts"] == {"unavailable": 1, "documented": 1}
    assert len(setup.calls) == 1


@pytest.mark.parametrize("update", [
    {"ticker": "000660"}, {"published_at": REVIEW + timedelta(days=1)},
    {"observed_at": REVIEW + timedelta(seconds=1)}, {"source_id": OLD_URL},
])
def test_new_source_identity_and_time_verified_before_model(setup, monkeypatch, update):
    capture(setup)
    monkeypatch.setattr(sources, "collect_new_documents", lambda *a: ([fresh_document(**update)], []))
    assert review(setup)["counts"] == {"unavailable": 1}
    assert len(setup.calls) == 1


def test_unconfirmed_order_is_unavailable_without_source_or_model_call(setup, monkeypatch):
    capture(setup)
    orders = setup.root / "unconfirmed.json"
    orders.write_text('{"005930": {"side": "sell"}}')
    def forbidden(*a):
        raise AssertionError("No source call while position is uncertain")
    monkeypatch.setattr(sources, "collect_new_documents", forbidden)
    result = asyncio.run(shadow.review_holdings(setup.portfolio, setup.journal, root=setup.root,
                          clock=lambda: REVIEW, fee_rate=0, tax_rate=0, unconfirmed_path=orders))
    assert result["counts"] == {"unavailable": 1}
    assert "unconfirmed_order" in review_record(setup).reason
    assert len(setup.calls) == 1


def test_repeated_capture_and_review_do_not_resample(setup):
    s = setup
    capture(s); capture(s); review(s); review(s)
    assert [c["label"] for c in s.calls] == ["entry_thesis_shadow", "holding_thesis_shadow"]


def test_two_concurrent_captures_claim_before_model_call(setup):
    async def both():
        return await asyncio.gather(*(shadow.capture_entries(setup.pending, root=setup.root, clock=lambda: ENTRY)
                                      for _ in range(2)))
    result = asyncio.run(both())
    assert len(setup.calls) == 1
    assert {next(iter(r["counts"])) for r in result} == {"documented", "already_attempted"}


def test_uncertain_failed_call_is_not_retried(setup, monkeypatch):
    attempts = []

    async def fail(**kwargs):
        attempts.append(kwargs)
        raise TimeoutError("result unknown")

    monkeypatch.setattr(shadow.codex_plan, "call_structured", fail)
    capture(setup); capture(setup)
    assert len(attempts) == 1 and entry_record(setup).status == "unavailable"


def test_interrupted_started_record_prevents_reanalysis(setup):
    capture(setup)
    path = next((setup.root / "entries").glob("*.json"))
    row = entry_record(setup).model_copy(update={"status": "started", "completed_at": None, "claims": []})
    path.write_text(row.model_dump_json())
    assert capture(setup)["counts"] == {"already_attempted": 1}
    assert review(setup)["counts"] == {"unavailable": 1}
    assert len(setup.calls) == 1


@pytest.mark.parametrize("when", [ENTRY.replace(hour=9), ENTRY.replace(hour=6), ENTRY.replace(day=7)])
def test_capture_cannot_backfill_after_entry_or_on_weekend(setup, when):
    assert capture(setup, lambda: when)["status"] == "outside_capture_window"
    assert not setup.calls


def test_capture_refuses_stale_pending(setup):
    payload = json.loads(setup.pending.read_text()); payload["day"] = "2026-11-04"
    setup.pending.write_text(json.dumps(payload))
    assert capture(setup)["status"] == "no_current_approved_decisions"
    assert not setup.calls


def test_late_completion_is_unavailable(setup):
    count = 0

    def clock():
        nonlocal count
        count += 1
        return ENTRY if count < 4 else ENTRY.replace(hour=8, minute=56)

    assert capture(setup, clock)["counts"] == {"unavailable": 1}
    assert "deadline" in entry_record(setup).reason


def test_capacity_unavailable_never_uses_another_provider(setup, monkeypatch):
    monkeypatch.setattr(shadow.codex_plan, "load_capacity_status", lambda day: SimpleNamespace(
        ordinary_usage_allowed=True, buy_judgment_allowed=False, remaining_percent=1))
    assert capture(setup)["counts"] == {"unavailable": 1}
    assert not setup.calls


def test_collection_failure_is_not_a_hold_and_never_calls_model(setup, monkeypatch):
    capture(setup)
    monkeypatch.setattr(sources, "collect_new_documents", lambda *a: ([fresh_document()], ["disclosure:failed"]))
    assert review(setup)["counts"] == {"unavailable": 1}
    assert len(setup.calls) == 1
    assert review_record(setup).source_failures == ["disclosure:failed"]


def test_no_new_data_is_distinct_from_unavailable_and_hold(setup, monkeypatch):
    capture(setup)
    monkeypatch.setattr(sources, "collect_new_documents", lambda *a: ([], []))
    assert review(setup)["counts"] == {"no_new_evidence": 1}
    assert len(setup.calls) == 1


def test_changed_entry_journal_cannot_be_attached_to_frozen_thesis(setup):
    capture(setup)
    row = json.loads(setup.journal.read_text()); row["decision"]["reason"] = "사후 작성된 다른 이유"
    setup.journal.write_text(json.dumps(row))
    assert review(setup)["counts"] == {"unavailable": 1}
    assert "entry_decision_mismatch" in review_record(setup).reason


@pytest.mark.parametrize("bad", [
    ThesisCitation(source_id="https://invented.example", quote="해당 수주 계약 해지"),
    ThesisCitation(source_id=NEW_URL, quote="없는 숫자 50%"),
    ThesisCitation(source_id=NEW_URL, quote=""),
])
def test_invented_source_or_quote_cannot_confirm_a_condition(setup, bad):
    capture(setup)
    with pytest.raises(ValueError):
        shadow.validate_review(response(citations=[bad]), entry_record(setup), [fresh_document()])


def test_headline_cannot_confirm_a_condition(setup):
    capture(setup)
    with pytest.raises(ValueError, match="headline"):
        shadow.validate_review(response(), entry_record(setup), [fresh_document(scope="headline")])


def test_all_frozen_conditions_must_be_checked_once(setup):
    capture(setup)
    for checks in ([], response().checks * 2, [response().checks[0].model_copy(update={"condition_index": 1})]):
        with pytest.raises(ValueError, match="condition"):
            shadow.validate_review(HoldingThesisResponse(checks=checks), entry_record(setup), [fresh_document()])


def test_unavailable_does_not_need_a_fabricated_quote(setup):
    capture(setup)
    assert shadow.validate_review(response("unavailable", citations=[]), entry_record(setup), []) == "unavailable"


def test_thesis_cannot_invent_original_reasoning():
    answer = EntryThesisResponse(status="documented", explanation="", claims=[claim().model_copy(update={"reasoning_quote": "성장률 50%"})])
    with pytest.raises(ValueError, match="original_reasoning"):
        shadow.validate_entry(answer, decision(), sources.original_documents(decision()))


def test_original_context_and_reasoning_are_preserved_without_changing_live_prompt(monkeypatch):
    async def fake(**kwargs):
        return analysts._NewsAnalysisResponse(score=0.8, confidence=0.9, reasoning="원래 수주 근거")
    monkeypatch.setattr(analysts.llm, "call_structured", fake)
    context = decision().inputs[0].source_context
    opinion = asyncio.run(analysts.news_analyst(context))
    assert opinion.reasoning == "원래 수주 근거" and opinion.source_context == context
    legacy = opinion.model_copy(update={"reasoning": None, "source_context": None})
    assert judgment._format_opinions([opinion]) == judgment._format_opinions([legacy])
