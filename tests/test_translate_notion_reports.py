import json

import pytest

from scripts import translate_notion_reports as repair


def block(kind, text, id="b"):
    return {"id": id, "type": kind, kind: {"rich_text": [{"type": "text", "text": {"content": text}}]}}


def test_selects_only_reasons_and_debate_leaves_financial_sections_alone():
    page = {"id": "p", "kind": "daily", "blocks": [
        block("heading_2", "오늘의 판단 요약"),
        block("bulleted_list_item", "종목: BUY(승인) (avg_score=0.5) — Strong chart signal"),
        block("bulleted_list_item", "종목: BUY(승인) — RSI 지표가 상승함"),
        block("heading_2", "총정리"),
        block("paragraph", "Do not translate this"),
    ]}
    found = list(repair.candidates(page))
    assert len(found) == 1
    assert found[0]["source"] == "Strong chart signal"
    assert found[0]["prefix"] == "종목: BUY(승인) (avg_score=0.5) — "
    page["kind"] = "buy"
    page["blocks"] = [block("heading_3", "토론 논거"),
                      block("bulleted_list_item", "[bull] (강도 0.50) Strong chart signal")]
    assert list(repair.candidates(page))[0]["prefix"] == "[bull] (강도 0.50) "


def test_apply_preserves_other_fields_and_can_resume(monkeypatch, tmp_path):
    original = block("paragraph", "Strong chart signal")
    item = {"block_id": "b", "kind": "paragraph", "before": original["paragraph"]["rich_text"],
            "after": repair.notion_sync._rich_text("강한 차트 신호")}
    path = tmp_path / "plan.json"
    path.write_text(json.dumps([item]))
    calls = []
    def fake(method, path, body=None):
        calls.append((method, path, body))
        if method == "PATCH":
            assert body == {"paragraph": {"rich_text": item["after"]}}
            original["paragraph"]["rich_text"] = item["after"]
        return original
    monkeypatch.setattr(repair, "request", fake)
    repair.apply(path)
    repair.apply(path)
    assert sum(c[0] == "PATCH" for c in calls) == 1


def test_apply_stops_when_source_was_edited(monkeypatch, tmp_path):
    original = block("paragraph", "Strong chart signal")
    path = tmp_path / "plan.json"
    path.write_text(json.dumps([{"block_id": "b", "kind": "paragraph",
                                 "before": original["paragraph"]["rich_text"],
                                 "after": repair.notion_sync._rich_text("강한 차트 신호")}]))
    def fake(method, path, body=None):
        assert method == "GET"
        return block("paragraph", "Manually edited text")
    monkeypatch.setattr(repair, "request", fake)
    with pytest.raises(ValueError, match="source changed"):
        repair.apply(path)
