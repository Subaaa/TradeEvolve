"""Smoke tests that exercise the full CLI path on a fixture candle CSV."""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path


REPO = Path(__file__).resolve().parents[2]


def test_cli_backtest_runs() -> None:
    proc = subprocess.run(
        [
            sys.executable, "-m", "app.cli", "backtest",
            "--config", str(REPO / "pinned" / "strategies" / "ema_atr_xauusd_h1_v1.json"),
            "--candles", str(REPO / "data" / "fixtures" / "xauusd-h1-trending.csv"),
            "--start", "2024-01-01T00:00:00+00:00",
            "--end", "2024-01-21T00:00:00+00:00",
            "--equity", "10000",
            "--oos-label", "train",
        ],
        cwd=REPO, capture_output=True, text=True, check=True,
    )
    out = json.loads(proc.stdout)
    assert "metrics" in out
    assert "trade_count" in out["metrics"]
    assert "config_hash" in out
    assert "input_hash" in out


def test_health_endpoint_via_uvicorn() -> None:
    """Bring up the app on an ephemeral port and call /health."""
    import threading
    import time
    import urllib.request

    import uvicorn

    from app.api.server import create_app

    app = create_app()
    config = uvicorn.Config(app, host="127.0.0.1", port=0, log_level="warning")
    server = uvicorn.Server(config)
    t = threading.Thread(target=server.run, daemon=True)
    t.start()
    # Wait for the server to come up
    for _ in range(50):
        if server.started:
            break
        time.sleep(0.1)
    port = server.servers[0].sockets[0].getsockname()[1] if server.servers else None
    assert port is not None, "uvicorn did not start"
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/health", timeout=2) as r:
            body = json.loads(r.read().decode("utf-8"))
            assert body["status"] == "ok"
    finally:
        server.should_exit = True
        t.join(timeout=2)
