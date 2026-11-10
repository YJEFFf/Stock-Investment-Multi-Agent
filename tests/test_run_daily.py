import asyncio
import pytest
from scripts import run_daily
from src import operating_mode


def test_retired_entrypoint_cannot_run_models_or_orders(monkeypatch):
    monkeypatch.setattr(operating_mode, "EXIT_ONLY", False)
    with pytest.raises(RuntimeError, match="retired"):
        asyncio.run(run_daily.main())


def test_retired_entrypoint_is_noop_in_exit_only(monkeypatch):
    monkeypatch.setattr(operating_mode, "EXIT_ONLY", True)
    asyncio.run(run_daily.main())
