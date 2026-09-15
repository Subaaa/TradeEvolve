"""Operator CLI.

Everything that needs a human lives here rather than in the read-only API:
clearing a kill switch that requires manual clearance, flattening a position
by hand, and probing what the broker will actually accept.
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pandas as pd

from app.broker.alpaca import build_client
from app.config import get_settings
from app.db import candles as candles_db
from app.db import experiments as exp_db
from app.db import strategy_versions as sv_db
from app.db import system_state as ss
from app.db import trades as trades_db
from app.db.journal_store import SqliteJournalStore
from app.db.sqlite import backup_to, open_db, utcnow
from app.evolution.backtester import BacktestInput
from app.evolution.backtester import run as run_backtest
from app.journal.types import JournalKind
from app.risk import kill_switch
from app.risk.types import ExitReason
from app.strategy.configs import StrategyConfig


def _out(obj: Any) -> None:
    print(json.dumps(obj, indent=2, default=str))


def _db(args: argparse.Namespace):  # type: ignore[no-untyped-def]
    """Open the database, migrate it, and make sure a champion exists.

    Bootstrapping here as well as in `app.main` means a fresh database is
    usable from the CLI without having started the agent first — otherwise
    `status` and `evolve` both report "no champion" on a clean install.
    """
    s = get_settings()
    conn = open_db(Path(args.db) if getattr(args, "db", None) else s.db_path)
    sv_db.ensure_champion(
        conn, StrategyConfig.from_json_file(s.resolved_champion_config())
    )
    return conn


# ------------------------------------------------------------------ backtest


def _load_candles(args: argparse.Namespace) -> pd.DataFrame:
    if getattr(args, "candles", None):
        df = pd.read_csv(args.candles)
        df["ts"] = pd.to_datetime(df["ts"], utc=True)
        return df.set_index("ts").sort_index()
    s = get_settings()
    return candles_db.read_bars(_db(args), s.instrument, s.granularity)


def cmd_backtest(args: argparse.Namespace) -> int:
    s = get_settings()
    config = StrategyConfig.from_json_file(
        Path(args.config) if args.config else s.resolved_champion_config()
    )
    candles = _load_candles(args)
    if candles.empty:
        print("no candles available; run `download-bars` first", file=sys.stderr)
        return 1

    start = (
        datetime.fromisoformat(args.start).replace(tzinfo=UTC)
        if args.start
        else candles.index[0].to_pydatetime()
    )
    end = (
        datetime.fromisoformat(args.end).replace(tzinfo=UTC)
        if args.end
        else candles.index[-1].to_pydatetime()
    )

    out = run_backtest(
        BacktestInput(
            config=config,
            candles=candles,
            start=start,
            end=end,
            initial_equity=float(args.equity),
            oos_label=args.oos_label,
        )
    )
    _out(
        {
            "config": config.version_tag,
            "window": [start.isoformat(), end.isoformat()],
            "bars": len(candles),
            "metrics": out.metrics.as_dict(),
            "config_hash": out.config_hash,
            "input_hash": out.input_hash,
        }
    )
    return 0


# ------------------------------------------------------------- download-bars


def cmd_download_bars(args: argparse.Namespace) -> int:
    s = get_settings()
    conn = _db(args)
    broker = build_client(s, write_enabled=False)
    end = utcnow()
    start = end - timedelta(days=int(args.days))

    bars = broker.fetch_bars(
        s.instrument, s.granularity, start=start, end=end, limit=int(args.limit)
    )
    written = candles_db.upsert_bars(conn, s.instrument, s.granularity, bars)
    total = candles_db.count(conn, s.instrument, s.granularity)

    if args.csv:
        path = Path(args.csv)
        path.parent.mkdir(parents=True, exist_ok=True)
        candles_db.read_bars(conn, s.instrument, s.granularity).to_csv(path)

    _out(
        {
            "fetched": len(bars),
            "written": written,
            "total_in_db": total,
            "first": bars[0].ts.isoformat() if bars else None,
            "last": bars[-1].ts.isoformat() if bars else None,
            "csv": args.csv,
        }
    )
    return 0


# -------------------------------------------------------------- broker-probe


def cmd_broker_probe(args: argparse.Namespace) -> int:
    """Find out what this account actually accepts, and record it."""
    s = get_settings()
    conn = _db(args)
    broker = build_client(s, write_enabled=s.alpaca_write_enabled)

    account = broker.get_account()
    bars = broker.fetch_bars(s.instrument, s.granularity, limit=3)
    quote = broker.latest_quote(s.instrument)

    capabilities: dict[str, Any] = {
        "checked_at": utcnow().isoformat(),
        "symbol": broker.symbol,
        "write_enabled": broker.write_enabled,
        "bracket_supported": None,
        "bracket_detail": "not probed (writes disabled)",
    }

    if broker.write_enabled and args.probe_bracket:
        # A bracket order is not documented for Alpaca crypto. Rather than
        # assume, try one tiny order far from the market and see; the result
        # decides which exit mode the position manager uses.
        # A resting buy far below the market, with the protective legs placed
        # relative to THAT entry price rather than to the current quote:
        # Alpaca validates the bracket against the order's own base price, so
        # legs computed from the live quote are rejected for arithmetic
        # reasons and tell us nothing about whether brackets are supported.
        entry = round(quote.bid * 0.5, 2)
        stop = round(entry * 0.95, 2)
        body = {
            "symbol": broker.symbol,
            "qty": "0.001",
            "side": "buy",
            "type": "limit",
            "limit_price": f"{entry:.2f}",
            "time_in_force": "gtc",
            "order_class": "bracket",
            "take_profit": {"limit_price": f"{entry * 1.10:.2f}"},
            "stop_loss": {
                "stop_price": f"{stop:.2f}",
                "limit_price": f"{stop * 0.99:.2f}",
            },
        }
        try:
            placed = broker.submit_raw_order(body)
            capabilities["bracket_supported"] = True
            capabilities["bracket_detail"] = f"accepted as order {placed.get('id')}"
            broker.cancel_order(str(placed["id"]))
        except Exception as exc:
            capabilities["bracket_supported"] = False
            capabilities["bracket_detail"] = str(exc)[:300]

    ss.set_(conn, ss.BROKER_CAPABILITIES, capabilities)
    _out(
        {
            "account": account.model_dump(mode="json"),
            "quote": quote.model_dump(mode="json"),
            "bars": [b.model_dump(mode="json") for b in bars],
            "capabilities": capabilities,
        }
    )
    return 0


# -------------------------------------------------------------- introspection


def cmd_status(args: argparse.Namespace) -> int:
    s = get_settings()
    conn = _db(args)
    champion = sv_db.load_champion(conn)
    kill = ss.load_kill_switch(conn)
    day = ss.load_day_anchor(conn)
    recent = trades_db.list_closed(conn, limit=20)
    _out(
        {
            "instrument": s.instrument,
            "granularity": s.granularity,
            "live_trading_enabled": s.live_trading_enabled,
            "champion": champion.version_tag if champion else None,
            "kill_switch": kill.model_dump(mode="json"),
            "day_anchor": day.model_dump(mode="json") if day else None,
            "open_trade": (
                t.model_dump(mode="json")
                if (t := trades_db.open_trade(conn))
                else None
            ),
            "candles_stored": candles_db.count(conn, s.instrument, s.granularity),
            "recent_summary": trades_db.summarize(recent),
            "active_experiment": (
                e.model_dump(mode="json")
                if (e := exp_db.active_experiment(conn))
                else None
            ),
        }
    )
    return 0


def cmd_trades(args: argparse.Namespace) -> int:
    rows = trades_db.list_recent(_db(args), limit=int(args.limit))
    _out(
        [
            {
                "id": t.id,
                "status": t.status.value,
                "opened_at": t.opened_at.isoformat(),
                "closed_at": t.closed_at.isoformat() if t.closed_at else None,
                "units": t.units,
                "entry": t.entry_fill_price,
                "sl": t.sl,
                "tp": t.tp,
                "exit": t.exit_price,
                "exit_reason": t.exit_reason.value if t.exit_reason else None,
                "pnl_usd": t.pnl_usd,
                "pnl_r": t.pnl_r,
                "mfe_r": t.mfe_r,
                "mae_r": t.mae_r,
                "bars_held": t.bars_held,
                "review": t.review,
            }
            for t in rows
        ]
    )
    return 0


def cmd_journal(args: argparse.Namespace) -> int:
    store = SqliteJournalStore(_db(args))
    kind = JournalKind(args.kind) if args.kind else None
    entries = store.list_recent(limit=int(args.limit), kind=kind)
    _out(
        [
            {
                "id": e.id,
                "ts": e.ts.isoformat(),
                "kind": e.kind.value,
                "next_state": e.next_state.value if e.next_state else None,
                "reason_codes": e.reason_codes,
                "payload": e.payload,
            }
            for e in entries
        ]
    )
    return 0


# ----------------------------------------------------------------- operations


def cmd_kill_switch(args: argparse.Namespace) -> int:
    conn = _db(args)
    if args.action == "engage":
        ks = kill_switch.engage(
            conn, reason=args.reason or "engaged from the CLI", requires_manual_clear=True
        )
    else:
        ks = kill_switch.clear(conn, force=True)
    _out(ks.model_dump(mode="json"))
    return 0


def cmd_flatten(args: argparse.Namespace) -> int:
    from app.scheduler import deps

    closed = deps.position_manager().flatten_all(reason=ExitReason.MANUAL)
    _out(
        [
            {"id": t.id, "exit_price": t.exit_price, "pnl_usd": t.pnl_usd}
            for t in closed
        ]
    )
    return 0


def cmd_evolve(args: argparse.Namespace) -> int:
    from app.evolution.pipeline import run_weekly_evolution
    from app.journal.writer import JournalWriter
    from app.llm.client import LlmClient
    from app.llm.client import build_client as build_llm

    s = get_settings()
    conn = _db(args)
    journal = JournalWriter(SqliteJournalStore(conn), session_id="cli-evolve")

    if s.llm_provider == "fake":
        # A dry run with the fake provider should exercise the whole pipeline,
        # so feed it a hypothesis that is actually valid rather than the
        # synthesised zeros the generic default produces.
        from app.llm.fake_provider import FakeProvider, weekly_review_handler

        champion = sv_db.load_champion(conn)
        assert champion is not None  # _db() bootstraps one
        llm = LlmClient(
            FakeProvider(handler=weekly_review_handler(champion.config)), conn
        )
    else:
        llm = build_llm(s, conn)

    result = run_weekly_evolution(
        conn,
        llm,
        journal,
        instrument=s.instrument,
        granularity=s.granularity,
        initial_equity=s.initial_paper_equity_usd,
        dry_run=bool(args.dry_run),
    )
    _out(
        {
            "accepted": result.accepted,
            "detail": result.detail,
            "candidate": (
                result.candidate.model_dump(mode="json") if result.candidate else None
            ),
            "challenger_id": result.challenger_id,
            "experiment_id": result.experiment_id,
        }
    )
    return 0


def cmd_llm_ping(args: argparse.Namespace) -> int:
    from app.llm.client import build_provider

    s = get_settings()
    provider = build_provider(s)
    result = provider.complete_json(
        system="You are a health check. Answer briefly and literally.",
        user="Reply with ok=true and a one-word note.",
        schema={
            "type": "object",
            "properties": {"ok": {"type": "boolean"}, "note": {"type": "string"}},
            "required": ["ok", "note"],
            "additionalProperties": False,
        },
        max_output_tokens=256,
        effort="low",
    )
    _out(
        {
            "provider": provider.name,
            "model": result.model,
            "data": result.data,
            "usage": result.usage.model_dump(mode="json"),
        }
    )
    return 0


def cmd_backup(args: argparse.Namespace) -> int:
    s = get_settings()
    conn = _db(args)
    dest = s.backups_dir / f"tradeevolve-{utcnow():%Y%m%dT%H%M%SZ}.sqlite"
    backup_to(conn, dest)
    _out({"backup": str(dest), "bytes": dest.stat().st_size})
    return 0


# ----------------------------------------------------------------------- main


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="tradeevolve")
    p.add_argument("--db", help="path to the SQLite file (defaults to DB_PATH)")
    sub = p.add_subparsers(dest="cmd", required=True)

    pb = sub.add_parser("backtest", help="run a backtest over stored or CSV candles")
    pb.add_argument("--config")
    pb.add_argument("--candles", help="CSV file; omit to use the database")
    pb.add_argument("--start")
    pb.add_argument("--end")
    pb.add_argument("--equity", default="10000")
    pb.add_argument("--oos-label", default="train", choices=["train", "validation"])
    pb.set_defaults(func=cmd_backtest)

    pd_ = sub.add_parser("download-bars", help="fetch history from Alpaca into SQLite")
    pd_.add_argument("--days", default="365")
    pd_.add_argument("--limit", default="200000")
    pd_.add_argument("--csv", help="also write the full series to this CSV path")
    pd_.set_defaults(func=cmd_download_bars)

    pp = sub.add_parser("broker-probe", help="check the account and what it accepts")
    pp.add_argument("--probe-bracket", action="store_true")
    pp.set_defaults(func=cmd_broker_probe)

    ps = sub.add_parser("status", help="one-shot system status")
    ps.set_defaults(func=cmd_status)

    pt = sub.add_parser("trades", help="list recent trades")
    pt.add_argument("--limit", default="20")
    pt.set_defaults(func=cmd_trades)

    pj = sub.add_parser("journal", help="tail the journal")
    pj.add_argument("--limit", default="20")
    pj.add_argument("--kind", choices=[k.value for k in JournalKind])
    pj.set_defaults(func=cmd_journal)

    pk = sub.add_parser("kill-switch", help="engage or clear the kill switch")
    pk.add_argument("action", choices=["engage", "clear"])
    pk.add_argument("--reason", default="")
    pk.set_defaults(func=cmd_kill_switch)

    pf = sub.add_parser("flatten", help="close every open position now")
    pf.set_defaults(func=cmd_flatten)

    pe = sub.add_parser("evolve", help="run the weekly evolution pipeline")
    pe.add_argument("--dry-run", action="store_true")
    pe.set_defaults(func=cmd_evolve)

    pl = sub.add_parser("llm-ping", help="verify the LLM provider is reachable")
    pl.set_defaults(func=cmd_llm_ping)

    pbk = sub.add_parser("backup", help="write a consistent copy of the database")
    pbk.set_defaults(func=cmd_backup)

    return p


def main() -> int:
    args = build_parser().parse_args()
    result: int = args.func(args)
    return result


if __name__ == "__main__":
    raise SystemExit(main())
