"""운영 `logs/` 오염 방지 자체를 검증한다.

`conftest._isolate_default_state_paths`는 `Path("logs/...")` 기본값을 tmp로 돌린다.
문제는 **새 기본값을 만들고 거기 등록하는 걸 잊는 것**이고, 그러면 테스트가 조용히
운영 기록에 가짜 줄을 쓴다. 2026-08-20(알림 마커), 08-27(sell_judgment,
observed_range), 09-08(매도 로그·매매일지 — 노션까지 동기화됨)까지 네 번 났고
매번 사람이 나중에 눈치채서 되돌렸다.

그 등록 누락을 사람이 아니라 코드가 잡는다. 새 기본값을 추가하면 등록하기 전까지
이 테스트가 깨진다.
"""

import importlib
import pkgutil
from pathlib import Path

import src

REPO_LOGS = (Path(__file__).resolve().parent.parent / "logs").resolve()


def _module_level_paths():
    for module in pkgutil.iter_modules(src.__path__):
        loaded = importlib.import_module(f"src.{module.name}")
        for name, value in vars(loaded).items():
            if isinstance(value, Path) and not name.startswith("_"):
                yield f"src.{module.name}.{name}", value


def test_no_default_log_path_survives_into_the_repo():
    leaked = sorted(
        qualified
        for qualified, value in _module_level_paths()
        if REPO_LOGS == value.resolve() or REPO_LOGS in value.resolve().parents
    )

    assert not leaked, (
        "운영 logs/를 가리키는 기본 경로가 테스트 중에 살아 있다. "
        f"conftest._isolate_default_state_paths에 등록할 것: {leaked}"
    )
