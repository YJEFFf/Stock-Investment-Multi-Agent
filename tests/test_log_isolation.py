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
import inspect
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


def _default_argument_paths():
    """함수 시그니처에 **정의 시점에 묶인** Path 기본값.

    `def f(log_path: Path = DEFAULT_X)`는 import 시점에 그 순간의 객체를 붙잡는다.
    나중에 `monkeypatch.setattr(module, "DEFAULT_X", tmp)`를 해도 이미 묶인 기본값은
    안 바뀐다 — 격리가 통째로 무효다. 모듈 상수만 보는 위 검사로는 못 잡는다.
    """
    for module in pkgutil.iter_modules(src.__path__):
        loaded = importlib.import_module(f"src.{module.name}")
        for func_name, func in vars(loaded).items():
            if not inspect.isfunction(func) or func.__module__ != loaded.__name__:
                continue
            for param in inspect.signature(func).parameters.values():
                if isinstance(param.default, Path):
                    yield f"src.{module.name}.{func_name}({param.name}=)", param.default


def test_no_function_default_still_points_at_the_repo_logs():
    """2026-09-08에 이 구멍으로 한 건이 더 샜다. 모듈 상수를 전부 등록한 뒤에도
    `pipeline.evaluate_holdings(log_path=DEFAULT_SELL_LOG_PATH)`가 정의 시점 값을
    들고 있어서, 격리를 고친 커밋으로 테스트를 돌리는 순간 운영 매매일지에 한 행이
    또 적혔다. 기본값은 `None`으로 두고 함수 안에서 해석해야 monkeypatch가 닿는다
    (`judgment.log_sell_judgment`가 원래 그렇게 되어 있어서 그쪽만 무사했다)."""
    leaked = sorted(
        qualified
        for qualified, value in _default_argument_paths()
        if REPO_LOGS == value.resolve() or REPO_LOGS in value.resolve().parents
    )

    assert not leaked, (
        "정의 시점에 운영 logs/ 경로가 기본값으로 묶여 있다. "
        f"`Path | None = None`으로 바꾸고 함수 안에서 `x = x or DEFAULT_X`로 해석할 것: {leaked}"
    )
