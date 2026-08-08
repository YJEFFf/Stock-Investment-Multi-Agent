from datetime import datetime, timezone

import pytest
from pydantic import ValidationError

from src.schemas import AnalystOpinion, Decision, GateResult


def _opinion(**overrides) -> dict:
    base = dict(
        agent="chart",
        ticker="005930",
        score=0.5,
        confidence=0.8,
        evidence=["prompt:chart@a3f2c1"],
        as_of=datetime.now(timezone.utc),
    )
    base.update(overrides)
    return base


def test_analyst_opinion_valid():
    opinion = AnalystOpinion(**_opinion())
    assert opinion.ticker == "005930"


@pytest.mark.parametrize("field, value", [("score", 1.5), ("score", -1.5), ("confidence", 1.1), ("confidence", -0.1)])
def test_analyst_opinion_rejects_out_of_range(field, value):
    with pytest.raises(ValidationError):
        AnalystOpinion(**_opinion(**{field: value}))


def test_decision_rejects_invalid_action():
    opinion = AnalystOpinion(**_opinion())
    with pytest.raises(ValidationError):
        Decision(ticker="005930", action="SELL", reason="x", inputs=[opinion], degraded=False)


def test_gate_result_allows_none_rejected_by():
    result = GateResult(approved=True, rejected_by=None)
    assert result.approved is True
