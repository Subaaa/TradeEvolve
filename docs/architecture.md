# Architecture

One Python process. No subprocess, no Postgres, no message broker, no worker
pool, no frontend.

```
                  ┌──────────────── one process ────────────────┐
   Alpaca paper ──┤  APScheduler ── tick_bar_close (on the bar)  │
   (HTTPS)        │              ── tick_monitor   (every 60s)   │
                  │              ── daily_rollover / review      │
                  │              ── weekly_evolution / shadow    │
                  │              ── audit_verify                 │
                  │  FastAPI (read-only, 127.0.0.1:8080)         │
                  │  SQLite (WAL) ── candles, trades, journal,   │
                  │                  strategy_versions, ...      │
                  └─────────────────────────────────────────────┘
                                     │
                            Anthropic API (daily + weekly only)
```

## The two loops

**The bar-close tick** fires at `:00/:15/:30/:45` plus twenty seconds. It
fetches bars from the broker, drops the forming bar, evaluates the rule on the
last closed bar, runs the risk engine, and — if approved — enters and protects
the position. It is guarded twice against acting on a bar twice: a
`last_processed_bar_ts` marker, and an idempotency tag that is a pure function
of (version, side, bar), which the broker itself deduplicates.

**The monitor** runs every sixty seconds regardless of the bar clock, because
an exit must not wait for a bar to close. It detects stop fills, takes profit,
applies the time stop and the hard lifetime cap, flattens on the kill switch,
re-places a stop that has gone missing, and market-sells as a failsafe if price
trades through an unfilled stop twice in a row.

## Why the discipline counters are a query

Every gate in `app/risk/engine.py` compares against a field on `AccountState`,
and `AccountState` is rebuilt from the `trades` table on every tick. This is
deliberate. The previous design incremented these counters in memory and, in
practice, never updated them at all — so the daily-loss limit, the weekly-loss
limit and the consecutive-loss cooldown were all unreachable code while
appearing to be enforced. Deriving them means a crash, a restart or a second
process all see the same numbers.

## Why a position is never unprotected

`PositionManager.open` places a `stop_limit` immediately after the entry fills.
If that placement fails, the position is market-sold on the spot rather than
left for the next cycle. Alpaca crypto has no plain `stop` order type, so the
protective order is a `stop_limit` whose limit sits 50 bps below the trigger —
wide enough that a fast move still fills. `reconcile_on_boot` closes the loop
after a restart: it adopts a position it has a record of (re-placing the stop
if needed), closes a database row whose position is gone, and flattens an
orphan position it cannot account for.

## The evolution loop

Once a week: build stats from the trades table, Claude proposes at most one
bounded parameter change, three independent layers validate it (the output
schema's field enum, the challenger builder's allow-list and range check, and
the config's own validators), walk-forward against the champion on 90 days,
bootstrap resampling, then stage as a shadow. Once a day the arbiter asks
whether the shadow has earned promotion. Only `promote()` can change the
champion, and it does so in one transaction guarded by a partial unique index.

Two gates in the previous version were incapable of ever firing: the Monte
Carlo compared the sum of a shuffled list to the sum of the original (always
equal), and the shadow measured elapsed hours from the last candle rather than
from the start. Both have regression tests naming the old bug.

## Failure handling

| Failure | Behaviour |
|---|---|
| A job raises | Caught and logged; the scheduler thread survives. |
| Stop placement fails | Position flattened immediately, risk event journaled. |
| Stop goes missing | Re-placed within one monitor cycle, up to three attempts, else flattened. |
| Price through an unfilled stop | Market sell after two consecutive sightings. |
| Process dies with a position | Adopted and re-protected at boot, or flattened if unaccountable. |
| Broker holds an unknown position | Flattened, journaled as an orphan. |
| Daily loss or loss count breached | Kill switch until the next UTC day. |
| Weekly loss or drawdown breached | Kill switch, manual CLI clear only. |
| Journal hash chain broken | Hourly audit writes a system event. |
| LLM unavailable | The day's review or the week's hypothesis is skipped; trading is unaffected. |
