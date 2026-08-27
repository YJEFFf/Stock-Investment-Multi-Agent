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
def _isolate_default_state_paths(monkeypatch, tmp_path):
    """호출부가 경로를 안 넘겼을 때 쓰이는 기본 상태/로그 경로를 전부 tmp로 돌린다.

    _isolate_alert_state와 같은 이유이고, 같은 사고가 2026-08-27 하루에만 두 번 더
    났다 — `sell_judgment.jsonl`과 `observed_range.json`이 각각 테스트 한 번에
    운영 `logs/`로 새어 나갔다. 알림 마커(2026-08-20)까지 세 번째다.

    **패턴이 분명하다: `Path("logs/...")` 기본값을 새로 만들면 여기 등록해야 한다.**
    등록을 잊으면 조용히 운영 기록이 오염되고, 그 기록들은 하나같이 "사후에 무슨
    일이 있었는지 보려고" 만든 것이라 가짜 줄이 섞이는 순간 목적을 잃는다.
    개별 테스트가 명시적으로 경로를 넘기는 건 이 픽스처와 무관하게 그대로 동작한다.
    """
    from src import judgment, pipeline

    monkeypatch.setattr(judgment, "DEFAULT_SELL_JUDGMENT_LOG_PATH", tmp_path / "sell_judgment.jsonl")
    monkeypatch.setattr(pipeline, "DEFAULT_OBSERVED_RANGE_PATH", tmp_path / "observed_range.json")
