"""The `pinned/` invariant and the other hard guard rails.

These tests fail the build if the architecture is broken in any of the
following ways:

* `LIVE_TRADING_ENABLED` is no longer False;
* a non-`pinned/` module imports something from `pinned/` that is not on the
  allow-list, which is how a mutable view of the risk limits would appear;
* the broker client would accept a non-paper base URL;
* the LLM gains a path to a field that is not in the mutation allow-list;
* the append-only journal triggers are missing from the schema.
"""
from __future__ import annotations

import dataclasses
import re
from pathlib import Path

import pytest

from pinned import promotion_gates, risk_limits, simulation_constants

pytestmark = pytest.mark.unit

ROOT = Path(__file__).resolve().parents[1]
APP = ROOT / "app"


def test_live_trading_is_off() -> None:
    assert simulation_constants.LIVE_TRADING_ENABLED is False


def test_the_pinned_singletons_are_frozen() -> None:
    """A frozen dataclass is what makes `RISK_LIMITS` a constant, not a variable."""
    with pytest.raises(dataclasses.FrozenInstanceError):
        risk_limits.RISK_LIMITS.max_daily_loss_pct = 0.99  # type: ignore[misc]
    with pytest.raises(dataclasses.FrozenInstanceError):
        promotion_gates.PROMOTION_GATES.min_relative_perf = 0.0  # type: ignore[misc]


def test_no_app_module_imports_a_non_allow_listed_pinned_symbol() -> None:
    allowed = {
        "RiskLimits",
        "RISK_LIMITS",
        "PromotionGates",
        "PROMOTION_GATES",
        "SimulationConstants",
        "SIM",
        "LIVE_TRADING_ENABLED",
    }
    forbidden: list[str] = []
    for py in APP.rglob("*.py"):
        for line in py.read_text(encoding="utf-8").splitlines():
            stripped = line.strip()
            if not stripped.startswith(("from pinned", "import pinned")):
                continue
            right = stripped.split("import", 1)[1].split(" as ")[0]
            for name in (n.strip() for n in right.split(",")):
                if name and name != "pinned" and name not in allowed:
                    forbidden.append(f"{py.relative_to(ROOT)}::{name}")
    assert not forbidden, "non-allow-listed pinned import: " + ", ".join(forbidden)


def test_nothing_in_app_writes_to_a_pinned_module() -> None:
    """An assignment onto a pinned singleton would defeat the whole design."""
    pattern = re.compile(r"\b(RISK_LIMITS|PROMOTION_GATES|SIM)\s*\.\s*\w+\s*=[^=]")
    offenders = [
        str(py.relative_to(ROOT))
        for py in APP.rglob("*.py")
        if pattern.search(py.read_text(encoding="utf-8"))
    ]
    assert not offenders, f"module assigns to a pinned constant: {offenders}"


def test_the_broker_client_refuses_a_live_base_url() -> None:
    src = (APP / "broker" / "alpaca.py").read_text(encoding="utf-8")
    assert '"paper-api" not in base_url' in src
    assert "LIVE_TRADING_ENABLED" in src


def test_the_broker_client_actually_raises_on_a_live_url() -> None:
    from app.broker.alpaca import AlpacaClient

    with pytest.raises(PermissionError, match="paper"):
        AlpacaClient(
            api_key_id="k",
            api_secret_key="s",
            base_url="https://api.alpaca.markets",
            data_url="https://data.alpaca.markets",
            symbol="PAXG/USD",
        )


def test_writes_are_refused_unless_explicitly_enabled() -> None:
    from app.broker.alpaca import AlpacaClient

    client = AlpacaClient(
        api_key_id="k",
        api_secret_key="s",
        base_url="https://paper-api.alpaca.markets",
        data_url="https://data.alpaca.markets",
        symbol="PAXG/USD",
        write_enabled=False,
    )
    try:
        with pytest.raises(PermissionError, match="disabled"):
            client.cancel_order("abc")
    finally:
        client.close()


def test_the_llm_cannot_reach_a_risk_limit() -> None:
    """The mutation allow-list must not overlap the risk or gate field names."""
    from app.strategy.configs import MUTABLE_BY_LLM, StrategyConfig

    risk_fields = set(vars(risk_limits.RISK_LIMITS))
    gate_fields = set(vars(promotion_gates.PROMOTION_GATES))
    assert not (MUTABLE_BY_LLM & risk_fields)
    assert not (MUTABLE_BY_LLM & gate_fields)
    # And every mutable name must really be a config field.
    assert set(StrategyConfig.model_fields) >= MUTABLE_BY_LLM


def test_the_llm_cannot_enable_shorting_or_relax_the_cost_floor() -> None:
    from app.strategy.configs import MUTABLE_BY_LLM

    for guarded in ("long_only", "min_atr_bps", "atr_percentile_max", "granularity"):
        assert guarded not in MUTABLE_BY_LLM


def test_the_journal_triggers_exist_in_the_schema() -> None:
    sql = (APP / "db" / "migrations" / "0001_init.sql").read_text(encoding="utf-8")
    assert "journal_no_update" in sql
    assert "journal_no_delete" in sql
    assert "trades_no_delete" in sql
    assert "trades_immutable_once_closed" in sql


def test_there_is_no_llm_call_in_the_trading_path() -> None:
    """The tick, the strategy, the risk engine and the executor stay deterministic."""
    trading_path = [
        APP / "strategy" / "runner.py",
        APP / "risk" / "engine.py",
        APP / "risk" / "account_state.py",
        APP / "execution" / "position_manager.py",
        APP / "scheduler" / "jobs" / "tick_bar_close.py",
        APP / "scheduler" / "jobs" / "tick_monitor.py",
    ]
    for py in trading_path:
        text = py.read_text(encoding="utf-8")
        assert "app.llm" not in text, f"{py.name} reaches into the LLM layer"
        assert "anthropic" not in text.lower(), f"{py.name} references the LLM provider"


def test_the_dead_broker_and_database_stacks_are_gone() -> None:
    """The MCP subprocess and Supabase are removed, not merely unused."""
    banned = ("mcp_client", "supabase", "psycopg", "sqlalchemy", "minimax", "openai")
    offenders: list[str] = []
    for py in APP.rglob("*.py"):
        lowered = py.read_text(encoding="utf-8").lower()
        offenders += [
            f"{py.relative_to(ROOT)}::{word}" for word in banned if word in lowered
        ]
    assert not offenders, f"removed stack still referenced: {offenders}"


def test_the_pinned_strategy_file_matches_the_config_model() -> None:
    from app.config import get_settings
    from app.strategy.configs import LLM_RANGES, MUTABLE_BY_LLM, StrategyConfig

    path = get_settings().resolved_champion_config()
    config = StrategyConfig.from_json_file(path)
    assert config.granularity == "M15"
    assert config.long_only is True

    import json

    raw = json.loads(path.read_text(encoding="utf-8"))
    assert set(raw["mutable_by_llm"]) == set(MUTABLE_BY_LLM)
    for name, (low, high) in LLM_RANGES.items():
        assert raw["llm_ranges"][name] == [low, high]
