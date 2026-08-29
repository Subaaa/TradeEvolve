# TradeEvolve

A self-improving, **paper-trading** research agent for **XAU/USD (gold spot vs US dollar)** on OANDA v20 fxTrade Practice. Python-only backend; **Supabase** for the data layer; **no frontend** in the MVP.

The LLM is a researcher and hypothesis generator only. Market math, risk, order validation, PnL, and promotion gates are deterministic code outside the LLM. The system is conservative by design: it must never weaken its own safety limits, switch to live trading, or alter the holdout dataset.

## Quick start

```bash
# 1. Install
uv sync --extra dev

# 2. Configure
cp .env.example .env.dev
# edit .env.dev with real keys

# 3. Migrations
uv run supabase db push   # against the configured Supabase project

# 4. Run
uv run python -m app.main
```

The agent process launches an OANDA MCP server as a stdio subprocess and starts an APScheduler-driven cron. The read-only JSON API is at `http://127.0.0.1:8080`.

## Components

See [`docs/architecture.md`](docs/architecture.md) for the full layout.

| Component | Role |
|-----------|------|
| `app/main.py` | Entry point. Boots subprocess, scheduler, FastAPI, signals. |
| `app/scheduler/` | APScheduler jobs. |
| `app/market/` | OANDA v20 client, candle pipeline, indicators, state builder. |
| `app/strategy/` | StrategyRunner + StrategyConfig (Pydantic). |
| `app/risk/` | RiskEngine (deterministic, LLM-unreachable). |
| `app/orders/` | OrderGateway + client extensions. |
| `app/journal/` | Append-only writer with hash chain. |
| `app/llm/` | `LlmClient` over `api.minimax.io`. |
| `app/evolution/` | Backtester, walk-forward, Monte Carlo, shadow, arbiter. |
| `app/reports/` | Daily/weekly reports. |
| `app/api/` | FastAPI read-only JSON API. |
| `app/ops/` | Kill switch, backup, alerts. |
| `app/db/` | DB client, migrations, grants. |
| `services/oanda_mcp/` | Standalone OANDA MCP server (stdio). |
| `pinned/` | Human-maintained, never LLM-written. |

## Tests

```bash
uv run pytest -m unit            # fast
uv run pytest -m integration     # needs a Supabase test schema
uv run pytest -m replay          # recorded fixtures
```

## The `pinned/` invariant

`pinned/` contains constants the LLM must never be able to modify:
risk limits, news policy, promotion gates, simulation constants, the
baseline strategy config, and the holdout hash. A `ruff` lint check
and a build-time test fail the build if any non-`pinned/` file imports
the write-side of these modules.

## Definition of Done

See Section 30 of [`docs/architecture.md`](docs/architecture.md). The MVP is not done when it is "making money" in paper trading.
