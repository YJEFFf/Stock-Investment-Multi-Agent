from datetime import datetime, timezone

from src.pipeline import check_gate
from src.schemas import AnalystOpinion, Decision, PortfolioState, Position, RiskGateConfig

CONFIG = RiskGateConfig()  # docs/PLAN.md §5 확정값: 15% / 40% / -5% / 100%


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
    result = check_gate(decision, PortfolioState(), CONFIG, sector="반도체", trade_weight=0.08)
    assert result.approved is False
    assert result.rejected_by is None


def test_passes_when_within_all_limits():
    result = check_gate(_buy_decision(), PortfolioState(), CONFIG, sector="반도체", trade_weight=0.08)
    assert result.approved is True
    assert result.rejected_by is None


def test_position_limit_rejects_when_exceeded():
    portfolio = PortfolioState(
        positions=[Position(ticker="005930", sector="반도체", weight=0.10)],
        cash_weight=0.90,
    )
    # 기존 0.10 + 신규 0.08 = 0.18 > 15% 한도
    result = check_gate(_buy_decision("005930"), portfolio, CONFIG, sector="반도체", trade_weight=0.08)
    assert result.approved is False
    assert result.rejected_by == "position_limit"


def test_sector_concentration_rejects_when_exceeded():
    portfolio = PortfolioState(
        positions=[
            Position(ticker="000660", sector="반도체", weight=0.15),
            Position(ticker="005935", sector="반도체", weight=0.15),
            Position(ticker="357780", sector="반도체", weight=0.05),
        ],
        cash_weight=0.65,
    )
    # 반도체 섹터 합산 0.35 + 신규 0.08 = 0.43 > 40% 한도 (개별 종목 한도는 통과)
    result = check_gate(_buy_decision("005930"), portfolio, CONFIG, sector="반도체", trade_weight=0.08)
    assert result.approved is False
    assert result.rejected_by == "sector_concentration"


def test_daily_loss_limit_blocks_new_buys():
    portfolio = PortfolioState(daily_pnl_pct=-0.06)
    result = check_gate(_buy_decision(), portfolio, CONFIG, sector="반도체", trade_weight=0.08)
    assert result.approved is False
    assert result.rejected_by == "daily_loss_limit"


def test_daily_loss_limit_boundary_is_inclusive():
    portfolio = PortfolioState(daily_pnl_pct=-0.05)
    result = check_gate(_buy_decision(), portfolio, CONFIG, sector="반도체", trade_weight=0.08)
    assert result.approved is False
    assert result.rejected_by == "daily_loss_limit"


def test_total_exposure_rejects_when_exceeded():
    tight_config = RiskGateConfig(total_exposure_limit=0.20)
    portfolio = PortfolioState(
        positions=[Position(ticker="000660", sector="반도체", weight=0.15)],
        cash_weight=0.85,
    )
    # 총 투자 비중 0.15 + 신규 0.08 = 0.23 > 20% 한도, 개별/섹터 한도는 통과
    result = check_gate(_buy_decision("005930"), portfolio, tight_config, sector="화학", trade_weight=0.08)
    assert result.approved is False
    assert result.rejected_by == "total_exposure"
