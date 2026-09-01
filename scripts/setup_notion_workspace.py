"""노션 워크스페이스 뼈대를 한 번 만드는 스크립트 — 랜딩 페이지에 소개+GitHub
링크 블록 추가, "프로젝트 소개·설계 철학" 페이지 생성, "매매일지"·"일일 리포트"
데이터베이스 생성. 각 산출물은 .env에 대응하는 ID가 이미 있으면 건너뛴다 —
재실행해도 중복 페이지가 안 생기고, 나중에 새 산출물이 추가돼도(이번에 일일
리포트가 그랬다) 이미 설정된 나머지를 건드리지 않고 빠진 것만 채운다.

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
    parent_page_id = os.environ.get("NOTION_PARENT_PAGE_ID")
    if not parent_page_id or not os.environ.get("NOTION_API_KEY"):
        print(".env에 NOTION_API_KEY / NOTION_PARENT_PAGE_ID를 먼저 채워주세요.")
        return

    existing_journal_db_id = os.environ.get("NOTION_TRADE_JOURNAL_DB_ID")
    if existing_journal_db_id:
        print("랜딩/소개/매매일지는 이미 설정됨 — 건너뜀.")
        # DB 생성은 건너뛰지만 나중에 추가된 속성은 채워 넣어야 한다 — 없는 속성에
        # 값을 쓰면 노션이 400을 내서 매도 동기화가 통째로 실패한다.
        # 목록을 여기 적지 않는다 — 속성이 늘 때마다 이 문구가 낡는다.
        # 실제 목록은 notion_sync._TRADE_JOURNAL_ADDED_PROPERTIES 하나뿐이다.
        print(
            "매매일지 속성 최신화 중: "
            + ", ".join(notion_sync._TRADE_JOURNAL_ADDED_PROPERTIES)
        )
        if notion_sync.ensure_trade_journal_properties(existing_journal_db_id):
            print("완료.")
        else:
            print("실패 — 노션에서 매매일지 DB가 integration과 연결됐는지 확인하세요.")
    else:
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
        _append_env_var("NOTION_INTRO_PAGE_ID", intro_page_id)

        print("매매일지 데이터베이스 생성 중...")
        database_id = notion_sync.create_trade_journal_database(parent_page_id)
        if database_id is None:
            print("실패.")
            return
        print("완료. database_id =", database_id)
        _append_env_var("NOTION_TRADE_JOURNAL_DB_ID", database_id)

    if os.environ.get("NOTION_DAILY_REPORT_DB_ID"):
        print("일일 리포트 데이터베이스는 이미 설정됨 — 건너뜀.")
    else:
        print("일일 리포트 데이터베이스 생성 중...")
        report_db_id = notion_sync.create_daily_report_database(parent_page_id)
        if report_db_id is None:
            print("실패.")
            return
        print("완료. database_id =", report_db_id)
        _append_env_var("NOTION_DAILY_REPORT_DB_ID", report_db_id)

    print("설정 완료. 노션 페이지를 열어 확인해보세요.")


if __name__ == "__main__":
    main()
