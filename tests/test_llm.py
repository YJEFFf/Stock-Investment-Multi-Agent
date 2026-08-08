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


def _call():
    return llm.call_structured(system="sys", user="user", response_model=_DummyModel, json_schema=_SCHEMA)


def test_call_structured_success(monkeypatch):
    async def fake_create(**kwargs):
        return _Response(json.dumps({"value": 42}))

    monkeypatch.setattr(llm._client.messages, "create", fake_create)

    assert asyncio.run(_call()) == _DummyModel(value=42)


def test_call_structured_retries_on_invalid_json_then_succeeds(monkeypatch):
    calls = {"n": 0}

    async def fake_create(**kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            return _Response("not json")
        return _Response(json.dumps({"value": 7}))

    monkeypatch.setattr(llm._client.messages, "create", fake_create)

    assert asyncio.run(_call()) == _DummyModel(value=7)
    assert calls["n"] == 2


def test_call_structured_retries_on_schema_validation_failure(monkeypatch):
    calls = {"n": 0}

    async def fake_create(**kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            return _Response(json.dumps({"value": "not-an-int"}))
        return _Response(json.dumps({"value": 1}))

    monkeypatch.setattr(llm._client.messages, "create", fake_create)

    assert asyncio.run(_call()) == _DummyModel(value=1)
    assert calls["n"] == 2


def test_call_structured_raises_after_exhausting_retries(monkeypatch):
    async def fake_create(**kwargs):
        return _Response("still not json")

    monkeypatch.setattr(llm._client.messages, "create", fake_create)

    with pytest.raises(RuntimeError):
        asyncio.run(_call())


def test_call_structured_raises_on_refusal(monkeypatch):
    async def fake_create(**kwargs):
        return _Response(json.dumps({"value": 1}), stop_reason="refusal")

    monkeypatch.setattr(llm._client.messages, "create", fake_create)

    with pytest.raises(RuntimeError):
        asyncio.run(_call())


def test_call_structured_retries_on_transport_error_then_succeeds(monkeypatch):
    calls = {"n": 0}
    request = httpx.Request("POST", "https://api.anthropic.com/v1/messages")

    async def fake_create(**kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            raise anthropic.APIConnectionError(request=request)
        return _Response(json.dumps({"value": 3}))

    monkeypatch.setattr(llm._client.messages, "create", fake_create)

    assert asyncio.run(_call()) == _DummyModel(value=3)
    assert calls["n"] == 2
