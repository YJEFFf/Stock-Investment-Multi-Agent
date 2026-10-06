import asyncio

import pytest
from pydantic import ValidationError

from src import llm, translate


def test_empty_and_korean_do_not_call_codex(monkeypatch):
    async def forbidden(**kwargs):
        raise AssertionError("unexpected call")
    monkeypatch.setattr(translate.codex_plan, "call_structured", forbidden)
    for text in (None, "", "수익성이 개선되어 매수합니다."):
        assert asyncio.run(translate.to_korean(text)) == text


def test_translates_with_claude_disabled_and_reuses_cached_result(monkeypatch):
    captured = []
    monkeypatch.setattr(llm, "CLAUDE_API_ENABLED", False)

    async def fake(**kwargs):
        captured.append(kwargs)
        return translate._Translation(translated="상승 모멘텀")
    monkeypatch.setattr(translate.codex_plan, "call_structured", fake)
    assert asyncio.run(translate.to_korean("bullish momentum", label="translate_buy_reason")) == "상승 모멘텀"
    assert asyncio.run(translate.to_korean("bullish momentum", label="translate_daily_report_reason")) == "상승 모멘텀"
    assert len(captured) == 1
    assert captured[0]["user"] == "bullish momentum"
    assert captured[0]["label"] == "translate_buy_reason"
    assert captured[0]["timeout_s"] == 30


def test_failure_keeps_original_and_does_not_cache(monkeypatch):
    calls = []
    async def fake(**kwargs):
        calls.append(kwargs)
        raise RuntimeError("unavailable")
    monkeypatch.setattr(translate.codex_plan, "call_structured", fake)
    for _ in range(2):
        assert asyncio.run(translate.to_korean("bullish momentum")) == "bullish momentum"
    assert len(calls) == 2
    assert not translate.DEFAULT_CACHE_DIR.exists()


def test_invalid_translation_is_rejected():
    for text in ("", "   ", "still English"):
        with pytest.raises(ValidationError):
            translate._Translation(translated=text)


def test_prompt_change_and_corrupt_cache_trigger_new_translation(monkeypatch, tmp_path):
    prompt = tmp_path / "prompt.md"
    prompt.write_text("first prompt")
    monkeypatch.setattr(translate, "PROMPT_PATH", prompt)
    calls = []
    async def fake(**kwargs):
        calls.append(kwargs)
        return translate._Translation(translated="번역 결과")
    monkeypatch.setattr(translate.codex_plan, "call_structured", fake)
    asyncio.run(translate.to_korean("English reason"))
    next(translate.DEFAULT_CACHE_DIR.glob("*.json")).write_text("partial")
    asyncio.run(translate.to_korean("English reason"))
    prompt.write_text("second prompt")
    asyncio.run(translate.to_korean("English reason"))
    assert len(calls) == 3
