"""운영 스위치의 현재 값을 테스트로 고정한다.

다시 켤 때는 코드 값·이 테스트·docs/CHANGELOG.md 항목을 **한 커밋에서** 의도적으로 바꾼다.
실수로 True가 되면: Claude API는 다음 08:30에 매수 판단으로 수백 번 과금 호출을 하고,
LLM 재량 매도는 71/71 HOLD였던 판단을 다시 매일 부른다.
"""

import scripts.decide_llm_sell as dls
from src import llm


def test_claude_api_is_switched_off_since_2026_09_14():
    assert llm.CLAUDE_API_ENABLED is False


def test_llm_discretionary_sell_is_stopped_since_2026_09_14():
    assert dls.LLM_SELL_ENABLED is False
