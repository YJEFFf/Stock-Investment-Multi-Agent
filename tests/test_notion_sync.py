import json

import pytest
import requests

from src import notion_sync


class _FakeResponse:
    def __init__(self, status_code=200, body=None, headers=None, text=""):
        self.status_code = status_code
        self._body = body or {}
        self.headers = headers or {}
        self.text = text

    def json(self):
        return self._body


def test_create_trade_journal_database_success(monkeypatch):
    captured = {}

    def fake_request(method, url, headers, json, timeout):
        captured["method"] = method
        captured["url"] = url
        captured["json"] = json
        return _FakeResponse(200, {"id": "db-123"})

    monkeypatch.setattr(notion_sync.requests, "request", fake_request)

    db_id = notion_sync.create_trade_journal_database("parent-page-1")

    assert db_id == "db-123"
    assert captured["method"] == "POST"
    assert captured["url"] == "https://api.notion.com/v1/databases"
    assert captured["json"]["parent"] == {"type": "page_id", "page_id": "parent-page-1"}


def test_notion_request_retries_on_rate_limit_then_succeeds(monkeypatch):
    calls = {"n": 0}

    def fake_request(method, url, headers, json, timeout):
        calls["n"] += 1
        if calls["n"] == 1:
            return _FakeResponse(429, headers={"Retry-After": "0"})
        return _FakeResponse(200, {"id": "ok"})

    monkeypatch.setattr(notion_sync.requests, "request", fake_request)
    monkeypatch.setattr(notion_sync.time, "sleep", lambda *_: None)

    result = notion_sync._notion_request("POST", "/pages", {})

    assert result == {"id": "ok"}
    assert calls["n"] == 2


def test_notion_request_returns_none_on_client_error(monkeypatch):
    def fake_request(method, url, headers, json, timeout):
        return _FakeResponse(400, text="validation_error")

    monkeypatch.setattr(notion_sync.requests, "request", fake_request)

    assert notion_sync._notion_request("POST", "/pages", {}) is None


def test_notion_request_returns_none_after_exhausting_network_retries(monkeypatch):
    def fake_request(method, url, headers, json, timeout):
        raise requests.ConnectionError("down")

    monkeypatch.setattr(notion_sync.requests, "request", fake_request)
    monkeypatch.setattr(notion_sync.time, "sleep", lambda *_: None)

    assert notion_sync._notion_request("POST", "/pages", {}) is None


def _buy_entry(ticker="005930", day="2026-08-10"):
    return {
        "event": "buy",
        "day": day,
        "ticker": ticker,
        "sector": "반도체",
        "quantity": 10,
        "entry_price": 231200.0,
        "order_no": "ODNO1",
        "gap_pct": 0.004,
        "decision": {
            "ticker": ticker,
            "action": "BUY",
            "reason": "종합 판단 근거",
            "inputs": [{"agent": "chart", "ticker": ticker, "score": 0.9, "confidence": 0.8}],
            "degraded": False,
            "debate": [{"stance": "bull", "ticker": ticker, "argument": "상승 논리", "strength": 0.7}],
            "evidence": [],
        },
    }


def _sell_entry(ticker="005930", day="2026-08-11", reason="stop_loss", reasoning=None):
    return {
        "event": "sell",
        "day": day,
        "ticker": ticker,
        "reason": reason,
        "reasoning": reasoning,
        "sell_fraction": 1.0,
        "exit_price": 200000.0,
        "entry_price": 231200.0,
        "realized_pnl_pct": -0.13,
        "holding_days": 1,
    }


def _write_jsonl(path, entries):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w") as f:
        for e in entries:
            f.write(json.dumps(e) + "\n")


def test_sync_trade_journal_creates_rows_for_new_entries(monkeypatch, tmp_path):
    log_path = tmp_path / "trade_journal.jsonl"
    _write_jsonl(log_path, [_buy_entry(), _sell_entry()])

    captured_bodies = []

    def fake_request(method, path, body=None):
        captured_bodies.append(body)
        return {"id": "row-x"}

    monkeypatch.setattr(notion_sync, "_notion_request", fake_request)

    state_path = tmp_path / "notion_sync_state.json"
    summary = notion_sync.sync_trade_journal(log_path, "db-123", state_path=state_path)

    assert summary == {"synced": 2, "failed": 0, "skipped": 0}
    assert len(captured_bodies) == 2
    assert captured_bodies[0]["parent"] == {"database_id": "db-123"}
    assert captured_bodies[0]["properties"]["구분"] == {"select": {"name": "매수"}}
    assert captured_bodies[1]["properties"]["구분"] == {"select": {"name": "매도"}}
    assert captured_bodies[1]["properties"]["사유"] == {"select": {"name": "손절"}}

    assert json.loads(state_path.read_text())["synced_keys"] == sorted(
        ["buy:005930:2026-08-10", "sell:005930:2026-08-11"]
    )


def test_sync_trade_journal_skips_already_synced_entries(monkeypatch, tmp_path):
    log_path = tmp_path / "trade_journal.jsonl"
    entry = _buy_entry()
    _write_jsonl(log_path, [entry])

    state_path = tmp_path / "notion_sync_state.json"
    state_path.write_text(json.dumps({"synced_keys": ["buy:005930:2026-08-10"]}))

    def fail_request(*a, **k):
        raise AssertionError("이미 동기화된 항목은 다시 요청하면 안 된다")

    monkeypatch.setattr(notion_sync, "_notion_request", fail_request)

    summary = notion_sync.sync_trade_journal(log_path, "db-123", state_path=state_path)

    assert summary == {"synced": 0, "failed": 0, "skipped": 1}


def test_sync_trade_journal_does_not_mark_failed_rows_synced(monkeypatch, tmp_path):
    log_path = tmp_path / "trade_journal.jsonl"
    _write_jsonl(log_path, [_buy_entry()])

    monkeypatch.setattr(notion_sync, "_notion_request", lambda *a, **k: None)

    state_path = tmp_path / "notion_sync_state.json"
    summary = notion_sync.sync_trade_journal(log_path, "db-123", state_path=state_path)

    assert summary == {"synced": 0, "failed": 1, "skipped": 0}
    assert json.loads(state_path.read_text())["synced_keys"] == []


def test_sync_trade_journal_missing_log_file_returns_zeroes(tmp_path):
    summary = notion_sync.sync_trade_journal(
        tmp_path / "does_not_exist.jsonl", "db-123", state_path=tmp_path / "state.json"
    )
    assert summary == {"synced": 0, "failed": 0, "skipped": 0}


def test_sell_row_children_includes_reasoning_only_when_present():
    with_reasoning = notion_sync._sell_row_children(_sell_entry(reasoning="근거 없어짐"))
    assert len(with_reasoning) == 2

    without_reasoning = notion_sync._sell_row_children(_sell_entry(reasoning=None))
    assert without_reasoning == []


def test_buy_row_properties_and_children():
    entry = _buy_entry()
    props = notion_sync._buy_row_properties(entry)
    assert props["가격"] == {"number": 231200.0}
    assert props["수량"] == {"number": 10}

    children = notion_sync._buy_row_children(entry)
    paragraphs = [b for b in children if b["type"] == "paragraph"]
    assert any("종합 판단 근거" in b["paragraph"]["rich_text"][0]["text"]["content"] for b in paragraphs)


def test_create_intro_page_writes_full_content_not_just_a_link(monkeypatch):
    captured = {}

    def fake_request(method, path, body=None):
        captured["method"] = method
        captured["path"] = path
        captured["body"] = body
        return {"id": "intro-page-1"}

    monkeypatch.setattr(notion_sync, "_notion_request", fake_request)

    page_id = notion_sync.create_intro_page("parent-1")

    assert page_id == "intro-page-1"
    children = captured["body"]["children"]
    # 아키텍처 다이어그램과 핵심 계약 코드가 링크가 아니라 코드 블록으로 페이지 안에 그대로 들어있어야 한다.
    code_blocks = [b for b in children if b["type"] == "code"]
    assert any("리스크 게이트" in b["code"]["rich_text"][0]["text"]["content"] for b in code_blocks)
    assert any("class AnalystOpinion" in b["code"]["rich_text"][0]["text"]["content"] for b in code_blocks)


def test_refresh_intro_page_deletes_old_blocks_then_appends_new(monkeypatch):
    calls = []

    def fake_request(method, path, body=None):
        calls.append((method, path))
        if method == "GET":
            return {"results": [{"id": "block-1"}, {"id": "block-2"}]}
        return {"id": "ok"}

    monkeypatch.setattr(notion_sync, "_notion_request", fake_request)

    assert notion_sync.refresh_intro_page("intro-page-1") is True

    deleted = [path for method, path in calls if method == "DELETE"]
    assert deleted == ["/blocks/block-1", "/blocks/block-2"]
    assert calls[-1][0] == "PATCH"


def test_refresh_intro_page_handles_no_existing_blocks(monkeypatch):
    def fake_request(method, path, body=None):
        if method == "GET":
            return {"results": []}
        return {"id": "ok"}

    monkeypatch.setattr(notion_sync, "_notion_request", fake_request)

    assert notion_sync.refresh_intro_page("intro-page-1") is True
