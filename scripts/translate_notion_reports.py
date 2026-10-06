"""기존 노션 매수 근거만 번역한다. snapshot → prepare → apply, 원본 백업 필수.

각 페이지/블록 ID로 수정하며 원문이 달라지면 중지한다. 매매 기록·판단 재실행 없음.
"""
import argparse
import asyncio
import json
import os
import re
import sys
from pathlib import Path
from urllib.parse import urlencode

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from src import notion_sync, translate  # noqa: E402


def request(method, path, body=None):
    result = notion_sync._notion_request(method, path, body)
    if result is None:
        raise RuntimeError(f"Notion request failed: {method} {path}")
    return result


def pages(database):
    cursor = None
    while True:
        body = {"page_size": 100}
        if cursor:
            body["start_cursor"] = cursor
        response = request("POST", f"/databases/{database}/query", body)
        yield from response["results"]
        if not response["has_more"]:
            break
        cursor = response["next_cursor"]


def blocks(page_id):
    cursor = None
    while True:
        query = {"page_size": 100}
        if cursor:
            query["start_cursor"] = cursor
        response = request("GET", f"/blocks/{page_id}/children?{urlencode(query)}")
        yield from response["results"]
        if not response["has_more"]:
            break
        cursor = response["next_cursor"]


def plain(block):
    return "".join(r.get("plain_text", r.get("text", {}).get("content", ""))
                   for r in block.get(block["type"], {}).get("rich_text", []))


def candidates(page):
    section = ""
    for block in page["blocks"]:
        kind = block["type"]
        text = plain(block)
        if kind.startswith("heading_"):
            section = text
            continue
        prefix, source = "", ""
        if page["kind"] == "daily" and section == "오늘의 판단 요약" and " — " in text:
            prefix, source = text.split(" — ", 1)
            prefix += " — "
        elif page["kind"] == "buy" and section == "매니저 최종 판단" and kind == "paragraph":
            source = text
        elif page["kind"] == "buy" and section == "토론 논거" and kind == "bulleted_list_item":
            match = re.match(r"(\[[^]]+\] \(강도 [^)]+\) )(.*)", text, re.S)
            if match:
                prefix, source = match.groups()
        # 한국어 안의 RSI/MACD 같은 약어는 대상이 아니다. 영어 산문이 남은 혼합 문장도 잡는다.
        if re.search(r"[A-Za-z]{2,}\s+[A-Za-z]{2,}", source):
            yield {"page_id": page["id"], "block_id": block["id"], "kind": kind,
                   "prefix": prefix, "source": source, "before": block[kind]["rich_text"]}


def save(path, value):
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(json.dumps(value, ensure_ascii=False, indent=2))
    temp.replace(path)


def snapshot(path):
    if path.exists():
        raise ValueError("snapshot exists; use a new path to preserve the original backup")
    result = []
    for kind, name in (("daily", "NOTION_DAILY_REPORT_DB_ID"), ("buy", "NOTION_TRADE_JOURNAL_DB_ID")):
        for page in pages(os.environ[name]):
            if kind == "buy" and (page["properties"].get("구분", {}).get("select") or {}).get("name") != "매수":
                continue
            result.append({"kind": kind, "id": page["id"], "url": page.get("url"),
                           "properties": page["properties"], "blocks": list(blocks(page["id"]))})
    save(path, result)
    items = [item for page in result for item in candidates(page)]
    print(json.dumps({"pages": len(result), "english_blocks": len(items),
                      "affected_pages": len({i['page_id'] for i in items})}))


async def prepare(snapshot_path, plan_path):
    original = [item for page in json.loads(snapshot_path.read_text()) for item in candidates(page)]
    plan = json.loads(plan_path.read_text()) if plan_path.exists() else original
    if [{k: v for k, v in item.items() if k != "after"} for item in plan] != original:
        raise ValueError("plan does not match snapshot")
    # 작은 동시성으로 번역만 실행한다. 분석가/게이트/주문에는 접근하지 않는다.
    semaphore = asyncio.Semaphore(4)

    async def one(item):
        if "after" in item:
            return
        async with semaphore:
            translated = await translate.to_korean(item["source"], label="translate_notion_backfill")
        if translated == item["source"] or not re.search(r"[가-힣]", translated or ""):
            raise ValueError(f"translation unavailable: {item['block_id']}")
        item["after"] = notion_sync._rich_text(item["prefix"] + translated)
        save(plan_path, plan)
        print(f"prepared {sum('after' in i for i in plan)}/{len(plan)}", flush=True)

    results = await asyncio.gather(*(one(item) for item in plan), return_exceptions=True)
    failures = [str(r) for r in results if isinstance(r, Exception)]
    if failures:
        raise RuntimeError(f"Incomplete plan: {failures}")


def apply(plan_path):
    plan = json.loads(plan_path.read_text())
    if not all("after" in item for item in plan):
        raise ValueError("plan is not completely translated")
    for index, item in enumerate(plan):
        live = request("GET", f"/blocks/{item['block_id']}")
        if live.get("archived") or live.get("in_trash") or live["type"] != item["kind"]:
            raise ValueError(f"block changed: {item['block_id']}")
        current = live[item["kind"]]["rich_text"]
        expected_text = "".join(r["text"]["content"] for r in item["after"])
        if plain(live) == expected_text:
            continue  # 이전 실행에서 이미 적용·검증된 블록
        if current != item["before"]:
            raise ValueError(f"source changed: {item['block_id']}")
        request("PATCH", f"/blocks/{item['block_id']}", {item["kind"]: {"rich_text": item["after"]}})
        verified = request("GET", f"/blocks/{item['block_id']}")
        if plain(verified) != expected_text:
            raise ValueError(f"verification failed: {item['block_id']}")
        print(f"applied {index + 1}/{len(plan)}", flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=["snapshot", "prepare", "apply"])
    parser.add_argument("--snapshot", type=Path, required=True)
    parser.add_argument("--plan", type=Path)
    args = parser.parse_args()
    if args.mode == "snapshot":
        snapshot(args.snapshot)
    elif args.plan is None:
        parser.error("--plan is required")
    elif args.mode == "prepare":
        asyncio.run(prepare(args.snapshot, args.plan))
    else:
        # 원본 백업과 계획의 연결을 적용 직전에도 확인한다.
        original = [item for page in json.loads(args.snapshot.read_text()) for item in candidates(page)]
        plan = json.loads(args.plan.read_text())
        if [{k: v for k, v in item.items() if k != "after"} for item in plan] != original:
            raise ValueError("plan does not match snapshot")
        apply(args.plan)


if __name__ == "__main__":
    main()
