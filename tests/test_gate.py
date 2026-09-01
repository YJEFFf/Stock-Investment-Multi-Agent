from datetime import datetime, timezone

from src.pipeline import check_gate
from src.schemas import AnalystOpinion, Decision, PortfolioState, Position, RiskGateConfig

CONFIG = RiskGateConfig()  # docs/PLAN.md §5: 종목당 15% / 총 노출 100%


def _buy_decision(ticker: str = "005930") -> Decision:
    opinion = AnalystOpinion(
        agent="dummy",
        ticker=ticker,
        score=0.9,
        confidence=0.9,
        evidence=["prompt:dummy@m1"],
        as_of=datetime.now(timezone.utc),
    )
    return Decision(ticker=ticker, action="BUY", reason="test", inputs=[opinion], degraded=False)


def test_hold_never_gated():
    decision = Decision(
        ticker="005930",
        action="HOLD",
        reason="test",
        inputs=[],
        degraded=False,
    )
    result = check_gate(decision, PortfolioState(), CONFIG, trade_weight=0.08)
    assert result.approved is False
    assert result.rejected_by is None


def test_passes_when_within_all_limits():
    result = check_gate(_buy_decision(), PortfolioState(), CONFIG, trade_weight=0.08)
    assert result.approved is True
    assert result.rejected_by is None


def test_position_limit_rejects_when_exceeded():
    portfolio = PortfolioState(
        positions=[Position(ticker="005930", sector="반도체", weight=0.10)],
        cash_weight=0.90,
    )
    # 기존 0.10 + 신규 0.08 = 0.18 > 15% 한도
    result = check_gate(_buy_decision("005930"), portfolio, CONFIG, trade_weight=0.08)
    assert result.approved is False
    assert result.rejected_by == "position_limit"


def test_sector_concentration_is_no_longer_gated():
    """섹터 집중도 한도(40%)는 2026-09-01에 **의도적으로** 폐기했다(사용자 확정).

    옛 한도로는 거부됐을 상황을 그대로 두고 통과를 단언한다 — 폐기가 실수로
    되돌려지면 여기서 깨진다. 폐기 근거는 pipeline.check_gate docstring에 있다
    (특정 섹터가 한동안 계속 오르는 국면에서 40% 상한이 수익을 제한한다).
    """
    portfolio = PortfolioState(
        positions=[
            Position(ticker="000660", sector="반도체", weight=0.15),
            Position(ticker="005935", sector="반도체", weight=0.15),
            Position(ticker="357780", sector="반도체", weight=0.05),
        ],
        cash_weight=0.65,
    )
    # 반도체 합산 0.35 + 신규 0.08 = 0.43. 옛 40% 한도였다면 거부됐을 자리다.
    result = check_gate(_buy_decision("005930"), portfolio, CONFIG, trade_weight=0.08)
    assert result.approved is True
    assert result.rejected_by is None


def test_risk_gate_config_has_no_sector_field():
    """폐기한 룰은 값도 남기지 않는다 — 설정에 숫자가 보이면 게이트가 아직 그걸
    본다고 읽힌다(daily_loss_limit을 걷어낼 때와 같은 이유)."""
    assert "sector_concentration_limit" not in RiskGateConfig.model_fields


def test_total_exposure_rejects_when_exceeded():
    tight_config = RiskGateConfig(total_exposure_limit=0.20)
    portfolio = PortfolioState(
        positions=[Position(ticker="000660", sector="반도체", weight=0.15)],
        cash_weight=0.85,
    )
    # 총 투자 비중 0.15 + 신규 0.08 = 0.23 > 20% 한도, 종목당 한도는 통과
    result = check_gate(_buy_decision("005930"), portfolio, tight_config, trade_weight=0.08)
    assert result.approved is False
    assert result.rejected_by == "total_exposure"
