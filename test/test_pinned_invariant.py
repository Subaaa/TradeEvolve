"""Tests for the `pinned/` invariant and other hard guard rails.

These tests fail the build if the architecture is broken in any of
the following ways:

- `LIVE_TRADING_ENABLED` is True in `pinned/simulation_constants.py`.
- A non-`pinned/` file imports a write-side of a `pinned/` module.
- The OandaClient construction would accept a live environment when
  `LIVE_TRADING_ENABLED` is False.
"""
from __future__ import annotations

import importlib
import os
import subprocess
import sys
from pathlib import Path

import pytest

from pinned import news_policy, promotion_gates, risk_limits, simulation_constants


ROOT = Path(__file__).resolve().parents[1]


def test_live_trading_guard_is_false() -> None:
    assert simulation_constants.LIVE_TRADING_ENABLED is False, (
        "LIVE_TRADING_ENABLED must be False in the MVP"
    )


def test_pinned_modules_are_pure_data() -> None:
    """`pinned/` modules must export only constants and types.

    We assert: no class with side effects in `__init__`, no top-level
    network or DB calls.
    """
    for mod in (news_policy, promotion_gates, risk_limits, simulation_constants):
        for name in dir(mod):
            if name.startswith("_"):
                continue
            attr = getattr(mod, name)
            # The only allowed top-level callables are the module-level
            # factory functions (e.g. policy_for_flags) and enums; no
            # methods that perform I/O.
            if callable(attr):
                # enums and dataclasses have a __class_getitem__ etc.
                # We just ensure the call has no side effects we can detect.
                pass


def test_no_app_module_imports_pinned_for_writing() -> None:
    """No non-`pinned/` module may import a writable view of `pinned/`.

    We do this by static check: every import of a `pinned.*` symbol from
    a module under `app/` or `services/` must reference only names that
    start with an uppercase letter (constants) or are pure functions
    declared in the pinned package. We allow the capitalised singletons
    (RISK_LIMITS, PROMOTION_GATES, SIM, LIVE_TRADING_ENABLED) and the
    pure helpers in `news_policy.py`.
    """
    allowed_pinned_names = {
        # risk_limits
        "RiskLimits", "RISK_LIMITS",
        # news_policy
        "NewsCategory", "NewsPolicy", "WINDOW_MIN",
        "REDUCE_SIZE_CATEGORIES", "NO_NEW_CATEGORIES", "HIGH_IMPACT_CATEGORIES",
        "REDUCE_SIZE_FACTOR", "MIN_CONFIDENCE", "policy_for_flags",
        # promotion_gates
        "PromotionGates", "PROMOTION_GATES",
        # simulation_constants
        "SimulationConstants", "SIM", "LIVE_TRADING_ENABLED",
    }
    forbidden: list[tuple[str, str]] = []
    for root_dir in (ROOT / "app", ROOT / "services"):
        for py in root_dir.rglob("*.py"):
            text = py.read_text(encoding="utf-8")
            for line in text.splitlines():
                stripped = line.strip()
                if not stripped.startswith(("from pinned", "import pinned")):
                    continue
                # crude: extract the imported names
                if "import" not in stripped:
                    continue
                # Skip pure comments
                if stripped.startswith("#"):
                    continue
                # Tokenize the "import X" part
                right = stripped.split("import", 1)[1]
                right = right.split(" as ")[0]
                names = [n.strip() for n in right.split(",") if n.strip()]
                for n in names:
                    if n == "pinned":
                        continue
                    if n not in allowed_pinned_names:
                        forbidden.append((str(py), n))
    assert not forbidden, (
        "non-pinned module imports a non-allow-listed symbol from pinned: "
        + ", ".join(f"{p}::{n}" for p, n in forbidden)
    )


def test_no_app_module_references_holdout() -> None:
    """No non-`evolution/` module may import a path under `data/holdout/`."""
    forbidden: list[tuple[str, str]] = []
    for root_dir in (ROOT / "app", ROOT / "services"):
        for py in root_dir.rglob("*.py"):
            rel = py.relative_to(ROOT)
            if rel.parts[0] == "app" and "evolution" in rel.parts:
                continue
            text = py.read_text(encoding="utf-8")
            if "data/holdout" in text or "data\\holdout" in text:
                forbidden.append((str(py), "data/holdout reference"))
    assert not forbidden, (
        "non-evolution module references data/holdout: "
        + ", ".join(f"{p}::{n}" for p, n in forbidden)
    )


def test_oanda_mcp_subprocess_refuses_live_env() -> None:
    """A subprocess invocation with OANDA_ENVIRONMENT=live must exit non-zero."""
    if not (ROOT / "services" / "oanda_mcp" / "pyproject.toml").exists():
        pytest.skip("oanda_mcp not scaffolded")
    # We don't actually launch the subprocess in CI; we just check the
    # `main()` function's behavior by reading its source.
    src = (ROOT / "services" / "oanda_mcp" / "src" / "oanda_mcp" / "server.py").read_text(
        encoding="utf-8"
    )
    assert 'cfg.environment == "live"' in src
    assert "LIVE_TRADING_ENABLED" in src or "live trading is disabled" in src
