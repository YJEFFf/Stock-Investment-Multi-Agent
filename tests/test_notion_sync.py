import asyncio
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


@pytest.fixture(autouse=True)
def _no_real_translation(monkeypatch):
    # 노션에 쓰기 전 영어 판단 로그를 한국어로 옮기는 단계(src/translate.py)가
    # 실제 Claude API를 타지 않게 기본적으로 항등 함수로 막는다 — 번역 자체를
    # 검증하는 테스트는 이 픽스처를 개별적으로 오버라이드한다.
    async def _identity(text, label="translate"):
        return text

    monkeypatch.setattr(notion_sync.translate, "to_korean", _identity)


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
    summary = asyncio.run(notion_sync.sync_trade_journal(log_path, "db-123", state_path=state_path))

    assert summary == {"synced": 2, "updated": 0, "failed": 0, "skipped": 0}
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

    summary = asyncio.run(notion_sync.sync_trade_journal(log_path, "db-123", state_path=tmp_path / "state.json"))

    assert summary == {"synced": 1, "updated": 0, "failed": 0, "skipped": 0}
    props = captured_bodies[0]["properties"]
    assert props["구분"] == {"select": {"name": "매수스킵"}}
    assert props["사유"] == {"select": {"name": "갭초과"}}
    body_text = json.dumps(captured_bodies[0]["children"], ensure_ascii=False)
    assert "4.20%" in body_text


def test_sync_trade_journal_syncs_both_same_day_same_ticker_sells(monkeypatch, tmp_path):
    # 트레일링 익절처럼 같은 종목을 같은 날 두 번(1/3씩) 매도하면 event:ticker:day
    # 키가 충돌해서 두 번째 매도가 조용히 스킵됐던 버그(2026-08-12 발견)의 회귀 테스트.
    log_path = tmp_path / "trade_journal.jsonl"
    first_sell = _sell_entry(ticker="192820", day="2026-08-12", reason="take_profit_trail")
    first_sell["exit_price"] = 252000.0
    second_sell = _sell_entry(ticker="192820", day="2026-08-12", reason="take_profit_trail")
    second_sell["exit_price"] = 240500.0
    _write_jsonl(log_path, [first_sell, second_sell])

    captured_bodies = []
    monkeypatch.setattr(
        notion_sync, "_notion_request", lambda method, path, body=None: captured_bodies.append(body) or {"id": "row-x"}
    )

    state_path = tmp_path / "state.json"
    summary = asyncio.run(notion_sync.sync_trade_journal(log_path, "db-123", state_path=state_path))

    assert summary == {"synced": 2, "updated": 0, "failed": 0, "skipped": 0}
    prices = {b["properties"]["가격"]["number"] for b in captured_bodies}
    assert prices == {252000.0, 240500.0}
    assert json.loads(state_path.read_text())["synced_keys"] == sorted(
        ["sell:192820:2026-08-12", "sell:192820:2026-08-12:1"]
    )


def test_sync_trade_journal_keeps_unsuffixed_key_for_single_occurrence(monkeypatch, tmp_path):
    # 하루 한 번만 발생하는 흔한 경우엔 키 형식이 기존 그대로여야, 이미 배포된 서버의
    # notion_sync_state.json에 쌓인 과거 키들과 계속 맞물려서 중복 재동기화가 안 된다.
    log_path = tmp_path / "trade_journal.jsonl"
    _write_jsonl(log_path, [_buy_entry()])

    state_path = tmp_path / "state.json"
    state_path.write_text(json.dumps({"synced_keys": ["buy:005930:2026-08-10"]}))

    def fail_request(*a, **k):
        raise AssertionError("이미 동기화된 항목은 다시 요청하면 안 된다")

    monkeypatch.setattr(notion_sync, "_notion_request", fail_request)

    summary = asyncio.run(notion_sync.sync_trade_journal(log_path, "db-123", state_path=state_path))

    assert summary == {"synced": 0, "updated": 0, "failed": 0, "skipped": 1}


def test_sync_trade_journal_skips_already_synced_entries(monkeypatch, tmp_path):
    log_path = tmp_path / "trade_journal.jsonl"
    entry = _buy_entry()
    _write_jsonl(log_path, [entry])

    state_path = tmp_path / "notion_sync_state.json"
    state_path.write_text(json.dumps({"synced_keys": ["buy:005930:2026-08-10"]}))

    def fail_request(*a, **k):
        raise AssertionError("이미 동기화된 항목은 다시 요청하면 안 된다")

    monkeypatch.setattr(notion_sync, "_notion_request", fail_request)

    summary = asyncio.run(notion_sync.sync_trade_journal(log_path, "db-123", state_path=state_path))

    assert summary == {"synced": 0, "updated": 0, "failed": 0, "skipped": 1}


def test_sync_trade_journal_does_not_mark_failed_rows_synced(monkeypatch, tmp_path):
    log_path = tmp_path / "trade_journal.jsonl"
    _write_jsonl(log_path, [_buy_entry()])

    monkeypatch.setattr(notion_sync, "_notion_request", lambda *a, **k: None)

    state_path = tmp_path / "notion_sync_state.json"
    summary = asyncio.run(notion_sync.sync_trade_journal(log_path, "db-123", state_path=state_path))

    assert summary == {"synced": 0, "updated": 0, "failed": 1, "skipped": 0}
    assert json.loads(state_path.read_text())["synced_keys"] == []


def test_sync_trade_journal_missing_log_file_returns_zeroes(tmp_path):
    summary = asyncio.run(
        notion_sync.sync_trade_journal(tmp_path / "does_not_exist.jsonl", "db-123", state_path=tmp_path / "state.json")
    )
    assert summary == {"synced": 0, "updated": 0, "failed": 0, "skipped": 0}


def test_sell_row_children_includes_reasoning_section_only_when_present():
    """LLM 재량 매도의 '판단 사유' 절은 reasoning이 있을 때만 붙는다. 다만 본문
    자체는 이제 reasoning이 없어도 비지 않는다 — 결정론적 매도는 발동 산식이
    대신 들어간다(_trigger_explanation)."""
    with_reasoning = json.dumps(
        asyncio.run(notion_sync._sell_row_children(_sell_entry(reasoning="근거 없어짐"))), ensure_ascii=False
    )
    assert "판단 사유" in with_reasoning
    assert "근거 없어짐" in with_reasoning

    without_reasoning = json.dumps(
        asyncio.run(notion_sync._sell_row_children(_sell_entry(reasoning=None))), ensure_ascii=False
    )
    assert "판단 사유" not in without_reasoning


def test_buy_row_properties_and_children():
    entry = _buy_entry()
    props = notion_sync._buy_row_properties(entry)
    assert props["가격"] == {"number": 231200.0}
    assert props["수량"] == {"number": 10}
    assert props["총매수금액"] == {"number": 2312000.0}

    children = asyncio.run(notion_sync._buy_row_children(entry))
    paragraphs = [b for b in children if b["type"] == "paragraph"]
    assert any("종합 판단 근거" in b["paragraph"]["rich_text"][0]["text"]["content"] for b in paragraphs)


def test_sell_row_properties_rounds_realized_pnl_to_two_decimal_percent():
    entry = _sell_entry()
    entry["realized_pnl_pct"] = 0.14523809523809525
    props = notion_sync._sell_row_properties(entry)
    assert props["실현손익률"] == {"number": 0.1452}


def test_daily_report_properties_rounds_cash_weight_to_two_decimal_percent():
    portfolio = PortfolioState(cash_weight=0.7243999999999999)
    props = notion_sync._daily_report_properties("2026-08-12", buys=[], sells=[], portfolio=portfolio)
    assert props["현금비중"] == {"number": 0.7244}


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

    def fake_request(method, path, body=None, notion_version=None):
        if path == "/pages":
            captured["body"] = body
        return {"id": "report-page-1"}

    monkeypatch.setattr(notion_sync, "_notion_request", fake_request)

    state_path = tmp_path / "notion_daily_report_state.json"
    created = asyncio.run(
        notion_sync.sync_daily_report(
            "2026-08-10",
            portfolio,
            "report-db-1",
            pipeline_log_path=pipeline_log,
            trade_journal_log_path=trade_journal_log,
            state_path=state_path,
        )
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
    assert "총 2개 종목 판단" in all_text  # 요약 문장은 그날 전체 판단 기준 그대로
    assert "BUY 승인 1개" in all_text
    assert "게이트 거부 1개" in all_text
    assert "005930" in all_text  # 승인된 매수는 상세 내용에 나온다
    # 상세 내용은 승인된 매수만 — 거부된 판단(000660)은 요약 숫자에만 잡히고 상세 목록엔 안 나온다.
    assert "000660" not in all_text
    assert "이미 종목당 한도를 채워" not in all_text
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
    blocks = asyncio.run(
        notion_sync._daily_report_children(
            "2026-08-10",
            decisions_today=[
                {"ticker": "005930", "action": "BUY", "approved": True, "rejected_by": None, "avg_score": 0.9}
            ],
            buys=[_buy_entry()],
            sells=[],
            skips=[_buy_skipped_entry(ticker="035420")],
            portfolio=portfolio,
        )
    )
    all_text = json.dumps(blocks, ensure_ascii=False)

    assert "삼성전자" in all_text
    assert "네이버" in all_text
    assert "현금 비중: 87.65%" in all_text


def test_daily_report_summary_section_uses_total_value_when_given():
    portfolio = PortfolioState(cash_weight=0.3)
    blocks = asyncio.run(
        notion_sync._daily_report_children(
            "2026-08-10",
            decisions_today=[],
            buys=[],
            sells=[],
            skips=[],
            portfolio=portfolio,
            total_value=100_000_000.0,
        )
    )
    all_text = json.dumps(blocks, ensure_ascii=False)

    assert "총정리" in all_text
    assert "투자한 금액: 70,000,000원" in all_text
    assert "남아있는 현금: 30,000,000원" in all_text
    assert "총 금액: 100,000,000원" in all_text


def test_daily_report_summary_section_omitted_when_total_value_unavailable():
    portfolio = PortfolioState(cash_weight=0.3)
    blocks = asyncio.run(
        notion_sync._daily_report_children(
            "2026-08-10", decisions_today=[], buys=[], sells=[], skips=[], portfolio=portfolio
        )
    )
    all_text = json.dumps(blocks, ensure_ascii=False)

    assert "총정리" in all_text
    assert "조회 실패" in all_text
    assert "원" not in all_text.split("총정리")[1]


def test_sync_daily_report_skips_if_already_synced_for_day(monkeypatch, tmp_path):
    state_path = tmp_path / "state.json"
    state_path.write_text(json.dumps({"synced_days": ["2026-08-10"]}))

    def fail_request(*a, **k):
        raise AssertionError("이미 만든 날짜는 다시 요청하면 안 된다")

    monkeypatch.setattr(notion_sync, "_notion_request", fail_request)

    created = asyncio.run(
        notion_sync.sync_daily_report("2026-08-10", PortfolioState(), "report-db-1", state_path=state_path)
    )

    assert created is False


def test_sync_daily_report_handles_no_decisions_that_day(tmp_path, monkeypatch):
    monkeypatch.setattr(notion_sync, "_notion_request", lambda *a, **k: {"id": "x"})

    created = asyncio.run(
        notion_sync.sync_daily_report(
            "2026-08-09",
            PortfolioState(),
            "report-db-1",
            pipeline_log_path=tmp_path / "does_not_exist.jsonl",
            trade_journal_log_path=tmp_path / "also_missing.jsonl",
            state_path=tmp_path / "state.json",
        )
    )

    assert created is True


# --- 매도 행의 비중/금액 속성 (사용자 요청 2026-08-15) ---


def test_sell_row_uses_position_relative_fractions_not_portfolio_weight():
    """노션에 보이는 비중은 **이 종목 보유분 기준**이다(합 100%). 포트폴리오 전체
    대비 값(portfolio_weight_*)은 로그엔 남지만 화면엔 안 나온다 — 사용자가 그걸로는
    해석이 안 된다고 했다."""
    entry = _sell_entry(reason="take_profit_trail")
    entry.update(
        shares_sold=10,
        shares_before=30,
        shares_after=20,
        sell_amount=2_000_000.0,
        position_fraction_sold=1 / 3,
        position_fraction_remaining=2 / 3,
        portfolio_weight_sold=0.04,  # 이 값이 화면에 새면 안 된다
        portfolio_weight_after=0.08,
    )

    properties = notion_sync._sell_row_properties(entry)

    assert properties["수량"]["number"] == 10
    assert properties["잔여수량"]["number"] == 20
    assert properties["매도금액"]["number"] == 2_000_000
    assert properties["매도비중"]["number"] == pytest.approx(0.3333, abs=1e-4)
    assert properties["잔여비중"]["number"] == pytest.approx(0.6667, abs=1e-4)
    # 두 값의 합이 100%여야 한 행만 보고 해석된다.
    assert properties["매도비중"]["number"] + properties["잔여비중"]["number"] == pytest.approx(1.0)


def test_sell_row_omits_new_fields_for_older_log_entries():
    """이 기능 이전에 쌓인 매도 로그엔 이 필드들이 없다 — 없으면 그냥 비운다."""
    properties = notion_sync._sell_row_properties(_sell_entry())

    assert "매도금액" not in properties
    assert "매도비중" not in properties
    assert "잔여비중" not in properties
    assert "잔여수량" not in properties


def test_ensure_trade_journal_properties_patches_the_existing_db(monkeypatch):
    """DB 생성은 .env에 ID가 있으면 건너뛰므로, 나중에 추가된 속성은 PATCH로 채워야
    한다 — 없는 속성에 값을 쓰면 노션이 400을 내고 매도 동기화가 통째로 실패한다."""
    captured = {}

    def fake_request(method, path, body=None):
        captured.update(method=method, path=path, body=body)
        return {"id": "db-1"}

    monkeypatch.setattr(notion_sync, "_notion_request", fake_request)

    assert notion_sync.ensure_trade_journal_properties("db-1") is True
    assert captured["method"] == "PATCH"
    assert captured["path"] == "/databases/db-1"
    assert set(captured["body"]["properties"]) == {"매도금액", "매도비중", "잔여비중", "잔여수량"}


def test_ensure_trade_journal_properties_reports_failure(monkeypatch):
    monkeypatch.setattr(notion_sync, "_notion_request", lambda *a, **k: None)
    assert notion_sync.ensure_trade_journal_properties("db-1") is False


def test_added_properties_are_all_declared_in_the_create_schema():
    """create 함수와 마이그레이션 목록이 갈라지면, 새로 만든 워크스페이스와 기존
    워크스페이스의 DB 스키마가 달라진다."""
    import inspect

    source = inspect.getsource(notion_sync.create_trade_journal_database)
    for name in notion_sync._TRADE_JOURNAL_ADDED_PROPERTIES:
        assert f'"{name}"' in source


def test_summary_does_not_count_holds_as_gate_rejections(monkeypatch, tmp_path):
    """HOLD는 게이트까지 가지도 않는다 — 거부로 세면 안 된다.

    2026-08-18 리포트가 HOLD 82건을 "게이트 거부 82개"로 적었다. approved=False를
    거부로 셌기 때문인데, 그러면 어떤 룰도 발동한 적 없는 날과 실제로 룰이 82번
    걸린 날이 같은 숫자로 보인다. CLAUDE.md 감시 지표가 정확히 그 구분을 요구한다.
    """
    monkeypatch.setattr(notion_sync.collectors, "fetch_kospi200_ticker_names", lambda: {})

    portfolio = PortfolioState(positions=[], cash_weight=1.0)
    blocks = asyncio.run(
        notion_sync._daily_report_children(
            "2026-08-18",
            decisions_today=[
                {"ticker": "005930", "action": "HOLD", "approved": False, "rejected_by": None, "avg_score": 0.1},
                {"ticker": "000660", "action": "HOLD", "approved": False, "rejected_by": None, "avg_score": 0.2},
                {"ticker": "035420", "action": "BUY", "approved": False, "rejected_by": "position_limit", "avg_score": 0.9},
            ],
            buys=[],
            sells=[],
            skips=[],
            portfolio=portfolio,
        )
    )
    all_text = json.dumps(blocks, ensure_ascii=False)

    assert "총 3개 종목 판단" in all_text
    assert "BUY 승인 0개" in all_text
    assert "게이트 거부 1개" in all_text  # position_limit 하나뿐. HOLD 2건은 아니다
    assert "HOLD 2개" in all_text


# --- 매도 페이지 본문 (발동 산식) + 사유 라벨 ---


def _sell_journal_entry(**overrides) -> dict:
    entry = {
        "event": "sell", "day": "2026-08-18", "ticker": "192820",
        "reason": "take_profit_trail", "reasoning": None,
        "exit_price": 232000.0, "exit_price_source": "fill", "entry_price": 232000.0,
        "realized_pnl_pct": 0.0, "holding_days": 6, "peak_price": 250500.0,
        "take_profit_stage": 3, "position_fraction_sold": 1 / 3,
        "position_fraction_remaining": 2 / 3, "shares_before": 12, "shares_sold": 4,
        "shares_after": 8, "sell_amount": 928000.0, "exit_plan": None,
    }
    entry.update(overrides)
    return entry


def test_deterministic_sell_page_explains_what_triggered_it(monkeypatch):
    """결정론적 매도는 reasoning이 없어서 본문이 늘 비어 있었다 — 왜 팔렸는지가
    사람이 읽는 자리에 아무것도 안 남았다. 발동 산식을 대신 적어준다."""
    blocks = asyncio.run(notion_sync._sell_row_children(_sell_journal_entry()))
    text = json.dumps(blocks, ensure_ascii=False)

    assert "판단 근거" in text
    assert "250,500" in text  # 트레일링 기준이 된 고점
    assert "-7.39%" in text or "-7.3" in text  # 고점 대비 하락률
    assert "232,000" in text  # 진입가
    assert "12주 → 8주" in text


def test_first_take_profit_explains_against_entry_not_peak(monkeypatch):
    """1회차 익절은 고점이 아니라 진입가 대비 익절선으로 발동한다 — 기준이 다르다."""
    blocks = asyncio.run(
        notion_sync._sell_row_children(
            _sell_journal_entry(take_profit_stage=0, exit_price=278400.0, realized_pnl_pct=0.20)
        )
    )
    text = json.dumps(blocks, ensure_ascii=False)

    assert "익절선" in text
    assert "고점" not in text


def test_stop_loss_page_explains_the_threshold(monkeypatch):
    blocks = asyncio.run(
        notion_sync._sell_row_children(
            _sell_journal_entry(
                reason="stop_loss", entry_price=255500.0, exit_price=229709.68,
                realized_pnl_pct=-0.1009, position_fraction_sold=1.0, shares_before=31,
                shares_sold=31, shares_after=0,
            )
        )
    )
    text = json.dumps(blocks, ensure_ascii=False)

    assert "손절선" in text
    assert "전량 매도" in text


def test_partial_fill_note_is_carried_into_the_page(monkeypatch):
    blocks = asyncio.run(
        notion_sync._sell_row_children(_sell_journal_entry(exit_price_source="fill_partial"))
    )
    text = json.dumps(blocks, ensure_ascii=False)
    assert "되세웠다" in text


def test_correction_note_survives_into_the_page(monkeypatch):
    blocks = asyncio.run(
        notion_sync._sell_row_children(_sell_journal_entry(correction_note="※ 테스트 교정 기록"))
    )
    assert "※ 테스트 교정 기록" in json.dumps(blocks, ensure_ascii=False)


def test_breakeven_trailing_exit_is_not_labelled_take_profit():
    """실현 +0.00%인데 '익절'로 적히면 나중에 익절 건만 모아 성과를 볼 때 오염된다."""
    assert notion_sync.sell_reason_label(_sell_journal_entry(realized_pnl_pct=0.0)) == "트레일링청산"
    assert notion_sync.sell_reason_label(_sell_journal_entry(realized_pnl_pct=-0.03)) == "트레일링청산"


def test_profitable_trailing_exit_is_still_take_profit():
    assert notion_sync.sell_reason_label(_sell_journal_entry(realized_pnl_pct=0.0427)) == "익절"


def test_stop_loss_label_is_unaffected():
    assert notion_sync.sell_reason_label(_sell_journal_entry(reason="stop_loss", realized_pnl_pct=-0.10)) == "손절"


def test_legacy_sell_without_trigger_values_does_not_invent_a_threshold():
    """peak_price/take_profit_stage를 남기기 전(2026-08-18 이전)에 쌓인 매도 기록은
    어느 문턱이 발동했는지 알 수 없다. 그때 '익절선 +20%에 도달'이라고 쓰면
    실제로는 +0.00%에 팔린 건이 +20%에 팔린 것처럼 읽힌다 — 지어내지 않는다."""
    entry = _sell_journal_entry()
    del entry["peak_price"]
    del entry["take_profit_stage"]

    text = json.dumps(asyncio.run(notion_sync._sell_row_children(entry)), ensure_ascii=False)

    assert "+0.00%" in text
    assert "익절선" not in text
    assert "트레일링 문턱" not in text
    assert "남아 있지 않다" in text


def test_stop_loss_explanation_says_whether_the_threshold_was_per_ticker():
    """LLM이 정한 문턱으로 잘린 건지 고정 기본값으로 잘린 건지 구분돼야 한다."""
    fixed = json.dumps(
        asyncio.run(notion_sync._sell_row_children(_sell_journal_entry(reason="stop_loss", exit_plan=None))),
        ensure_ascii=False,
    )
    assert "고정 기본 손절선" in fixed

    per_ticker = json.dumps(
        asyncio.run(
            notion_sync._sell_row_children(
                _sell_journal_entry(
                    reason="stop_loss",
                    exit_plan={"stop_loss_pct": -0.06, "take_profit_pct": 0.12,
                               "take_profit_fraction": 0.25, "trail_pct": -0.04},
                )
            )
        ),
        ensure_ascii=False,
    )
    assert "종목별 손절선" in per_ticker
    assert "-6.00%" in per_ticker


# --- 교정된 항목의 갱신 (일지가 바뀌면 노션도 따라간다) ---


def test_corrected_entry_updates_the_existing_page_instead_of_duplicating(monkeypatch, tmp_path):
    """일지는 소급 교정이 실제로 일어나는 파일이다(체결 원본 복원, 비중 기준 변경).
    한 번 올리고 끝내면 노션이 조용히 틀린 값을 들고 있게 되고, 그때마다 일회용
    스크립트로 페이지를 찾아 고치게 된다 — 2026-08까지 그걸 세 번 했다."""
    log_path = tmp_path / "trade_journal.jsonl"
    state_path = tmp_path / "state.json"
    entry = _sell_entry(ticker="036570", day="2026-08-18")
    _write_jsonl(log_path, [entry])

    requests_made = []

    def fake_request(method, path, body=None):
        requests_made.append((method, path))
        if method == "POST" and path == "/pages":
            return {"id": "page-1"}
        if method == "GET":
            return {"results": []}
        return {"ok": True}

    monkeypatch.setattr(notion_sync, "_notion_request", fake_request)

    first = asyncio.run(notion_sync.sync_trade_journal(log_path, "db-1", state_path=state_path))
    assert first == {"synced": 1, "updated": 0, "failed": 0, "skipped": 0}
    assert json.loads(state_path.read_text())["entries"]["sell:036570:2026-08-18"]["page_id"] == "page-1"

    # 바뀐 게 없으면 아무 요청도 안 나간다.
    requests_made.clear()
    second = asyncio.run(notion_sync.sync_trade_journal(log_path, "db-1", state_path=state_path))
    assert second == {"synced": 0, "updated": 0, "failed": 0, "skipped": 1}
    assert requests_made == []

    # 일지를 교정하면 같은 페이지가 갱신된다 — 새 행이 생기면 안 된다.
    entry["exit_price"] = 229709.68
    entry["realized_pnl_pct"] = -0.1009
    _write_jsonl(log_path, [entry])

    requests_made.clear()
    third = asyncio.run(notion_sync.sync_trade_journal(log_path, "db-1", state_path=state_path))

    assert third == {"synced": 0, "updated": 1, "failed": 0, "skipped": 0}
    assert ("POST", "/pages") not in requests_made  # 중복 생성 금지
    assert ("PATCH", "/pages/page-1") in requests_made


def test_legacy_key_without_fingerprint_is_adopted_without_touching_notion(monkeypatch, tmp_path):
    """page_id·지문을 안 남기던 시절의 상태 파일이 있어도, 안 바뀐 항목 때문에
    노션을 훑지 않는다. 지문만 심어두고 다음 변경부터 잡는다."""
    log_path = tmp_path / "trade_journal.jsonl"
    state_path = tmp_path / "state.json"
    _write_jsonl(log_path, [_sell_entry(ticker="192820", day="2026-08-14")])
    state_path.write_text(json.dumps({"synced_keys": ["sell:192820:2026-08-14"]}))

    def fail(*a, **k):
        raise AssertionError("안 바뀐 항목 때문에 노션을 부르면 안 된다")

    monkeypatch.setattr(notion_sync, "_notion_request", fail)

    summary = asyncio.run(notion_sync.sync_trade_journal(log_path, "db-1", state_path=state_path))

    assert summary == {"synced": 0, "updated": 0, "failed": 0, "skipped": 1}
    assert json.loads(state_path.read_text())["entries"]["sell:192820:2026-08-14"]["fingerprint"]


def test_legacy_row_is_found_in_notion_when_it_actually_needs_updating(monkeypatch, tmp_path):
    """page_id가 없는 옛 행이라도, 실제로 교정되면 노션에서 짝을 찾아 갱신한다."""
    log_path = tmp_path / "trade_journal.jsonl"
    state_path = tmp_path / "state.json"
    entry = _sell_entry(ticker="192820", day="2026-08-14")
    _write_jsonl(log_path, [entry])
    state_path.write_text(
        json.dumps({"entries": {"sell:192820:2026-08-14": {"fingerprint": "stale00000000000"}}})
    )

    requests_made = []

    def fake_request(method, path, body=None):
        requests_made.append((method, path))
        if method == "POST" and path.endswith("/query"):
            return {
                "results": [
                    {
                        "id": "legacy-page",
                        "properties": {
                            "구분": {"select": {"name": "매도"}},
                            "티커": {"rich_text": [{"plain_text": "192820"}]},
                            "날짜": {"date": {"start": "2026-08-14"}},
                            "가격": {"number": 200000.0},
                        },
                    }
                ],
                "has_more": False,
            }
        if method == "GET":
            return {"results": []}
        return {"ok": True}

    monkeypatch.setattr(notion_sync, "_notion_request", fake_request)

    summary = asyncio.run(notion_sync.sync_trade_journal(log_path, "db-1", state_path=state_path))

    assert summary == {"synced": 0, "updated": 1, "failed": 0, "skipped": 0}
    assert ("PATCH", "/pages/legacy-page") in requests_made
    assert json.loads(state_path.read_text())["entries"]["sell:192820:2026-08-14"]["page_id"] == "legacy-page"


def test_update_replaces_the_body_instead_of_appending_to_it(monkeypatch, tmp_path):
    """숫자가 교정된 경우 옛 문장과 새 문장이 나란히 남으면 어느 쪽이 맞는지 알 수 없다."""
    log_path = tmp_path / "trade_journal.jsonl"
    state_path = tmp_path / "state.json"
    entry = _sell_entry(ticker="036570", day="2026-08-18")
    _write_jsonl(log_path, [entry])
    state_path.write_text(
        json.dumps({"entries": {"sell:036570:2026-08-18": {"page_id": "p1", "fingerprint": "stale"}}})
    )

    deleted = []

    def fake_request(method, path, body=None):
        if method == "GET":
            return {"results": [{"id": "old-block-1"}, {"id": "old-block-2"}]}
        if method == "DELETE":
            deleted.append(path)
        return {"ok": True}

    monkeypatch.setattr(notion_sync, "_notion_request", fake_request)

    asyncio.run(notion_sync.sync_trade_journal(log_path, "db-1", state_path=state_path))

    assert deleted == ["/blocks/old-block-1", "/blocks/old-block-2"]


# --- 일일 리포트 표 정렬: 최신이 맨 위 (사용자 요청 2026-08-23) ---


def _record_view_requests(monkeypatch, list_result=None):
    """/views 호출만 기록하는 가짜 _notion_request. 나머지 요청은 성공으로 흘린다."""
    calls = []

    def fake_request(method, path, body=None, notion_version=None):
        if path.startswith("/views"):
            calls.append({"method": method, "path": path, "body": body, "version": notion_version})
            if method == "GET":
                return list_result if list_result is not None else {"results": [{"id": "view-1"}]}
        return {"id": "x"}

    monkeypatch.setattr(notion_sync, "_notion_request", fake_request)
    return calls


def test_sort_database_newest_first_sorts_every_view_by_date_descending(monkeypatch):
    calls = _record_view_requests(monkeypatch, {"results": [{"id": "view-1"}, {"id": "view-2"}]})

    assert notion_sync.sort_database_newest_first("db-1") is True

    assert calls[0]["method"] == "GET"
    assert calls[0]["path"] == "/views?database_id=db-1"
    assert [c["path"] for c in calls[1:]] == ["/views/view-1", "/views/view-2"]
    for patch_call in calls[1:]:
        assert patch_call["method"] == "PATCH"
        assert patch_call["body"] == {"sorts": [{"property": "날짜", "direction": "descending"}]}


def test_view_sort_requests_use_the_newer_api_version(monkeypatch):
    """뷰 API는 2025-09-03부터 열렸다 — 모듈 기본값(2022-06-28)으로 보내면 404다."""
    calls = _record_view_requests(monkeypatch)

    notion_sync.sort_database_newest_first("db-1")

    assert calls, "뷰 요청이 아예 나가지 않았다"
    for call in calls:
        assert call["version"] == "2025-09-03"


def test_notion_request_sends_the_version_override_in_the_header(monkeypatch):
    sent = {}

    def fake_request(method, url, headers=None, json=None, timeout=None):
        sent["headers"] = headers
        return _FakeResponse(200, {"ok": True})

    monkeypatch.setattr(notion_sync.requests, "request", fake_request)

    notion_sync._notion_request("GET", "/views?database_id=db-1", None, notion_version="2025-09-03")
    assert sent["headers"]["Notion-Version"] == "2025-09-03"

    notion_sync._notion_request("POST", "/pages", {})
    assert sent["headers"]["Notion-Version"] == notion_sync.NOTION_VERSION


def test_sort_database_newest_first_reports_failure_without_raising(monkeypatch):
    monkeypatch.setattr(notion_sync, "_notion_request", lambda *a, **k: None)
    assert notion_sync.sort_database_newest_first("db-1") is False

    monkeypatch.setattr(notion_sync, "_notion_request", lambda *a, **k: {"results": []})
    assert notion_sync.sort_database_newest_first("db-1") is False


def test_create_daily_report_database_sorts_the_new_table_newest_first(monkeypatch):
    calls = _record_view_requests(monkeypatch)

    assert notion_sync.create_daily_report_database("parent-1") == "x"

    assert [c["method"] for c in calls] == ["GET", "PATCH"]
    assert calls[1]["body"] == {"sorts": [{"property": "날짜", "direction": "descending"}]}


def test_sync_daily_report_fixes_the_view_sort_before_adding_todays_row(monkeypatch, tmp_path):
    """이미 만들어진 DB(정렬 없이 생성된)도 다음 리포트 때 스스로 고쳐진다 —
    EC2에서 손으로 정렬을 걸어두는(=git에 안 남는) 조치가 필요 없게."""
    calls = _record_view_requests(monkeypatch)

    created = asyncio.run(
        notion_sync.sync_daily_report(
            "2026-08-23",
            PortfolioState(),
            "report-db-1",
            pipeline_log_path=tmp_path / "missing.jsonl",
            trade_journal_log_path=tmp_path / "missing2.jsonl",
            state_path=tmp_path / "state.json",
        )
    )

    assert created is True
    assert calls[0]["path"] == "/views?database_id=report-db-1"
    assert calls[1]["body"] == {"sorts": [{"property": "날짜", "direction": "descending"}]}


def test_sync_daily_report_still_written_when_the_view_sort_fails(monkeypatch, tmp_path):
    def fake_request(method, path, body=None, notion_version=None):
        if path.startswith("/views"):
            return None
        return {"id": "report-page-1"}

    monkeypatch.setattr(notion_sync, "_notion_request", fake_request)

    created = asyncio.run(
        notion_sync.sync_daily_report(
            "2026-08-23",
            PortfolioState(),
            "report-db-1",
            pipeline_log_path=tmp_path / "missing.jsonl",
            trade_journal_log_path=tmp_path / "missing2.jsonl",
            state_path=tmp_path / "state.json",
        )
    )

    assert created is True
