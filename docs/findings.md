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

Exit reasons: 78 time stops, 3 weekend flattens, 2 stop losses, 2 hard time
caps, 1 take profit.

That distribution is the diagnosis. The target sits 5 ATR from entry (a 2.5-ATR
stop at 2 R) and sixteen M15 bars is four hours; price essentially never travels
that far in that time, so trades expire rather than resolve. Either the target
is too far or the time stop is too short, and the weekly review reads exactly
these counts.

## What this means

The baseline is shipped as a **measurement harness, not an edge**. It exists so
the journal, the daily review and the weekly evolution loop have real trades to
work with, on paper money, with the risk limits capping the damage at 2% a day
and 10% total.

Nothing here was tuned toward a positive number. With 36 of 36 cells negative
there is nothing to tune toward, and selecting the least-bad cell of a grid on
the full history is precisely the p-hacking that the walk-forward, bootstrap and
plateau gates exist to prevent.

Three honest directions, in order of how much they change:

1. **Accept a lower trade frequency.** H4 costs 0.20 R per trade instead of
   0.42 R, and the instrument's volatility supports it better.
2. **Change the entry rule.** A 20.9% win rate against a 2 R target is a losing
   combination before fees; the rule is not finding the moves it needs.
3. **Change the instrument.** PAXG/USD showed a 114 bps quoted spread when last
   sampled, on top of the 50 bps fee. A liquid major would halve the cost base
   before anything else is touched.

## 4. Alpaca does not support bracket orders for crypto

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
