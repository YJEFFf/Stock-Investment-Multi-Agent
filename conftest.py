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


@pytest.fixture(autouse=True)
def _isolate_alert_state(monkeypatch, tmp_path):
    """알림 상태 파일(하루 1회 마커 + 시세 공백 구간)을 항상 tmp로 돌린다.

    기본값은 레포의 logs/alert_markers다. 막지 않으면 테스트가 운영 상태를
    덮어쓴다 — EC2에서 테스트를 한 번 돌리면 그날 진짜 장애 알림이 "이미 보냈다"로
    묻히고, 열려 있던 공백 구간도 지워진다. tests/test_evaluate_holdings.py에만
    있던 방어를 여기로 올려 파일을 새로 만들 때 빠뜨릴 수 없게 했다.
    """
    from src import notify

    monkeypatch.setattr(notify, "ALERT_MARKER_DIR", tmp_path / "alert_markers")
    monkeypatch.setattr(notify, "BLACKOUT_STATE_DIR", tmp_path / "alert_markers")


@pytest.fixture(autouse=True)
def _isolate_sell_judgment_log(monkeypatch, tmp_path):
    """LLM 재량 매도의 종목별 판단 로그를 항상 tmp로 돌린다.

    _isolate_alert_state와 같은 이유다 — 기본값이 레포의 logs/sell_judgment.jsonl이라
    막지 않으면 테스트 한 번에 가짜 판단 수십 건이 운영 기록에 섞인다. 이 로그는
    "왜 안 팔았는가"를 사후에 보려고 만든 것이라(judgment.py 주석), 목킹된 줄이
    섞이는 순간 목적을 잃는다.
    """
    from src import judgment

    monkeypatch.setattr(judgment, "DEFAULT_SELL_JUDGMENT_LOG_PATH", tmp_path / "sell_judgment.jsonl")
