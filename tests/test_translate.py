import asyncio

import pytest

from src import translate


@pytest.fixture(autouse=True)
def _claude_api_switched_on(monkeypatch):
    """번역 경로 자체를 검증한다. 꺼진 상태는 맨 아래 테스트가 본다."""
    monkeypatch.setattr(translate.llm, "CLAUDE_API_ENABLED", True)


def test_to_korean_returns_none_and_empty_string_unchanged():
    assert asyncio.run(translate.to_korean(None)) is None
    assert asyncio.run(translate.to_korean("")) == ""


def test_to_korean_returns_translated_text(monkeypatch):
    captured = {}

    async def fake_call_structured(**kwargs):
        captured.update(kwargs)
        return translate._Translation(translated="번역된 문장")

    monkeypatch.setattr(translate.llm, "call_structured", fake_call_structured)

    result = asyncio.run(translate.to_korean("bullish momentum", label="translate_buy_reason"))

    assert result == "번역된 문장"
    assert captured["user"] == "bullish momentum"
    assert captured["label"] == "translate_buy_reason"


def test_to_korean_falls_back_to_original_text_on_failure(monkeypatch):
    async def fake_call_structured(**kwargs):
        raise RuntimeError("boom")

    monkeypatch.setattr(translate.llm, "call_structured", fake_call_structured)

    result = asyncio.run(translate.to_korean("bullish momentum"))

    assert result == "bullish momentum"


def test_switched_off_returns_the_original_without_calling_claude(monkeypatch, caplog):
    """스위치로 끈 것은 실패가 아니라 스택트레이스를 남기지 않는다(노션 일일 리포트가 사유
    수십 개를 번역하려 들면 cron.log가 트레이스로 덮인다)."""
    monkeypatch.setattr(translate.llm, "CLAUDE_API_ENABLED", False)

    async def must_not_be_called(**kwargs):
        raise AssertionError("스위치가 꺼졌는데 번역을 호출했다")

    monkeypatch.setattr(translate.llm, "call_structured", must_not_be_called)

    with caplog.at_level("INFO"):
        assert asyncio.run(translate.to_korean("bullish momentum")) == "bullish momentum"

    assert not any(r.exc_info for r in caplog.records)
