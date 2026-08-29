"""Small CLI for inspecting the system without a web UI.

Subcommands:
- backtest    : run a backtest over a candle CSV
- journal     : tail the journal (stub)
- replay      : run the full weekly loop on fixtures
- promote-dry-run : print what would happen for an experiment
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

from app.evolution.backtester import BacktestInput, run as run_backtest
from app.strategy.configs import StrategyConfig


def _load_candles_csv(path: Path) -> pd.DataFrame:
    df = pd.read_csv(path)
    df["ts"] = pd.to_datetime(df["ts"], utc=True)
    df = df.set_index("ts").sort_index()
    return df


def cmd_backtest(args: argparse.Namespace) -> int:
    cfg = StrategyConfig.from_json_file(Path(args.config))
    candles = _load_candles_csv(Path(args.candles))
    bt = BacktestInput(
        config=cfg, candles=candles,
        start=datetime.fromisoformat(args.start).replace(tzinfo=timezone.utc),
        end=datetime.fromisoformat(args.end).replace(tzinfo=timezone.utc),
        initial_equity=float(args.equity),
        oos_label=args.oos_label,
    )
    out = run_backtest(bt)
    print(json.dumps({
        "trades": len(out.trades),
        "metrics": {
            "net_return_pct": out.metrics.net_return_pct,
            "expectancy_r": out.metrics.expectancy_r,
            "max_drawdown_pct": out.metrics.max_drawdown_pct,
            "sharpe": out.metrics.sharpe,
            "profit_factor": out.metrics.profit_factor,
            "trade_count": out.metrics.trade_count,
            "consistency_score": out.metrics.consistency_score,
            "complexity_penalty": out.metrics.complexity_penalty,
            "primary_score": out.metrics.primary_score,
            "tie_break_score": out.metrics.tie_break_score,
        },
        "config_hash": out.config_hash,
        "input_hash": out.input_hash,
    }, indent=2, default=str))
    return 0


def cmd_journal(args: argparse.Namespace) -> int:
    print("(stub) journal listing is not wired to a real store in MVP scaffold")
    return 0


def cmd_replay(args: argparse.Namespace) -> int:
    print("(stub) replay is not wired to a real LLM stub in MVP scaffold")
    return 0


def cmd_promote_dry_run(args: argparse.Namespace) -> int:
    print(f"(stub) promote-dry-run experiment_id={args.experiment_id}")
    return 0


def main() -> int:
    p = argparse.ArgumentParser(prog="nito-trade")
    sub = p.add_subparsers(dest="cmd", required=True)

    pb = sub.add_parser("backtest")
    pb.add_argument("--config", required=True)
    pb.add_argument("--candles", required=True)
    pb.add_argument("--start", required=True)
    pb.add_argument("--end", required=True)
    pb.add_argument("--equity", default="10000")
    pb.add_argument("--oos-label", default="train", choices=["train", "validation", "holdout"])
    pb.set_defaults(func=cmd_backtest)

    pj = sub.add_parser("journal")
    pj.add_argument("--limit", type=int, default=20)
    pj.set_defaults(func=cmd_journal)

    pr = sub.add_parser("replay")
    pr.add_argument("--week", required=True)
    pr.set_defaults(func=cmd_replay)

    pp = sub.add_parser("promote-dry-run")
    pp.add_argument("--experiment-id", type=int, required=True)
    pp.set_defaults(func=cmd_promote_dry_run)

    args = p.parse_args()
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
