import asyncio
import json

import anthropic
import httpx
import pytest
from pydantic import BaseModel

from src import llm


class _DummyModel(BaseModel):
    value: int


_SCHEMA = {
    "type": "object",
    "properties": {"value": {"type": "integer"}},
    "required": ["value"],
    "additionalProperties": False,
}


class _Block:
    def __init__(self, text):
        self.type = "text"
        self.text = text


class _Usage:
    input_tokens = 10
    output_tokens = 5


class _Response:
    def __init__(self, text, stop_reason="end_turn"):
        self.content = [_Block(text)]
        self.usage = _Usage()
        self.stop_reason = stop_reason


def _call(log_path):
    return llm.call_structured(
        system="sys", user="user", response_model=_DummyModel, json_schema=_SCHEMA, log_path=log_path
    )


def test_call_structured_success(monkeypatch, tmp_path):
    async def fake_create(**kwargs):
        return _Response(json.dumps({"value": 42}))

    monkeypatch.setattr(llm._client.messages, "create", fake_create)

    assert asyncio.run(_call(tmp_path / "llm_calls.jsonl")) == _DummyModel(value=42)


def test_call_structured_retries_on_invalid_json_then_succeeds(monkeypatch, tmp_path):
    calls = {"n": 0}

    async def fake_create(**kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            return _Response("not json")
        return _Response(json.dumps({"value": 7}))

    monkeypatch.setattr(llm._client.messages, "create", fake_create)

    assert asyncio.run(_call(tmp_path / "llm_calls.jsonl")) == _DummyModel(value=7)
    assert calls["n"] == 2


def test_call_structured_retries_on_schema_validation_failure(monkeypatch, tmp_path):
    calls = {"n": 0}

    async def fake_create(**kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            return _Response(json.dumps({"value": "not-an-int"}))
        return _Response(json.dumps({"value": 1}))

    monkeypatch.setattr(llm._client.messages, "create", fake_create)

    assert asyncio.run(_call(tmp_path / "llm_calls.jsonl")) == _DummyModel(value=1)
    assert calls["n"] == 2


def test_call_structured_raises_after_exhausting_retries(monkeypatch, tmp_path):
    async def fake_create(**kwargs):
        return _Response("still not json")

    monkeypatch.setattr(llm._client.messages, "create", fake_create)

    with pytest.raises(RuntimeError):
        asyncio.run(_call(tmp_path / "llm_calls.jsonl"))


def test_call_structured_raises_on_refusal(monkeypatch, tmp_path):
    async def fake_create(**kwargs):
        return _Response(json.dumps({"value": 1}), stop_reason="refusal")

    monkeypatch.setattr(llm._client.messages, "create", fake_create)

    with pytest.raises(RuntimeError):
        asyncio.run(_call(tmp_path / "llm_calls.jsonl"))


def test_call_structured_retries_on_transport_error_then_succeeds(monkeypatch, tmp_path):
    calls = {"n": 0}
    request = httpx.Request("POST", "https://api.anthropic.com/v1/messages")

    async def fake_create(**kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            raise anthropic.APIConnectionError(request=request)
        return _Response(json.dumps({"value": 3}))

    monkeypatch.setattr(llm._client.messages, "create", fake_create)

    assert asyncio.run(_call(tmp_path / "llm_calls.jsonl")) == _DummyModel(value=3)
    assert calls["n"] == 2


def test_call_structured_logs_success_with_label(monkeypatch, tmp_path):
    async def fake_create(**kwargs):
        return _Response(json.dumps({"value": 1}))

    monkeypatch.setattr(llm._client.messages, "create", fake_create)
    log_path = tmp_path / "llm_calls.jsonl"

    asyncio.run(
        llm.call_structured(
            system="sys",
            user="user",
            response_model=_DummyModel,
            json_schema=_SCHEMA,
            label="chart",
            log_path=log_path,
        )
    )

    entries = [json.loads(line) for line in log_path.read_text().splitlines()]
    assert len(entries) == 1
    assert entries[0]["label"] == "chart"
    assert entries[0]["success"] is True
    assert entries[0]["input_tokens"] == 10
    assert entries[0]["output_tokens"] == 5


def test_call_structured_logs_failure_with_label(monkeypatch, tmp_path):
    async def fake_create(**kwargs):
        return _Response("still not json")

    monkeypatch.setattr(llm._client.messages, "create", fake_create)
    log_path = tmp_path / "llm_calls.jsonl"

    with pytest.raises(RuntimeError):
        asyncio.run(
            llm.call_structured(
                system="sys",
                user="user",
                response_model=_DummyModel,
                json_schema=_SCHEMA,
                label="news",
                log_path=log_path,
            )
        )

    entries = [json.loads(line) for line in log_path.read_text().splitlines()]
    assert len(entries) == 1
    assert entries[0]["label"] == "news"
    assert entries[0]["success"] is False


def test_call_log_day_is_kst_not_utc(monkeypatch, tmp_path):
    """08:30 KST 매수 판단 cron은 UTC로 전날 23:30이다 — timestamp만 있으면
    하루 호출의 대부분이 전날 몫으로 집계된다(2026-08-20 실측: 파일상 35건).
    day는 반드시 KST 기준이어야 CLAUDE.md 감시 지표가 맞는 숫자를 낸다."""
    from datetime import datetime, timezone

    log_path = tmp_path / "llm_calls.jsonl"

    class _FixedDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            # 2026-08-19 23:35 UTC == 2026-08-20 08:35 KST (아침 매수 판단 시각)
            utc = datetime(2026, 8, 19, 23, 35, tzinfo=timezone.utc)
            return utc if tz is None else utc.astimezone(tz)

    monkeypatch.setattr(llm, "datetime", _FixedDatetime)

    async def fake_create(**kwargs):
        return _Response(json.dumps({"value": 1}))

    monkeypatch.setattr(llm._client.messages, "create", fake_create)

    asyncio.run(_call(log_path))

    entry = json.loads(log_path.read_text().strip())
    assert entry["timestamp"].startswith("2026-08-19T23:35")  # 순간은 UTC 그대로
    assert entry["day"] == "2026-08-20"  # 집계 단위는 장이 도는 날짜
