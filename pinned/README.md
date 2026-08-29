# `pinned/`

This directory contains the **frozen** configuration and policy files that the
LLM must never be able to modify.

## Invariant

The LLM client is constructed without any environment access to this directory.
The LLM is not given any tool that could write here. A `ruff` custom check
and a build-time test fail the build if any non-`pinned/` file imports the
write-side of these modules.

## Files

| File | Purpose | Owner |
|------|---------|-------|
| `risk_limits.py` | Hard risk ceilings. | human |
| `news_policy.py` | News → risk-policy mapping. | human |
| `promotion_gates.py` | Promotion score weights and thresholds. | human |
| `simulation_constants.py` | Spread, slippage, fees assumptions. | human |
| `strategies/ema_atr_xauusd_h1_v1.json` | Baseline strategy config. | human |
| `holdout/xauusd-h1-holdout-hash.txt` | SHA-256 of the frozen holdout. | human |

## How to make a change

1. Open a PR titled `pinned/<file>: <short rationale>`.
2. Bump the relevant git tag (e.g. `pinned-v0.2.0`).
3. Update the canonical hash in the corresponding `_HASH` constant if the file is hashed.
4. Wait for the human operator to approve.

## What the LLM may NOT do

- Write to any file in this directory.
- Mutate any constant imported from this directory.
- Bypass the `MutableByLLM` allow-list declared in `app/strategy/configs.py`.
- Read `holdout/xauusd-h1-holdout-hash.txt` for anything other than verifying
  the dataset has not been tampered with.
