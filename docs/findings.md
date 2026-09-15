# Measured findings

Numbers from real Alpaca PAXG/USD data, 19,237 M15 bars covering
2026-02-12 to 2026-09-15. Reproduce with `uv run python -m app.cli backtest`.

## 1. The stop width alone decides what fees cost you

Risk-based sizing sets `units = risk / stop_distance`, so notional is inversely
proportional to the stop. The round-trip fee, charged on notional, therefore
works out to:

```
fee_in_R = round_trip_fee_bps / stop_width_bps
```

Account size, price and strategy all cancel out. On Alpaca crypto the round
trip is 50 bps, so:

| Stop width | Fee per trade |
|---|---|
| 50 bps | 1.00 R |
| 120 bps (a 1.5-ATR stop on M15) | 0.42 R |
| 200 bps | 0.25 R |
| 500 bps | 0.10 R |

This is why `RISK_LIMITS.min_sl_distance_bps` is 200 and why it is pinned. A
tighter stop is not a riskier choice, it is an arithmetically losing one.

## 2. Median ATR by timeframe, and the stop it implies

| Timeframe | Bars | Median ATR | 1.5-ATR stop | Fee per trade |
|---|---|---|---|---|
| M15 | 19,237 | 80 bps | 120 bps | 0.42 R |
| H1 | 5,142 | 126 bps | 189 bps | 0.26 R |
| H4 | 1,290 | 170 bps | 254 bps | 0.20 R |
| D | 216 | 321 bps | 482 bps | 0.10 R |

At M15 a 1.5-ATR stop cannot clear the cost floor at all, which is why the
baseline config uses 2.5.

## 3. The baseline strategy does not have an edge here

Across 36 configurations — four timeframes by four stop widths by three target
multiples — **not one was profitable.** The best was H4 with a 2.5-ATR stop and
a 1.5 R target, at -0.19% over seven months.

The shipped baseline (M15, 2.5 ATR, 2 R) measures:

| | |
|---|---|
| Trades | 86 |
| Net return | -7.53% |
| Expectancy | -0.204 R |
| Win rate | 20.9% |
| Fees paid | $816 |
| Max drawdown | 7.53% |

Exit reasons: 81 time stops, 2 stop losses, 2 hard time caps, 1 take profit.

That distribution is the diagnosis. The target sits 5 ATR from entry (a 2.5-ATR
stop at 2 R) and sixteen M15 bars is four hours; price essentially never travels
that far in that time, so trades expire rather than resolve. Either the target
is too far or the time stop is too short, and the weekly review reads exactly
these counts.

## What the baseline is for

The baseline is shipped as a **measurement harness, not an edge**. It exists so
the journal, the daily review and the weekly evolution loop have real trades to
work with, on paper money, with the risk limits capping the damage at 2% a day
and 10% total.

Nothing here was tuned toward a positive number. With 36 of 36 cells negative
there is nothing to tune toward, and selecting the least-bad cell of a grid on
the full history is precisely the p-hacking that the walk-forward, bootstrap and
plateau gates exist to prevent.

## 4. Neither a stricter entry filter nor a longer timeframe finds the edge

Two follow-up experiments, done as scratch diagnostics against the real
history (not committed as separate scripts, but reproducible from this
description):

**Momentum confirmation on M15.** Six filters layered on the baseline entry —
a strong-close requirement, a rising-EMA50 trend filter, an RSI band, a
double-bar breakout, a filter combination, and a breakeven-stop exit — were
tested with the same fees and risk gates. The best (strong close + trend
filter combined) roughly halved the loss (-0.204R → -0.076R) but also cut
trade count by 70% (86 → 26) and stayed net negative. An RSI band and a
breakeven-stop both made results *worse*: the RSI filter excluded exactly the
trades with real follow-through, and moving the stop to breakeven at 0.5R
converted marginal winners into scratches faster than it protected against
reversals — this setup needs room, not tight management.

**Different signal families on M15.** Mean-reversion (buying a z-score dip),
mean-reversion filtered to uptrends only, a volatility-squeeze breakout, and a
raw-momentum entry with no pullback wait were tested the same way.
Mean-reversion was the worst result found in any experiment (-0.26R to
-0.31R, 13-15% win rate) — dips here continue more often than they bounce,
which is itself informative: the instrument has enough directional
persistence that naive fading gets run over. Squeeze breakout fired twice in
seven months, too rare to read anything into. None were profitable.

**H4, with the two best M15 filters re-applied and a stop/target/time-stop
grid on top.** 81 configurations, 0 positive. Best: -0.078R (strong-close
filter, 1.5-ATR stop, 3R target), on only 9 trades in seven months — fewer
trades than M15, not more, and too few to be statistically meaningful either
way.

Across every experiment run against this dataset — the original 36-cell grid,
8 momentum-filter variants, 8 signal-family variants, and 81 H4
configurations, **135 configurations, 0 profitable** — no combination of
entry filter, exit management or timeframe tested turns this pattern
profitable on PAXG/USD. The cost floor (finding 1) sets a hard lower bound on
how tight a stop can be; changing what triggers the entry moves win rate
by single-digit points at best, nowhere near what the arithmetic needs.

## 5. A backtester/live mismatch was found and fixed while testing H4

The backtester used to force-close any open position when the bar entered the
weekend window (Friday 21:00 → Sunday 22:00 UTC). The live `PositionManager`
never implemented this — it only blocks *new* entries on the weekend, via the
same `is_weekend` check, and never force-exits a position already open. On
M15 the discrepancy was nearly invisible (3 of 86 trades). On H4, where a
typical 48-hour time stop is comparable to the 49-hour weekend window, it was
force-closing 30-40% of trades the live bot would have simply carried through
the weekend. Removed from `app/evolution/backtester.py` so the backtest
matches what `PositionManager` actually does; the M15 baseline numbers above
are unchanged by the fix (net -7.53%, 86 trades), which is the tell that this
particular bug was never the reason M15 was unprofitable — it just made the
H4 read look worse than it should have.

## What this means, updated

Three honest directions remain, in order of how much they change:

1. **Accept a lower trade frequency on H4.** Even after the backtester fix
   and re-applying the best M15 filters, H4 stayed net negative (0/81) and
   produced fewer trades than M15, not more. This lever is exhausted for this
   entry-rule family.
2. **Change the entry rule to a different logic entirely.** Tried — mean
   reversion and squeeze breakout — and mean reversion made things worse,
   which is itself real evidence about how this instrument moves at this
   timeframe, not a dead end to abandon without noting.
3. **Change the instrument.** The one lever not yet tested. PAXG/USD's ~114
   bps quoted spread plus the 50 bps fee is the largest unexamined cost
   component; nothing above changes it, because every experiment kept the
   instrument fixed. This is a product-scope decision — it moves the bot off
   gold entirely — not a parameter change, so it is not made unilaterally
   here; the spread/fee numbers above are what it would take to evaluate a
   candidate replacement.

## 6. Alpaca does not support bracket orders for crypto

`uv run python -m app.cli broker-probe --probe-bracket` against a live paper
account returns:

```
HTTP 422 crypto orders not allowed for advanced order_class: otoco
```

So a position cannot be opened and protected in a single atomic order. The
executor therefore enters at market and places a separate `stop_limit` sell
immediately afterwards, and treats failure to place it as an emergency: the
position is flattened rather than left naked. The probe result is stored in
`system_state.broker_capabilities` so the decision is recorded rather than
assumed.
