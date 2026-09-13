"""드릴 시나리오를 테스트마다 돌린다 — 주문·게이트 경로가 조용히 깨지면 여기서 먼저 깨진다."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest

import scripts.drill_order_paths as drill


@pytest.mark.parametrize("scenario", drill.SCENARIOS, ids=lambda s: s.__name__)
def test_drill_scenario(scenario):
    outcome = scenario()
    assert outcome.passed, f"{outcome.name}: {outcome.observed}"


def test_the_drill_never_talks_to_the_real_broker():
    """토큰 발급 POST가 오면 FakeBroker가 즉시 실패시킨다 — 모든 시나리오가 끝난 뒤에도
    운영 모듈의 HTTP·토큰 함수가 원래대로 돌아와 있어야 한다."""
    from src import kis

    before = (kis.requests.get, kis.requests.post, kis.get_access_token)
    drill.run_all()
    assert (kis.requests.get, kis.requests.post, kis.get_access_token) == before
