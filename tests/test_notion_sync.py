import json

import pytest
import requests

from src import notion_sync
from src.schemas import PortfolioState, Position


@pytest.fixture(autouse=True)
def _no_real_ticker_name_lookup(monkeypatch):
    # _display_name이 실제 네이버 스크래핑(collectors.fetch_kospi200_ticker_names)을
    # 타지 않도록 기본적으로 막는다 — None을 반환하면 _display_name이 코드로
    # 폴백하므로, 이걸 쓰지 않는 기존 테스트들은 전과 동일하게 동작한다.
    monkeypatch.setattr(notion_sync.collectors, "fetch_kospi200_ticker_names", lambda: None)


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


def _buy_skipped_entry(ticker="005930", day="2026-08-10", reason="gap_too_large", gap_pct=0.042):
    return {"event": "buy_skipped", "day": day, "ticker": ticker, "reason": reason, "gap_pct": gap_pct}


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


def test_sync_trade_journal_handles_buy_skipped_events(monkeypatch, tmp_path):
    log_path = tmp_path / "trade_journal.jsonl"
    _write_jsonl(log_path, [_buy_skipped_entry()])

    captured_bodies = []

    def fake_request(method, path, body=None):
        captured_bodies.append(body)
        return {"id": "row-x"}

    monkeypatch.setattr(notion_sync, "_notion_request", fake_request)

    summary = notion_sync.sync_trade_journal(log_path, "db-123", state_path=tmp_path / "state.json")

    assert summary == {"synced": 1, "failed": 0, "skipped": 0}
    props = captured_bodies[0]["properties"]
    assert props["구분"] == {"select": {"name": "매수스킵"}}
    assert props["사유"] == {"select": {"name": "갭초과"}}
    body_text = json.dumps(captured_bodies[0]["children"], ensure_ascii=False)
    assert "4.2%" in body_text


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


def test_row_titles_use_display_name_but_ticker_column_keeps_code(monkeypatch):
    monkeypatch.setattr(notion_sync.collectors, "fetch_kospi200_ticker_names", lambda: {"005930": "삼성전자"})

    buy_props = notion_sync._buy_row_properties(_buy_entry())
    assert buy_props["이름"]["title"][0]["text"]["content"] == "삼성전자 매수"
    assert buy_props["티커"]["rich_text"][0]["text"]["content"] == "005930"

    sell_props = notion_sync._sell_row_properties(_sell_entry())
    assert sell_props["이름"]["title"][0]["text"]["content"] == "삼성전자 매도"

    skip_props = notion_sync._buy_skipped_row_properties(_buy_skipped_entry())
    assert skip_props["이름"]["title"][0]["text"]["content"] == "삼성전자 매수 스킵"


def test_display_name_falls_back_to_ticker_when_lookup_fails(monkeypatch):
    monkeypatch.setattr(notion_sync.collectors, "fetch_kospi200_ticker_names", lambda: None)
    assert notion_sync._display_name("005930") == "005930"


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


def _write_pipeline_jsonl(path, entries):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w") as f:
        for e in entries:
            f.write(json.dumps(e) + "\n")


def test_sync_daily_report_creates_one_page_summarizing_the_day(monkeypatch, tmp_path):
    pipeline_log = tmp_path / "pipeline.jsonl"
    _write_pipeline_jsonl(
        pipeline_log,
        [
            {
                "day": "2026-08-10",
                "ticker": "005930",
                "action": "BUY",
                "approved": True,
                "rejected_by": None,
                "avg_score": 0.9,
                "avg_confidence": 0.8,
            },
            {
                "day": "2026-08-10",
                "ticker": "000660",
                "action": "BUY",
                "approved": False,
                "rejected_by": "position_limit",
                "avg_score": 0.87,
                "avg_confidence": 0.75,
                "reason": "이미 종목당 한도를 채워 신규 편입은 보류한다",
            },
            {
                "day": "2026-08-09",
                "ticker": "999999",
                "action": "HOLD",
                "approved": True,
                "rejected_by": None,
                "avg_score": 0.1,
                "avg_confidence": 0.5,
            },
        ],
    )
    trade_journal_log = tmp_path / "trade_journal.jsonl"
    _write_jsonl(
        trade_journal_log,
        [_buy_entry(day="2026-08-10"), _buy_skipped_entry(ticker="035420", day="2026-08-10")],
    )

    portfolio = PortfolioState(
        positions=[Position(ticker="005930", sector="반도체", weight=0.08, entry_price=231200.0, quantity=10)],
        cash_weight=0.92,
    )

    captured = {}

    def fake_request(method, path, body=None):
        captured["body"] = body
        return {"id": "report-page-1"}

    monkeypatch.setattr(notion_sync, "_notion_request", fake_request)

    state_path = tmp_path / "notion_daily_report_state.json"
    created = notion_sync.sync_daily_report(
        "2026-08-10",
        portfolio,
        "report-db-1",
        pipeline_log_path=pipeline_log,
        trade_journal_log_path=trade_journal_log,
        state_path=state_path,
    )

    assert created is True
    body = captured["body"]
    assert body["parent"] == {"database_id": "report-db-1"}
    assert body["properties"]["매수"] == {"number": 1}
    assert body["properties"]["매도"] == {"number": 0}
    assert body["properties"]["보유종목수"] == {"number": 1}
    # 다른 날짜(2026-08-09) 판단은 포함되면 안 된다.
    all_text = json.dumps(body["children"], ensure_ascii=False)
    assert "999999" not in all_text
    assert "005930" in all_text
    assert "000660" in all_text  # 거부된 판단도 요약에는 포함
    assert "이미 종목당 한도를 채워" in all_text  # HOLD/거부 종목도 실제 판단 문장이 노출됨
    assert "035420" in all_text  # 게이트 승인됐지만 체결 스킵된 "특별한 일"도 노출됨
    assert "특별한 일" in all_text

    assert json.loads(state_path.read_text())["synced_days"] == ["2026-08-10"]


def test_daily_report_shows_display_names_and_cash_weight_to_two_decimals(monkeypatch):
    monkeypatch.setattr(
        notion_sync.collectors, "fetch_kospi200_ticker_names", lambda: {"005930": "삼성전자", "035420": "네이버"}
    )

    portfolio = PortfolioState(
        positions=[Position(ticker="005930", sector="반도체", weight=0.08, entry_price=231200.0, quantity=10)],
        cash_weight=0.8765,
    )
    blocks = notion_sync._daily_report_children(
        "2026-08-10",
        decisions_today=[{"ticker": "005930", "action": "BUY", "approved": True, "rejected_by": None, "avg_score": 0.9}],
        buys=[_buy_entry()],
        sells=[],
        skips=[_buy_skipped_entry(ticker="035420")],
        portfolio=portfolio,
    )
    all_text = json.dumps(blocks, ensure_ascii=False)

    assert "삼성전자" in all_text
    assert "네이버" in all_text
    assert "현금 비중: 87.65%" in all_text


def test_sync_daily_report_skips_if_already_synced_for_day(monkeypatch, tmp_path):
    state_path = tmp_path / "state.json"
    state_path.write_text(json.dumps({"synced_days": ["2026-08-10"]}))

    def fail_request(*a, **k):
        raise AssertionError("이미 만든 날짜는 다시 요청하면 안 된다")

    monkeypatch.setattr(notion_sync, "_notion_request", fail_request)

    created = notion_sync.sync_daily_report(
        "2026-08-10", PortfolioState(), "report-db-1", state_path=state_path
    )

    assert created is False


def test_sync_daily_report_handles_no_decisions_that_day(tmp_path, monkeypatch):
    monkeypatch.setattr(notion_sync, "_notion_request", lambda *a, **k: {"id": "x"})

    created = notion_sync.sync_daily_report(
        "2026-08-09",
        PortfolioState(),
        "report-db-1",
        pipeline_log_path=tmp_path / "does_not_exist.jsonl",
        trade_journal_log_path=tmp_path / "also_missing.jsonl",
        state_path=tmp_path / "state.json",
    )

    assert created is True
