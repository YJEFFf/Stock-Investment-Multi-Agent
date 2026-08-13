import asyncio

from src import translate


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
