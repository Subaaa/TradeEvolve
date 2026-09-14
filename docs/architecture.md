# Architecture

This document is the full architecture for the TradeEvolve
agent. It is the canonical reference for "why is it this way". The
implementation plan in `plan.md` and the `AGENTS.md` operating manual
are the in-flight references.

## Goals and non-goals

The agent is a self-improving, **paper-trading** research tool for
**XAU/USD** (traded as **PAXG/USD** on Alpaca paper trading). The MVP is a single
asset, a single timeframe, one explainable baseline strategy, one
champion + one challenger, paper trading only, no leverage, daily
review, weekly evolution, one bounded mutation per week.

The MVP does **not** include live trading, a frontend, multi-asset,
sub-hour timeframes, leverage, or any backtest UI. The "Definition of
Done" in `plan.md` Section 30 is the source of truth for what the MVP
is and is not.

## Topology

A single VPS runs one Python process under `systemd` (or one Docker
container). The process holds:

- the **APScheduler** in-process cron (all jobs in `app/scheduler/jobs/`)
- the **FastAPI** read-only JSON API on `127.0.0.1:8080`
- the Alpaca **MCP subprocess** driver (the subprocess holds the Alpaca
  API key/secret; the parent never sees them)
- the deterministic **StrategyRunner** and **RiskEngine**
- the in-process **Backtester**
- the append-only **Journal** writer

The process connects over TLS to **Supabase Postgres** for state. Two
DB roles are used: a service role for reads and non-journal writes,
and a `journal_writer` role for `INSERT`-only on the journal tables.

There is no Redis, no separate worker containers, no event streaming,
no vector database, no frontend, no microservices.

## The LLM is a researcher

The `LlmClient` is constructed with only the `minimax_api_key`. It has
no access to Alpaca credentials, DB URLs, or any other secret. The LLM is
called in five places, each with a strict Pydantic input and output
schema:

1. `classify_news` — when a new high-impact news item arrives.
2. `review_trade` — when a trade closes.
3. `daily_commentary` — at 00:10 UTC daily.
4. `weekly_review` — at 22:00 UTC on Sunday.
5. `reflect_failure_pattern` — when the Reviewer surfaces a recurring
   pattern.

There is **no LLM call in the trading loop**. The `StrategyRunner`
and `RiskEngine` are pure Python functions of their inputs.

## The `pinned/` invariant

`pinned/` is the set of files the LLM cannot modify. A build-time
test (`test_pinned_invariant.py`) fails the build if any non-`pinned/`
module imports a non-allow-listed symbol from `pinned/`. The
`LIVE_TRADING_ENABLED` constant in `pinned/simulation_constants.py` is
`False`; the Alpaca MCP subprocess refuses to connect to anything but a
paper-trading base URL.

## The four-phase promotion loop

A candidate challenger must pass four phases before it can be
auto-promoted to champion:

1. **Walk-forward validation** — N folds of training/test splits.
2. **Holdout** — single run on the frozen holdout dataset (whose hash
   is pinned; the LLM never sees the dataset).
3. **Monte Carlo** — 1,000 random order reshuffles; the candidate's
   actual PnL must be in the 80th percentile of the distribution.
4. **Shadow paper-trading** — the candidate runs in parallel with the
   champion for at least 240 hours and 30 trades; its shadow metrics
   must beat the champion's live metrics over the same window.

Each phase writes a journal entry. The `PromotionArbiter` is the only
code path that may write `final_decision = 'promoted'`. It is a pure
function of the phase results; it never reads LLM output.

## Failure handling

See `plan.md` Section 22 for the full table. The key invariants:

- The journal hash chain is verified hourly; a mismatch writes a
  `system_event` and an alert.
- The kill switch is a row in `system_state`; the live loop checks
  it every tick.
- Duplicate orders are prevented by `client_tag`, passed as Alpaca's
  `client_order_id`; Alpaca rejects re-submission of the same id.

## The Alpaca MCP server

Lives in `services/alpaca_mcp/`. Built on the official `mcp` Python
SDK v2.x. Seven tools: five read-only, two write (gated by
`ALPACA_MCP_WRITE_ENABLED`, and by `AlpacaClient` refusing to connect to
a non-paper base URL). The subprocess holds the Alpaca API key/secret;
the parent process communicates over stdio.

## Why this is enough to be worth building

- The LLM is rate-limited, schema-validated, and cannot bypass the
  risk engine.
- The risk engine applies unchanged to every strategy. The LLM can
  propose a strategy change; the risk engine then validates the
  resulting trade. The PromotionArbiter explicitly checks for
  risk-engine circumvention.
- The holdout is hash-pinned and never shown to the LLM.
- All decisions are journaled. Every non-action (rejection, skip,
  no-signal) is a row.
- The system is honest about its limitations: it is a research tool,
  not a product.
