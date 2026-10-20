import asyncio
from datetime import datetime, timezone

from src.pipeline import (
    execute_simulated,
    make_combined_analyst_fn,
    make_dummy_analyst_fn,
    propose_decision,
    run_day,
)
from src.schemas import AnalystOpinion, PortfolioState, RiskGateConfig

UNIVERSE = [
    ("005930", "반도체"),
    ("000660", "반도체"),
    ("035420", "IT"),
    ("051910", "화학"),
    ("005380", "자동차"),
]


def test_candidate_concurrency_is_bounded_without_dropping_candidates(tmp_path):
    from src.schemas import Decision
    universe = [(f"{i:06}", "sector") for i in range(25)]
    active = 0
    peak = 0
    seen = []
    async def analyst(ticker, sector, day):
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        await asyncio.sleep(0.001)
        seen.append(ticker)
        active -= 1
        return [AnalystOpinion(agent="chart", ticker=ticker, score=0, confidence=1,
                               evidence=["prompt:test@1"], as_of=day)]
    async def judge(opinions, total):
        return Decision(ticker=opinions[0].ticker, action="HOLD", reason="test",
                        inputs=opinions, degraded=False)
    _, results = asyncio.run(run_day(
        universe, datetime(2026, 10, 20, tzinfo=timezone.utc),
        PortfolioState(), RiskGateConfig(), analyst, judge, execute_simulated,
        log_path=tmp_path / "bounded.jsonl"))
    assert peak == 8
    assert sorted(seen) == [t for t, _ in universe]
    assert [d.ticker for d, _ in results] == [t for t, _ in universe]


def test_run_day_is_deterministic(tmp_path):
    day = datetime(2026, 1, 5, tzinfo=timezone.utc)
    config = RiskGateConfig()
    analyst_fn = make_dummy_analyst_fn(base_seed=42)

    _, results_a = asyncio.run(
        run_day(
            UNIVERSE,
            day,
            PortfolioState(),
            config,
            analyst_fn,
            propose_decision,
            execute_simulated,
            log_path=tmp_path / "a.jsonl",
        )
    )
    _, results_b = asyncio.run(
        run_day(
            UNIVERSE,
            day,
            PortfolioState(),
            config,
            analyst_fn,
            propose_decision,
            execute_simulated,
            log_path=tmp_path / "b.jsonl",
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
            execute_simulated,
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
            execute_simulated,
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


def test_each_ticker_judged_without_global_collection_barrier_but_execution_waits(tmp_path):
    from src.schemas import AnalystOpinion, Decision
    async def scenario():
        first_judged=asyncio.Event()
        all_done=asyncio.Event()
        executed=[]
        async def analyst(ticker,sector,day):
            if ticker=='slow':
                await asyncio.wait_for(first_judged.wait(), .5)
                all_done.set()
            return [AnalystOpinion(agent='chart',ticker=ticker,score=.9,confidence=.9,
                                   evidence=['prompt:test@1'],as_of=day)]
        async def judge(opinions,expected):
            if opinions[0].ticker=='fast':first_judged.set()
            return Decision(ticker=opinions[0].ticker,action='HOLD',reason='test',inputs=opinions,degraded=False)
        async def execute(decision,gate,portfolio,sector,weight):
            assert all_done.is_set()
            executed.append(decision.ticker)
            return portfolio
        await run_day([('slow',''),('fast','')],datetime(2026,10,16),PortfolioState(),RiskGateConfig(),
                      analyst,judge,execute,log_path=tmp_path/'pipeline.jsonl')
        assert executed==['slow','fast']
    asyncio.run(scenario())
