# AGENTS.md — Operating manual for coding agents working in this repo

> Read this before touching any file. The repository is more constrained than it looks.

## Hard invariants

1. **The LLM never modifies `pinned/`.** Files in `pinned/` are imported by the rest of the app. Adding a new mutable field to a `pinned/` config requires a human PR.
2. **`LIVE_TRADING_ENABLED = False` is a hard constant.** Live trading is a Phase 7+ change, gated on a separate sign-off process. The OANDA client factory in `services/oanda_mcp/oanda_client.py` must reject any construction that would result in a live client.
3. **The journal is append-only at the DB level.** No `UPDATE` or `DELETE` on `journal_entries`, `trades`, `risk_events`, or `audit_log`. Two Postgres roles enforce this: the service role for non-journal writes; a `journal_writer` role for `INSERT`-only on the journal tables.
4. **The RiskEngine is a pure function.** It takes a `Proposal` and returns a `Decision`. No network, no I/O, no LLM. If a change would add any of these, the change is wrong.
5. **The LLM is never given the holdout dataset.** `data/holdout/` and `pinned/holdout/` are not imported by any non-evolution module. A lint rule and a build-time test enforce this.
6. **The PromotionArbiter applies fixed gates.** It never reads LLM output. The gates live in `pinned/promotion_gates.py`.
7. **One bounded hypothesis per week.** The system never accepts more.

## Conventions

- **Type everything.** `mypy --strict` is on. If you can't type it, you can't ship it.
- **Pydantic v2 for all data shapes.** Dataclasses are reserved for pure internal value objects where validation is overkill.
- **`match` for strategy state machines.** The StrategyRunner is a state machine; use `match`.
- **Pure functions get property-based tests with `hypothesis`.** Indicators, RiskEngine, Backtester, and the hash chain all have `hypothesis` tests.
- **No comments that explain "what" — only "why".** Code should be self-documenting; comments are for tradeoffs, gotchas, and references.
- **One file per concern.** If a file mixes concerns, split it.
- **Tests live in `test/`.** Unit tests are pure. Integration tests use a real Supabase test schema (CI provisions one). Replay tests use recorded fixtures.

## Running things

```bash
# Install
uv sync --extra dev

# Lint
uv run ruff check .
uv run ruff format --check .
uv run mypy .

# Tests
uv run pytest -m unit
uv run pytest -m unit --co -q | head   # see what's covered

# CLI
uv run python -m app.cli backtest --config pinned/strategies/ema_atr_xauusd_h1_v1.json
uv run python -m app.cli journal --limit 20
uv run python -m app.cli promote-dry-run --experiment-id 42

# Run the agent
uv run python -m app.main
```

## Adding a new indicator

1. Write a pure function in `app/market/indicators/<name>.py` taking a `pd.Series` and returning a `pd.Series` (or a dict of series for multi-line indicators).
2. Add a property-based test in `test/unit/market/indicators/test_<name>.py`.
3. Wire it into `MarketStateBuilder` if the StrategyRunner needs it.

## Adding a new LLM call site

1. Add a Pydantic input and output model in `app/llm/schemas.py`.
2. Add the site name to the `Literal` in `LlmClient.call`.
3. Add the token budget to `app/llm/budgets.py`.
4. Add a prompt template in `app/llm/prompts/<site>.py`.
5. Wire it in the relevant job.

## What NOT to do

- Don't add a new dependency without justification. The current set is curated.
- Don't add a new service, container, or external process. One Python process + one MCP subprocess is the boundary.
- Don't add a "quick fix" that bypasses the journal. Everything that affects state goes through the journal.
- Don't add LLM calls to the trading loop. The StrategyRunner and RiskEngine are deterministic; that's the point.
- Don't add a frontend. The MVP exposes a JSON API and a CLI; that is the entire human interface.
