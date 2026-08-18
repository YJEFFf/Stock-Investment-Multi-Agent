import pytest

from src import kis


@pytest.fixture(autouse=True)
def _no_fill_polling_wait(monkeypatch):
    """kis.fill_after_order가 테스트에서 실제로 대기하지 않게 한다.

    이 함수는 주문 수량이 다 체결될 때까지 누적 집계를 다시 조회하며 기다린다
    (2026-08-18 부분 체결 누락 대응). 실제 값 그대로면 체결이 안 잡히는 목킹
    경로마다 12초씩 멈춘다. 타임아웃 0이면 조회는 정확히 한 번만 돌아서 폴링
    도입 전과 호출 횟수가 같아진다 — 기존 목이 그대로 유효하다.

    폴링 자체의 동작은 타임아웃을 명시로 넘기는 tests/test_kis_fill.py에서 검증한다.
    """
    monkeypatch.setattr(kis, "FILL_POLL_TIMEOUT_S", 0.0)
    monkeypatch.setattr(kis, "FILL_POLL_INTERVAL_S", 0.0)
