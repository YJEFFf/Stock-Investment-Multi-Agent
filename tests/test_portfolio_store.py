"""src/portfolio_store.py — 로드/저장(원자적 쓰기)과 락 직렬화를 검증한다.

여러 스크립트(execute_open.py 하루 한 번, check_stop_loss.py 1분마다)가 이제
같은 portfolio_state.json을 건드리므로, 이 파일이 실제로 안전한지가 이번
스케줄 분리의 전제조건이다.
"""

import multiprocessing
import time

import pytest

from src import portfolio_store
from src.schemas import PortfolioState, Position


def test_load_portfolio_returns_default_when_no_file(tmp_path):
    path = tmp_path / "does_not_exist.json"
    assert portfolio_store.load_portfolio(path) == PortfolioState()


def test_save_and_load_portfolio_roundtrip(tmp_path):
    path = tmp_path / "nested" / "portfolio_state.json"
    portfolio = PortfolioState(cash_weight=0.7, positions=[Position(ticker="005930", sector="반도체", weight=0.3)])

    portfolio_store.save_portfolio(portfolio, path)

    assert portfolio_store.load_portfolio(path) == portfolio


def test_save_uses_atomic_replace_no_leftover_tmp_file(tmp_path):
    path = tmp_path / "portfolio_state.json"
    portfolio_store.save_portfolio(PortfolioState(cash_weight=0.5), path)

    assert path.exists()
    assert not path.with_suffix(path.suffix + ".tmp").exists()


def test_default_path_is_late_bound_for_monkeypatching(monkeypatch, tmp_path):
    """load_portfolio()/save_portfolio()를 인자 없이 호출해도 monkeypatch로 바꾼
    PORTFOLIO_STATE_PATH를 실제로 따라가야 한다 — 함수 정의 시점에 기본값이
    고정되는 흔한 함정(디폴트 인자 바인딩)이 없어야 execute_open.py/
    check_stop_loss.py 같은 호출부가 인자 없이 불러도 테스트에서 격리된다."""
    patched_path = tmp_path / "portfolio_state.json"
    monkeypatch.setattr(portfolio_store, "PORTFOLIO_STATE_PATH", patched_path)

    portfolio = PortfolioState(cash_weight=0.42)
    portfolio_store.save_portfolio(portfolio)

    assert patched_path.exists()
    assert portfolio_store.load_portfolio() == portfolio


def _increment_under_lock(lock_path, counter_path, delay):
    from src import portfolio_store as ps

    with ps.portfolio_lock(lock_path):
        current = int(counter_path.read_text())
        time.sleep(delay)  # 락이 없으면 이 사이에 다른 프로세스가 끼어들 수 있다
        counter_path.write_text(str(current + 1))


def test_portfolio_lock_serializes_concurrent_writers(tmp_path):
    """락 없이 두 프로세스가 동시에 읽고-고치고-쓰면 나중에 쓴 쪽이 먼저 쓴 쪽의
    변경을 덮어써 최종값이 1로 남는 레이스가 생긴다. 락이 실제로 직렬화한다면
    두 증가가 모두 반영되어 최종값은 2여야 한다."""
    lock_path = tmp_path / "portfolio_state.lock"
    counter_path = tmp_path / "counter.txt"
    counter_path.write_text("0")

    p1 = multiprocessing.Process(target=_increment_under_lock, args=(lock_path, counter_path, 0.3))
    p2 = multiprocessing.Process(target=_increment_under_lock, args=(lock_path, counter_path, 0.0))
    p1.start()
    time.sleep(0.05)  # p1이 먼저 락을 잡도록 보장
    p2.start()
    p1.join(timeout=5)
    p2.join(timeout=5)

    assert int(counter_path.read_text()) == 2
