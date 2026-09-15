# TradeEvolve

A disciplined, **paper-trading** bot for gold, traded as **PAXG/USD** (Pax Gold, 1 token = 1 troy oz) on Alpaca paper, with a Claude-driven weekly loop that proposes bounded parameter changes.

The design goal is not a clever strategy. It is a bot that cannot make the mistakes a human makes under pressure: chasing an entry, widening a stop, doubling down to get a loss back, or taking one more trade after a bad morning. The strategy is deliberately simple; the discipline around it is where the work went.

## What it actually does

Every 15 minutes, on the bar boundary, it evaluates one fixed rule on **closed** bars only. If the rule fires and every risk gate passes, it buys at market, immediately places a protective stop at the broker, and manages the exit from a separate loop that runs every minute. Every decision, fill and exit is written to an append-only journal. Once a day Claude reads the trades that closed and says what happened; once a week it reads the aggregate and may propose **one** parameter change, which deterministic code then validates, walk-forward tests, and shadow-trades before anything is promoted.

## The rules that matter

These live in [`pinned/risk_limits.py`](pinned/risk_limits.py) and only a human can change them.

| Rule | Value |
|---|---|
| Risk per trade | 0.5% of equity, computed from the stop distance |
| Daily loss limit | 2% of **start-of-day** equity, then the day is over |
| Losses per day | 2, then the day is over |
| Weekly loss limit | 5%, kill switch, manual clear only |
| Max drawdown | 10% from the equity high, kill switch, manual clear only |
| Trades per day | 4 |
| After a stop-out | 4 bars (1h) before any re-entry |
| After any loss | 8 bars (2h) before re-entering the same direction |
| Between entries | 2 bars minimum |
| Open positions | 1, never scaled into, never averaged down |
| Stop loss | Mandatory, at the broker, within 3 ATR and at least 20 bps wide |
| Position lifetime | 96 bars (24h) hard cap |
| Weekend | No entries between Friday 21:00 and Sunday 22:00 UTC |

Seven "emotion-proof invariants" are stated in full at the top of that file, and each has a test that fails if the enforcement is removed.

## Quick start

```bash
uv sync --extra dev
cp .env.example .env     # add your Alpaca paper keys and Anthropic key
uv run python -m app.cli broker-probe
uv run python -m app.cli download-bars --days 365
uv run python -m app.cli backtest
uv run python -m app.main
```

The read-only JSON API comes up on `http://127.0.0.1:8080`.

## Components

| Component | Role |
|---|---|
| [`app/main.py`](app/main.py) | Boot: migrate, reconcile open positions, start the scheduler and the API. |
| [`app/broker/`](app/broker/) | In-process Alpaca paper client, plus a fake broker for tests. |
| [`app/market/`](app/market/) | Bar helpers (including the forming-bar guard) and indicators. |
| [`app/strategy/`](app/strategy/) | The rule and its config, with the LLM-mutable allow-list. |
| [`app/risk/`](app/risk/) | RiskEngine (pure), position sizing, account state, kill switch. |
| [`app/execution/`](app/execution/) | Position lifecycle: open, protect, monitor, exit, reconcile. |
| [`app/journal/`](app/journal/) | Append-only journal with a per-session hash chain. |
| [`app/llm/`](app/llm/) | Provider boundary, Claude provider, budgets and accounting. |
| [`app/evolution/`](app/evolution/) | Backtester, walk-forward, bootstrap, shadow, arbiter, weekly pipeline. |
| [`app/db/`](app/db/) | SQLite schema and stores. |
| [`app/scheduler/`](app/scheduler/) | The job table and the jobs. |
| [`pinned/`](pinned/) | Constants the LLM can never reach. |

## The strategy

Long-only EMA trend with a pullback resumption entry, on M15 closed bars:

1. Fast EMA above slow EMA, and price above the slow EMA.
2. Price touched the fast EMA within the last few bars.
3. The current bar closes above the previous bar's high and above the fast EMA.
4. Volatility is neither a spike nor too quiet to pay the round-trip fee.

Exit at an ATR stop, a fixed R-multiple target, or a bar-count time stop. It is not assumed to be profitable; it is assumed to be *measurable*.

## Costs are modelled honestly

Alpaca charges 25 basis points per side on crypto, so a market-in/market-out round trip costs about 50 bps of notional. That is comparable to the entire move an M15 gold strategy is trying to capture, so the backtester applies it by default and the strategy refuses to trade a market too quiet to pay it. A backtest that assumes zero fees is not optimistic, it is unrelated.

## The `pinned/` invariant

`pinned/` holds risk limits, promotion gates, simulation constants and the baseline strategy. A build-time test fails if any module under `app/` imports a symbol from `pinned/` that is not on the allow-list, assigns to a pinned constant, or if the LLM's mutation allow-list ever overlaps a risk or gate field name.

## Tests

```bash
uv run python -m pytest -q
```

Around 200 tests. The ones that matter most: every discipline gate has an explicit test, the position manager is tested against a fake broker through stop fills, failed stop placement, lost stops, failsafes and boot reconciliation, and the two gates that were previously incapable of firing (the resampling gate and the shadow readiness gate) have regression tests naming the old bug.

## Status

Paper trading only. `LIVE_TRADING_ENABLED` is a pinned `False`, and the broker client refuses to construct against a live URL. Going live would be a separate, deliberate, human decision — and the honest prerequisite is a backtest that clears its costs, not a run of good days.
