# Operations

This document covers how to run, monitor, and back up the
TradeEvolve agent. For the architecture overview, see
`architecture.md`.

## Local development

```bash
# 1. Install
uv sync --extra dev

# 2. Configure
cp .env.example .env.dev
# Edit .env.dev with real keys.

# 3. Run
uv run python -m app.main
```

The agent binds to `127.0.0.1:8080`. The Alpaca MCP subprocess is
started on demand (lazy) by the order gateway.

## CLI

```bash
# Backtest a strategy on a candle CSV
uv run python -m app.cli backtest \
  --config pinned/strategies/ema_atr_xauusd_h1_v1.json \
  --candles data/fixtures/xauusd-h1-trending.csv \
  --start 2024-01-01T00:00:00+00:00 \
  --end 2024-01-21T00:00:00+00:00 \
  --equity 10000

# Tail the journal
uv run python -m app.cli journal --limit 20

# Replay the weekly loop
uv run python -m app.cli replay --week 2025-W01

# Dry-run a promotion
uv run python -m app.cli promote-dry-run --experiment-id 42
```

## Production (Ubuntu VPS)

### systemd

```bash
# Install
sudo cp deploy/TradeEvolve.service /etc/systemd/system/
sudo cp deploy/TradeEvolve-db-backup.{service,timer} /etc/systemd/system/
sudo systemctl daemon-reload

# Configure
sudo mkdir -p /opt/TradeEvolve
sudo chown -R deploy:deploy /opt/TradeEvolve
# rsync the repo
# create /opt/TradeEvolve/.env with real keys

# Start
sudo systemctl enable --now TradeEvolve.service
sudo systemctl enable --now TradeEvolve-db-backup.timer
```

### Docker (one container)

```bash
docker compose -f deploy/docker/docker-compose.yml up -d
```

### Backups

The `TradeEvolve-db-backup.timer` runs `pg_dump` once a day
at 00:00 UTC. The dump is gzipped to `data/backups/backup-<date>.sql.gz`.

A `restore_drill` is a planned Phase 6 addition; for now, the
procedure is manual:

```bash
# 1. Stop the agent
sudo systemctl stop TradeEvolve

# 2. Restore
gunzip -c data/backups/backup-2025-01-15.sql.gz | psql "$SUPABASE_DB_URL"

# 3. Start the agent
sudo systemctl start TradeEvolve
```

## Monitoring

The FastAPI service exposes:

- `GET /health` — liveness
- `GET /metrics` — Prometheus text
- `GET /journal`, `/trades`, `/reports/*`, `/strategy/*`, `/system/state` — read-only JSON

The CLI's `journal` subcommand is a fallback when the API is down.

Alerts (kill switch, feed degradation, audit chain mismatch, etc.) are
drained to `ALERT_WEBHOOK_URL`. If unset, they are logged only.

## Updating

```bash
cd /opt/TradeEvolve
git pull
uv sync
# Apply migrations
supabase db push
sudo systemctl restart TradeEvolve
```

The previous image is kept as `tradeevolve:previous` for fast
rollback.

## Hardening checklist

- `LIVE_TRADING_ENABLED` is `False` in `pinned/simulation_constants.py`.
- The Alpaca MCP subprocess refuses to connect to anything but a
  paper-trading base URL.
- The FastAPI service binds to `127.0.0.1` only.
- The `journal_writer` DB role has `INSERT`-only on the journal tables.
- The systemd unit sets `NoNewPrivileges`, `PrivateTmp`,
  `ProtectSystem=strict`, and `ProtectHome=true`.
