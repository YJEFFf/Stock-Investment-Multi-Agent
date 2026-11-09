"""User-selected operating mode; changing it requires an explicit user decision."""

# 2026-11-09: manage only the existing holdings with recorded hypothetical sales.
# This remains enabled even if the simulation state is missing or damaged.
EXIT_ONLY = True


def require_broker_mode() -> None:
    if EXIT_ONLY:
        raise RuntimeError("exit_only: new analysis and broker account/order operations are disabled")
