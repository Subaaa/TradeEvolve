"""The weekly evolution loop, end to end, with a fake model.

`cli evolve --dry-run` runs this same path, so these tests also cover the
operator's way of checking the pipeline without spending a token.
"""
from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta

import pytest

from app.db import candles as candles_db
from app.db import experiments as exp_db
from app.db import strategy_versions as sv_db
from app.db.strategy_versions import VersionStatus
from app.evolution.pipeline import run_weekly_evolution
from app.journal.writer import JournalWriter
from app.llm.client import LlmClient
from app.llm.fake_provider import FakeProvider
from app.strategy.configs import StrategyConfig
from test.conftest import M15, flat_series

pytestmark = pytest.mark.unit

NOW = datetime(2026, 3, 8, 22, 0, tzinfo=UTC)


def seed_candles(conn: sqlite3.Connection, *, days: int = 95) -> None:
    bars_needed = days * 96
    df = flat_series(bars_needed, start=NOW - bars_needed * M15)
    from app.broker.types import Bar

    candles_db.upsert_bars(
        conn,
        "XAU_USD",
        "M15",
        [
            Bar(
                ts=ts.to_pydatetime(),
                o=float(r["o"]),
                h=float(r["h"]),
                l=float(r["l"]),
                c=float(r["c"]),
                v=float(r["v"]),
            )
            for ts, r in df.iterrows()
        ],
    )


def llm_with(candidate: dict[str, object] | None, conn: sqlite3.Connection) -> LlmClient:
    payload: dict[str, object] = {
        "summary": "a quiet week",
        "failure_patterns": [],
        "candidate": candidate,
        "blocked_reason": None if candidate else "nothing in the evidence",
    }
    return LlmClient(FakeProvider(responses=[payload]), conn)


def run_pipeline(
    conn: sqlite3.Connection,
    journal: JournalWriter,
    candidate: dict[str, object] | None,
    **kwargs: object,
):  # type: ignore[no-untyped-def]
    return run_weekly_evolution(
        conn,
        llm_with(candidate, conn),
        journal,
        instrument="XAU_USD",
        granularity="M15",
        initial_equity=10_000.0,
        now=NOW,
        **kwargs,  # type: ignore[arg-type]
    )


def test_no_candidate_is_an_acceptable_outcome(
    conn: sqlite3.Connection, journal: JournalWriter, champion_id: int
) -> None:
    result = run_pipeline(conn, journal, None)
    assert result.accepted is False
    assert "nothing in the evidence" in result.detail
    # No strategy version and no experiment were created.
    assert sv_db.load_shadow(conn) is None
    assert exp_db.active_experiment(conn) is None


def test_an_out_of_range_candidate_is_rejected_without_creating_a_version(
    conn: sqlite3.Connection, journal: JournalWriter, champion_id: int
) -> None:
    result = run_pipeline(
        conn,
        journal,
        {
            "field": "atr_sl_multiplier",
            "from_value": 1.5,
            "to_value": 9.0,  # far outside LLM_RANGES
            "reason": "much wider stops",
        },
    )
    assert result.accepted is False
    assert "outside" in result.detail
    assert len(sv_db.lineage(conn)) == 1  # only the champion
    rejected = exp_db.hypotheses_for_champion(conn, champion_id)
    assert len(rejected) == 1
    assert rejected[0].accepted is False


def test_a_candidate_naming_a_forbidden_field_never_reaches_the_config(
    conn: sqlite3.Connection, journal: JournalWriter, champion_id: int
) -> None:
    """Two layers refuse this, and the outer one fires first.

    The output schema constrains `field` to the mutable allow-list, so a
    response naming `min_atr_bps` fails validation before the pipeline sees
    it; `challenger_builder` re-checks the name anyway. Either way, no
    strategy version is created.
    """
    result = run_pipeline(
        conn,
        journal,
        {
            "field": "min_atr_bps",
            "from_value": 12.0,
            "to_value": 0.0,
            "reason": "trade everything",
        },
    )
    assert result.accepted is False
    assert len(sv_db.lineage(conn)) == 1
    assert exp_db.active_experiment(conn) is None


def test_a_stale_from_value_is_rejected(
    conn: sqlite3.Connection, journal: JournalWriter, champion_id: int
) -> None:
    result = run_pipeline(
        conn,
        journal,
        {
            "field": "tp_r_multiple",
            "from_value": 99.0,
            "to_value": 2.5,
            "reason": "written against an older champion",
        },
    )
    assert result.accepted is False
    assert "does not match" in result.detail


def test_a_valid_candidate_without_history_fails_offline_validation(
    conn: sqlite3.Connection, journal: JournalWriter, champion_id: int
) -> None:
    """No candles means no walk-forward, which means no promotion path."""
    result = run_pipeline(
        conn,
        journal,
        {
            "field": "tp_r_multiple",
            "from_value": 2.0,
            "to_value": 2.5,
            "reason": "targets are being missed",
        },
    )
    assert result.accepted is False
    assert "offline validation" in result.detail
    challenger = sv_db.get(conn, result.challenger_id or 0)
    assert challenger is not None
    assert challenger.status is VersionStatus.REJECTED
    experiment = exp_db.get_experiment(conn, result.experiment_id or 0)
    assert experiment is not None
    assert experiment.final_decision == "rejected"


def test_a_dry_run_changes_nothing(
    conn: sqlite3.Connection, journal: JournalWriter, champion_id: int
) -> None:
    seed_candles(conn, days=10)
    result = run_pipeline(
        conn,
        journal,
        {
            "field": "tp_r_multiple",
            "from_value": 2.0,
            "to_value": 2.5,
            "reason": "dry run",
        },
        dry_run=True,
    )
    assert "dry run" in result.detail
    # Ten days of flat candles cannot produce enough out-of-sample trades, so
    # the dry run must say it would reject — not merely that it ran.
    assert result.accepted is False
    assert "would reject" in result.detail
    assert len(sv_db.lineage(conn)) == 1
    assert exp_db.active_experiment(conn) is None


def test_only_one_experiment_runs_at_a_time(
    conn: sqlite3.Connection,
    journal: JournalWriter,
    champion_id: int,
    config: StrategyConfig,
) -> None:
    challenger = config.model_copy(update={"version_tag": "chal", "tp_r_multiple": 2.5})
    challenger_id = sv_db.insert_version(
        conn, challenger, status=VersionStatus.SHADOW, created_by="test"
    )
    exp_db.insert_experiment(
        conn,
        hypothesis_id=None,
        champion_id=champion_id,
        challenger_id=challenger_id,
        phases={},
        shadow_started_at=NOW - timedelta(days=1),
    )

    result = run_pipeline(
        conn,
        journal,
        {
            "field": "tp_r_multiple",
            "from_value": 2.0,
            "to_value": 3.0,
            "reason": "another idea",
        },
    )
    assert result.accepted is False
    assert "already in progress" in result.detail


def test_the_plateau_guard_blocks_a_fifth_variation(
    conn: sqlite3.Connection, journal: JournalWriter, champion_id: int
) -> None:
    from pinned.promotion_gates import PROMOTION_GATES

    for i in range(PROMOTION_GATES.max_hypothesis_attempts_per_champion):
        exp_db.insert_hypothesis(
            conn,
            weekly_review_id=None,
            champion_id=champion_id,
            field="tp_r_multiple",
            from_value=2.0,
            to_value=2.0 + i / 10,
            reason="attempt",
            accepted=False,
            reject_reason="did not beat the champion",
        )

    result = run_pipeline(
        conn,
        journal,
        {
            "field": "tp_r_multiple",
            "from_value": 2.0,
            "to_value": 2.6,
            "reason": "one more time",
        },
    )
    assert result.accepted is False
    assert "already failed" in result.detail


def test_the_journal_records_why_a_candidate_was_refused(
    conn: sqlite3.Connection, journal: JournalWriter, champion_id: int
) -> None:
    run_pipeline(
        conn,
        journal,
        {
            "field": "atr_sl_multiplier",
            "from_value": 1.5,
            "to_value": 9.0,  # a valid field name, an invalid value
            "reason": "much wider stops",
        },
    )
    rows = conn.execute(
        "SELECT payload FROM journal_entries WHERE kind = 'experiment_event'"
    ).fetchall()
    assert rows
    assert any("outside the allowed range" in r["payload"] for r in rows)


def test_the_weekly_review_is_persisted(
    conn: sqlite3.Connection, journal: JournalWriter, champion_id: int
) -> None:
    run_pipeline(conn, journal, None)
    rows = conn.execute("SELECT * FROM weekly_reviews").fetchall()
    assert len(rows) == 1
    assert "a quiet week" in rows[0]["output"]

