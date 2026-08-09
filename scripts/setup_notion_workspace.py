"""노션 워크스페이스 뼈대를 한 번 만드는 스크립트 — 랜딩 페이지에 소개+GitHub
링크 블록 추가, "프로젝트 소개·설계 철학" 페이지 생성, "매매일지" 데이터베이스
생성. 이미 만들어져 있으면(.env에 NOTION_TRADE_JOURNAL_DB_ID가 있으면) 아무것도
안 하고 종료한다 — 재실행해도 중복 페이지가 안 생기게.

사전 준비: .env에 NOTION_API_KEY, NOTION_PARENT_PAGE_ID가 채워져 있어야 한다
(노션에서 페이지를 만들고 integration과 연결한 뒤 그 페이지 ID).

실행: uv run python scripts/setup_notion_workspace.py (레포 루트에서)
"""

import os
import sys
from pathlib import Path

# scripts/를 직접 실행하면 sys.path[0]이 scripts/ 자신이라 `from src import ...`가
# 실패한다 — run_daily.py와 같은 이유로 레포 루트를 명시적으로 추가.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from dotenv import load_dotenv  # noqa: E402

from src import notion_sync  # noqa: E402

load_dotenv()

ENV_PATH = Path(".env")


def _append_env_var(key: str, value: str) -> None:
    with ENV_PATH.open("a") as f:
        f.write(f"\n{key}={value}\n")


def main() -> None:
    if os.environ.get("NOTION_TRADE_JOURNAL_DB_ID"):
        print("이미 설정되어 있습니다. NOTION_TRADE_JOURNAL_DB_ID:", os.environ["NOTION_TRADE_JOURNAL_DB_ID"])
        return

    parent_page_id = os.environ.get("NOTION_PARENT_PAGE_ID")
    if not parent_page_id or not os.environ.get("NOTION_API_KEY"):
        print(".env에 NOTION_API_KEY / NOTION_PARENT_PAGE_ID를 먼저 채워주세요.")
        return

    print("랜딩 페이지에 소개 + GitHub 링크 추가 중...")
    if not notion_sync.add_landing_page_content(parent_page_id):
        print("실패 — NOTION_API_KEY가 올바른지, 페이지가 integration과 연결됐는지 확인하세요.")
        return
    print("완료.")

    print("프로젝트 소개 페이지 생성 중...")
    intro_page_id = notion_sync.create_intro_page(parent_page_id)
    if intro_page_id is None:
        print("실패.")
        return
    print("완료. intro_page_id =", intro_page_id)

    print("매매일지 데이터베이스 생성 중...")
    database_id = notion_sync.create_trade_journal_database(parent_page_id)
    if database_id is None:
        print("실패.")
        return
    print("완료. database_id =", database_id)

    _append_env_var("NOTION_TRADE_JOURNAL_DB_ID", database_id)
    print(".env에 NOTION_TRADE_JOURNAL_DB_ID 저장 완료. 노션 페이지를 열어 확인해보세요.")


if __name__ == "__main__":
    main()
