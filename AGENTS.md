# AGENTS.md — Operating manual for coding agents working in this repo

> Read this before touching any file. The repository is more constrained than it looks.

## Hard invariants

1. **The LLM never modifies `pinned/`.** Files in `pinned/` are imported by the rest of the app. Adding a new mutable field to a `pinned/` config requires a human change.
2. **`LIVE_TRADING_ENABLED = False` is a hard constant.** `AlpacaClient` in `app/broker/alpaca.py` refuses to construct against a base URL that is not a paper-trading URL, and refuses every write unless `write_enabled` was passed explicitly.
3. **The journal is append-only at the database level.** Two SQLite triggers abort any `UPDATE` or `DELETE` on `journal_entries`. A third makes a closed trade immutable except for its `review` column. Do not add a code path that tries to work around them.
4. **The RiskEngine is a pure function.** It takes a `Proposal` and an `AccountState` and returns a `Decision`. No network, no I/O, no LLM. If a change would add any of these, the change is wrong.
5. **Every position has a broker-resident stop.** `PositionManager.open` places a `stop_limit` immediately after the entry fills and flattens the position if it cannot. A position that is unprotected for longer than one monitor cycle is a bug, not a tradeoff.
6. **The discipline counters are derived, never accumulated.** `build_from_db` rebuilds them from the `trades` table on every tick. Do not replace this with an in-memory counter: the previous design did exactly that, and the daily-loss, weekly-loss and cooldown gates were all silently dead as a result.
7. **The PromotionArbiter applies fixed gates** from `pinned/promotion_gates.py` and never reads LLM output.
8. **One bounded hypothesis per week.** The system never accepts more, and a field outside `MUTABLE_BY_LLM` (or a value outside `LLM_RANGES`) is refused three separate times: by the output schema, by `challenger_builder`, and by the config's own validators.

## Architecture in one paragraph

One Python process. It holds an APScheduler cron, a read-only FastAPI on `127.0.0.1:8080`, one SQLite file, and one HTTP session to Alpaca paper. There is no subprocess, no Postgres, no message broker, no worker pool, and no frontend. The trading tick fires on the bar boundary; a separate monitor runs every minute so exits never wait for a bar.

## Conventions

- **Type everything.** `mypy --strict` is on and clean.
- **Pydantic v2 for all data shapes.** Dataclasses are for frozen pinned constants and pure internal value objects.
- **`match` for state machines.** `PositionManager` is one.
- **Pure functions get property-based tests with `hypothesis`.** Indicators, the RiskEngine, the bar helpers and the hash chain all have them.
- **Comments explain "why", never "what".** Several modules carry a note about the bug the current design exists to prevent; keep those.
- **One file per concern.** If a file mixes concerns, split it.
- **Tests live in `test/`.** `test/conftest.py` isolates every test from the developer's `.env`; do not read real settings in a test.

## Running things

```bash
uv sync --extra dev

uv run ruff check .
uv run mypy app pinned
uv run python -m pytest -q

# Operator CLI
uv run python -m app.cli status
uv run python -m app.cli broker-probe
uv run python -m app.cli download-bars --days 365
uv run python -m app.cli backtest
uv run python -m app.cli trades --limit 20
uv run python -m app.cli journal --kind trade_closed
uv run python -m app.cli kill-switch clear --reason "reviewed the drawdown"
uv run python -m app.cli flatten
LLM_PROVIDER=fake uv run python -m app.cli evolve --dry-run

# Run the agent
uv run python -m app.main
```

Note: on Windows, `uv run pytest` can fail to canonicalize the script path; use `uv run python -m pytest`.

## Adding a new indicator

1. Write a pure function in `app/market/indicators/<name>.py` taking a `pd.Series` and returning a `pd.Series`.
2. Add a property-based test in `test/unit/market/indicators/test_<name>.py`.
3. Wire it into `app/strategy/runner.py` if the rule needs it, and add it to the `indicators` snapshot so the journal records it.

## Adding a strategy parameter

1. Add the field to `StrategyConfig` with pydantic bounds.
2. Decide deliberately whether the LLM may tune it. If yes, add the name to `MUTABLE_BY_LLM`, a range to `LLM_RANGES`, and the name to the `MutableField` literal in `app/llm/schemas.py`. If it is a safety floor, leave it out of all three.
3. Update `pinned/strategies/pullback_paxg_m15_v1.json` — a test asserts the file and the code agree.

## Adding an LLM call site

1. Add pydantic input and output models in `app/llm/schemas.py`. Output models must be `extra="forbid"` with no `Any` and no bare `dict`.
2. Add the site to the `Site` literal, to `LLM_SITE_BUDGETS` and to `SITE_EFFORT`.
3. Add a system prompt in `app/llm/prompts/registry.py`, opening with the standard boundary statement.
4. Call it from a scheduled job, never from the trading path.

## What NOT to do

- Don't add a dependency without justification. The set is curated and small.
- Don't add a service, container, or external process. One Python process is the boundary.
- Don't add an LLM call to the trading loop. The strategy and the risk engine are deterministic; that is the point.
- Don't add a "quick fix" that bypasses the journal or the trades table. Everything that affects state goes through both.
- Don't weaken a discipline gate to make a backtest look better. The gates are the product.
- Don't add a frontend. The JSON API and the CLI are the entire human interface.
