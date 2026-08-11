"""logs/portfolio_state.json에 대한 유일한 읽기/쓰기 창구.

원래 scripts/run_daily.py에 있던 로드/저장 로직을 여기로 옮겼다 — 이제 하루에
여러 스크립트(장 시작 집행, 1분 손절 체크, ...)가 같은 파일을 건드리므로 두
가지가 필요해졌다: 쓰기 도중에 다른 프로세스가 읽어서 깨진 JSON을 보는 걸 막는
원자적 쓰기, 그리고 읽고-고치고-쓰는 구간 전체를 직렬화하는 락. 로직이 하나뿐일
땐 run_daily.py 안에 있어도 됐지만, 여러 진입점이 같은 이유(상태 파일 하나를
안전하게 공유)로 이 코드를 필요로 하게 되면서 분리했다.
"""

import fcntl
import os
from contextlib import contextmanager
from pathlib import Path

from src.schemas import PortfolioState

PORTFOLIO_STATE_PATH = Path("logs/portfolio_state.json")
PORTFOLIO_LOCK_PATH = Path("logs/portfolio_state.lock")


def load_portfolio(path: Path | None = None) -> PortfolioState:
    # 기본값을 함수 시그니처에 바로 안 두는 이유: 그렇게 하면 모듈 로드 시점의
    # PORTFOLIO_STATE_PATH 값이 함수 객체에 고정돼서, 테스트가
    # monkeypatch.setattr(portfolio_store, "PORTFOLIO_STATE_PATH", ...)로 바꿔도
    # 이미 정의된 함수의 기본값엔 반영되지 않는다. 본문에서 매번 전역을 찾게 하면
    # (late binding) 몽키패치가 실제로 먹는다.
    path = path or PORTFOLIO_STATE_PATH
    if path.exists():
        return PortfolioState.model_validate_json(path.read_text())
    return PortfolioState()


def save_portfolio(portfolio: PortfolioState, path: Path | None = None) -> None:
    """임시 파일에 쓰고 os.replace로 교체한다 — 같은 이름으로 직접 덮어쓰면 쓰는
    도중에(특히 지금처럼 하루에도 여러 프로세스가 이 파일을 읽는 상황에서) 다른
    프로세스가 절반만 쓰인 JSON을 읽을 수 있다. os.replace는 같은 파일시스템
    안에서 원자적이라 그 틈이 없다."""
    path = path or PORTFOLIO_STATE_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_suffix(path.suffix + ".tmp")
    tmp_path.write_text(portfolio.model_dump_json(indent=2))
    os.replace(tmp_path, path)


@contextmanager
def portfolio_lock(lock_path: Path | None = None):
    """logs/portfolio_state.json의 읽고-고치고-쓰는 구간을 감싸는 락.

    하루 여러 스크립트(장 시작 집행이 하루 한 번, 손절 체크가 1분마다)가 같은
    상태 파일을 건드리게 되면서 필요해졌다 — 락 없이 두 프로세스가 동시에
    읽고-고치고-쓰면 나중에 쓴 쪽이 먼저 쓴 쪽의 변경을 덮어써 버릴 수 있다.
    fcntl.flock은 블로킹이라 락을 못 잡으면 그냥 기다린다(재시도 로직 불필요)."""
    lock_path = lock_path or PORTFOLIO_LOCK_PATH
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("w") as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(f, fcntl.LOCK_UN)
