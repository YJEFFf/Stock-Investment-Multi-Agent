import asyncio
from datetime import datetime, timezone

from src.pipeline import make_combined_analyst_fn, make_dummy_analyst_fn, propose_decision, run_day
from src.schemas import AnalystOpinion, PortfolioState, RiskGateConfig

UNIVERSE = [
    ("005930", "반도체"),
    ("000660", "반도체"),
    ("035420", "IT"),
    ("051910", "화학"),
    ("005380", "자동차"),
]


def test_run_day_is_deterministic(tmp_path):
    day = datetime(2026, 1, 5, tzinfo=timezone.utc)
    config = RiskGateConfig()
    analyst_fn = make_dummy_analyst_fn(base_seed=42)

    _, results_a = asyncio.run(
        run_day(
            UNIVERSE, day, PortfolioState(), config, analyst_fn, propose_decision, log_path=tmp_path / "a.jsonl"
        )
    )
    _, results_b = asyncio.run(
        run_day(
            UNIVERSE, day, PortfolioState(), config, analyst_fn, propose_decision, log_path=tmp_path / "b.jsonl"
        )
    )

    assert [d.model_dump() for d, _ in results_a] == [d.model_dump() for d, _ in results_b]
    assert [g.model_dump() for _, g in results_a] == [g.model_dump() for _, g in results_b]


def test_different_seed_can_change_results(tmp_path):
    day = datetime(2026, 1, 5, tzinfo=timezone.utc)
    config = RiskGateConfig()

    _, results_a = asyncio.run(
        run_day(
            UNIVERSE,
            day,
            PortfolioState(),
            config,
            make_dummy_analyst_fn(base_seed=1),
            propose_decision,
            log_path=tmp_path / "a.jsonl",
        )
    )
    _, results_b = asyncio.run(
        run_day(
            UNIVERSE,
            day,
            PortfolioState(),
            config,
            make_dummy_analyst_fn(base_seed=2),
            propose_decision,
            log_path=tmp_path / "b.jsonl",
        )
    )

    scores_a = [o.score for d, _ in results_a for o in d.inputs]
    scores_b = [o.score for d, _ in results_b for o in d.inputs]
    assert scores_a != scores_b


def test_make_combined_analyst_fn_survives_partial_failure():
    day = datetime(2026, 1, 5, tzinfo=timezone.utc)

    async def working_fn(ticker, sector, day):
        return [AnalystOpinion(agent="a", ticker=ticker, score=0.5, confidence=0.5, evidence=["e"], as_of=day)]

    async def failing_fn(ticker, sector, day):
        raise RuntimeError("boom")

    combined = make_combined_analyst_fn([working_fn, failing_fn])

    opinions = asyncio.run(combined("005930", "반도체", day))

    assert len(opinions) == 1
    assert opinions[0].agent == "a"


def test_make_combined_analyst_fn_merges_all_successes():
    day = datetime(2026, 1, 5, tzinfo=timezone.utc)

    async def fn_a(ticker, sector, day):
        return [AnalystOpinion(agent="a", ticker=ticker, score=0.5, confidence=0.5, evidence=["e"], as_of=day)]

    async def fn_b(ticker, sector, day):
        return [AnalystOpinion(agent="b", ticker=ticker, score=-0.2, confidence=0.4, evidence=["e"], as_of=day)]

    combined = make_combined_analyst_fn([fn_a, fn_b])

    opinions = asyncio.run(combined("005930", "반도체", day))

    assert {o.agent for o in opinions} == {"a", "b"}
