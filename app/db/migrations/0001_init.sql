-- TradeEvolve SQLite schema.
--
-- Append-only guarantees that used to rely on two Postgres roles are now
-- enforced by triggers: the journal cannot be updated or deleted at all, and
-- a closed trade can only ever have its `review` column filled in.

PRAGMA journal_mode = WAL;
PRAGMA foreign_keys = ON;

-- ---------------------------------------------------------------- candles --

CREATE TABLE IF NOT EXISTS candles (
    instrument  TEXT NOT NULL,
    granularity TEXT NOT NULL,
    ts          TEXT NOT NULL,
    o           REAL NOT NULL,
    h           REAL NOT NULL,
    l           REAL NOT NULL,
    c           REAL NOT NULL,
    v           REAL NOT NULL DEFAULT 0.0,
    PRIMARY KEY (instrument, granularity, ts)
);

-- ------------------------------------------------------- strategy versions --

CREATE TABLE IF NOT EXISTS strategy_versions (
    id                INTEGER PRIMARY KEY,
    version_tag       TEXT NOT NULL UNIQUE,
    parent_version_id INTEGER REFERENCES strategy_versions(id),
    created_at        TEXT NOT NULL,
    created_by        TEXT NOT NULL,
    config            TEXT NOT NULL,
    config_hash       TEXT NOT NULL,
    status            TEXT NOT NULL
        CHECK (status IN ('champion', 'challenger', 'shadow', 'retired', 'rejected'))
);

-- At most one champion, ever.
CREATE UNIQUE INDEX IF NOT EXISTS strategy_versions_one_champion
    ON strategy_versions(status) WHERE status = 'champion';

-- --------------------------------------------------------------- journal ---

CREATE TABLE IF NOT EXISTS journal_entries (
    id                  INTEGER PRIMARY KEY,
    ts                  TEXT NOT NULL,
    kind                TEXT NOT NULL,
    strategy_version_id INTEGER,
    session_id          TEXT NOT NULL,
    prev_state          TEXT,
    next_state          TEXT,
    reason_codes        TEXT NOT NULL DEFAULT '[]',
    payload             TEXT NOT NULL,
    hash                BLOB NOT NULL,
    prev_hash           BLOB
);

CREATE INDEX IF NOT EXISTS journal_session ON journal_entries(session_id, id);
CREATE INDEX IF NOT EXISTS journal_kind_ts ON journal_entries(kind, ts);

CREATE TRIGGER IF NOT EXISTS journal_no_update
BEFORE UPDATE ON journal_entries
BEGIN
    SELECT RAISE(ABORT, 'journal_entries is append-only');
END;

CREATE TRIGGER IF NOT EXISTS journal_no_delete
BEFORE DELETE ON journal_entries
BEGIN
    SELECT RAISE(ABORT, 'journal_entries is append-only');
END;

-- ---------------------------------------------------------------- trades ---

CREATE TABLE IF NOT EXISTS trades (
    id                     INTEGER PRIMARY KEY,
    client_tag             TEXT NOT NULL UNIQUE,
    strategy_version_id    INTEGER NOT NULL REFERENCES strategy_versions(id),
    config_hash            TEXT NOT NULL,
    session_id             TEXT NOT NULL,
    status                 TEXT NOT NULL
        CHECK (status IN ('PENDING_ENTRY', 'OPEN_UNPROTECTED', 'OPEN_PROTECTED',
                          'PENDING_EXIT', 'CLOSED')),
    side                   TEXT NOT NULL,
    units                  REAL NOT NULL,
    signal_bar_ts          TEXT NOT NULL,
    decision_close         REAL NOT NULL,
    opened_at              TEXT NOT NULL,
    entry_order_id         TEXT,
    entry_fill_price       REAL,
    entry_slippage_bps     REAL,
    sl                     REAL NOT NULL,
    tp                     REAL NOT NULL,
    sl_distance            REAL NOT NULL,
    sl_order_id            TEXT,
    indicators             TEXT NOT NULL DEFAULT '{}',
    closed_at              TEXT,
    exit_order_id          TEXT,
    exit_price             REAL,
    exit_reason            TEXT,
    pnl_usd                REAL,
    pnl_r                  REAL,
    fees_usd               REAL,
    fees_estimated         INTEGER NOT NULL DEFAULT 1,
    mfe                    REAL,
    mae                    REAL,
    mfe_r                  REAL,
    mae_r                  REAL,
    bars_held              INTEGER,
    journal_entry_open_id  INTEGER,
    journal_entry_close_id INTEGER,
    review                 TEXT
);

CREATE INDEX IF NOT EXISTS trades_status ON trades(status);
CREATE INDEX IF NOT EXISTS trades_closed_at ON trades(closed_at);
CREATE INDEX IF NOT EXISTS trades_opened_at ON trades(opened_at);

-- A closed trade is immutable except for the LLM review text.
CREATE TRIGGER IF NOT EXISTS trades_immutable_once_closed
BEFORE UPDATE ON trades
WHEN OLD.status = 'CLOSED'
 AND (NEW.status          IS NOT OLD.status
   OR NEW.exit_price      IS NOT OLD.exit_price
   OR NEW.pnl_usd         IS NOT OLD.pnl_usd
   OR NEW.pnl_r           IS NOT OLD.pnl_r
   OR NEW.exit_reason     IS NOT OLD.exit_reason
   OR NEW.closed_at       IS NOT OLD.closed_at
   OR NEW.units           IS NOT OLD.units
   OR NEW.entry_fill_price IS NOT OLD.entry_fill_price)
BEGIN
    SELECT RAISE(ABORT, 'a closed trade is immutable');
END;

CREATE TRIGGER IF NOT EXISTS trades_no_delete
BEFORE DELETE ON trades
BEGIN
    SELECT RAISE(ABORT, 'trades is append-only');
END;

-- ----------------------------------------------------------- risk events ---

CREATE TABLE IF NOT EXISTS risk_events (
    id               INTEGER PRIMARY KEY,
    ts               TEXT NOT NULL,
    type             TEXT NOT NULL,
    limit_name       TEXT NOT NULL,
    value            REAL,
    action           TEXT NOT NULL,
    journal_entry_id INTEGER
);

CREATE INDEX IF NOT EXISTS risk_events_ts ON risk_events(ts);

-- --------------------------------------------------------- system state ----

CREATE TABLE IF NOT EXISTS system_state (
    key        TEXT PRIMARY KEY,
    value      TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

-- ------------------------------------------------------------- evolution ---

CREATE TABLE IF NOT EXISTS weekly_reviews (
    id           INTEGER PRIMARY KEY,
    period_start TEXT NOT NULL,
    period_end   TEXT NOT NULL,
    input        TEXT NOT NULL,
    output       TEXT NOT NULL,
    llm_call_id  INTEGER
);

CREATE TABLE IF NOT EXISTS hypotheses (
    id               INTEGER PRIMARY KEY,
    weekly_review_id INTEGER REFERENCES weekly_reviews(id),
    proposed_at      TEXT NOT NULL,
    champion_id      INTEGER NOT NULL REFERENCES strategy_versions(id),
    field            TEXT NOT NULL,
    from_value       TEXT NOT NULL,
    to_value         TEXT NOT NULL,
    reason           TEXT,
    accepted         INTEGER NOT NULL DEFAULT 0,
    reject_reason    TEXT
);

CREATE TABLE IF NOT EXISTS experiments (
    id                INTEGER PRIMARY KEY,
    hypothesis_id     INTEGER REFERENCES hypotheses(id),
    champion_id       INTEGER NOT NULL REFERENCES strategy_versions(id),
    challenger_id     INTEGER NOT NULL REFERENCES strategy_versions(id),
    created_at        TEXT NOT NULL,
    shadow_started_at TEXT,
    phases            TEXT NOT NULL DEFAULT '{}',
    final_decision    TEXT,
    reason_codes      TEXT NOT NULL DEFAULT '[]',
    decided_at        TEXT
);

CREATE INDEX IF NOT EXISTS experiments_decision ON experiments(final_decision);

-- ----------------------------------------------------------- reporting -----

CREATE TABLE IF NOT EXISTS daily_reports (
    day            TEXT PRIMARY KEY,
    net_pnl        REAL NOT NULL DEFAULT 0.0,
    num_trades     INTEGER NOT NULL DEFAULT 0,
    num_rejections INTEGER NOT NULL DEFAULT 0,
    payload        TEXT NOT NULL DEFAULT '{}'
);

CREATE TABLE IF NOT EXISTS llm_calls (
    id                INTEGER PRIMARY KEY,
    ts                TEXT NOT NULL,
    site              TEXT NOT NULL,
    model             TEXT NOT NULL,
    input_tokens      INTEGER NOT NULL DEFAULT 0,
    output_tokens     INTEGER NOT NULL DEFAULT 0,
    cache_read_tokens INTEGER NOT NULL DEFAULT 0,
    latency_ms        INTEGER NOT NULL DEFAULT 0,
    ok                INTEGER NOT NULL DEFAULT 0,
    error             TEXT,
    request_id        TEXT
);

CREATE INDEX IF NOT EXISTS llm_calls_ts ON llm_calls(ts);
