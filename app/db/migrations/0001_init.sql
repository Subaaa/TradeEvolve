-- 0001_init.sql
-- Core schema for the nito-trade-research agent.
-- All tables are append-only; only `system_state` is updated in normal operation.
-- RLS is enabled on all tables; the application uses two roles:
--   * service role: full access for reads and non-journal writes.
--   * journal_writer role: INSERT-only on the journal tables.

create extension if not exists "uuid-ossp";
create extension if not exists "pgcrypto";

-- --- Strategy versions -----------------------------------------------------
create table if not exists strategy_versions (
  id                  bigserial primary key,
  version_tag         text unique not null,
  parent_version_id   bigint references strategy_versions(id),
  created_at          timestamptz not null default now(),
  created_by          text not null,
  config              jsonb not null,
  config_hash         bytea not null,
  status              text not null
);
create index if not exists strategy_versions_status_idx on strategy_versions(status);
create index if not exists strategy_versions_parent_idx on strategy_versions(parent_version_id);

-- --- Hypotheses -----------------------------------------------------------
create table if not exists hypotheses (
  id                  bigserial primary key,
  weekly_review_id    bigint,
  proposed_at         timestamptz not null default now(),
  proposed_field      text not null,
  from_value          jsonb not null,
  to_value            jsonb not null,
  reason              text
);

-- --- Weekly reviews -------------------------------------------------------
create table if not exists weekly_reviews (
  id                  bigserial primary key,
  period_start        timestamptz not null,
  period_end          timestamptz not null,
  summary             text not null,
  failure_patterns    jsonb,
  raw                 jsonb
);

-- --- Experiments ----------------------------------------------------------
create table if not exists experiments (
  id                  bigserial primary key,
  hypothesis_id       bigint references hypotheses(id),
  champion_id         bigint not null references strategy_versions(id),
  challenger_id       bigint not null references strategy_versions(id),
  created_at          timestamptz not null default now(),
  phases              jsonb not null,
  final_decision      text,
  reason_codes        text[] not null default '{}'
);

-- --- Daily reports --------------------------------------------------------
create table if not exists daily_reports (
  id                  bigserial primary key,
  day                 date unique not null,
  net_pnl             numeric(18, 8) not null,
  num_trades          int not null,
  num_rejections       int not null,
  sharpe              numeric(18, 8),
  max_drawdown        numeric(18, 8),
  payload             jsonb not null
);

-- --- Candles --------------------------------------------------------------
create table if not exists candles (
  instrument          text not null,
  granularity         text not null,
  ts                  timestamptz not null,
  open                numeric(18, 8) not null,
  high                numeric(18, 8) not null,
  low                 numeric(18, 8) not null,
  close               numeric(18, 8) not null,
  volume              bigint,
  primary key (instrument, granularity, ts)
);

-- --- News -----------------------------------------------------------------
create table if not exists news_items (
  id                  bigserial primary key,
  url                 text not null,
  source              text not null,
  published_at        timestamptz,
  received_at         timestamptz not null default now(),
  title               text not null,
  body                text not null,
  dedup_hash          text unique not null,
  cluster_id          bigint
);
create index if not exists news_items_received_idx on news_items(received_at desc);

create table if not exists news_classifications (
  news_id             bigint primary key references news_items(id) on delete cascade,
  classified_at       timestamptz not null default now(),
  category            text not null,
  asset_tags          text[] not null,
  sentiment           numeric(4, 3) not null,
  confidence          numeric(4, 3) not null,
  is_fact             boolean not null
);

-- --- Journal (append-only) ------------------------------------------------
create table if not exists journal_entries (
  id                  bigserial primary key,
  ts                  timestamptz not null default now(),
  kind                text not null,
  strategy_version_id bigint,
  session_id          uuid not null,
  prev_state          text,
  next_state          text,
  reason_codes        text[] not null default '{}',
  payload             jsonb not null,
  hash                bytea not null,
  prev_hash           bytea
);
create index if not exists journal_entries_session_idx on journal_entries(session_id);
create index if not exists journal_entries_kind_ts_idx on journal_entries(kind, ts desc);

create table if not exists trades (
  id                       bigserial primary key,
  strategy_version_id      bigint not null references strategy_versions(id),
  opened_at                timestamptz not null,
  closed_at                timestamptz,
  side                     text not null,
  units                    bigint not null,
  entry                    numeric(18, 8) not null,
  sl                       numeric(18, 8),
  tp                       numeric(18, 8),
  exit                     numeric(18, 8),
  pnl_usd                  numeric(18, 8),
  pnl_r                    numeric(18, 8),
  mae                      numeric(18, 8),
  mfe                      numeric(18, 8),
  exit_reason              text,
  journal_entry_open_id    bigint references journal_entries(id),
  journal_entry_close_id   bigint references journal_entries(id)
);

create table if not exists risk_events (
  id                  bigserial primary key,
  ts                  timestamptz not null default now(),
  type                text not null,
  limit_name          text not null,
  value               numeric(18, 8) not null,
  action              text not null
);

create table if not exists audit_log (
  id                  bigserial primary key,
  ts                  timestamptz not null default now(),
  actor               text not null,
  action              text not null,
  details             jsonb not null
);

-- --- System state (only table that is updated in normal operation) -------
create table if not exists system_state (
  key                 text primary key,
  value               jsonb not null,
  updated_at          timestamptz not null default now()
);
