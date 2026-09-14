-- grants.sql
-- Two roles. The application connects as `service_role` (Supabase managed)
-- for reads and non-journal writes. It connects as `journal_writer` for
-- journal inserts. The journal_writer role can INSERT and read back the
-- `id` of what it just inserted (needed for `INSERT ... RETURNING id`,
-- which Postgres requires column-level SELECT for); it cannot read row
-- content, UPDATE, or DELETE. The writer (app/journal/writer.py) caches
-- the last hash per session in memory, so the app reads the chain's
-- tail via the admin connection instead (only needed once, at cold
-- start / cache miss) -- journal_writer never needs broader SELECT.

-- Run as a privileged role (e.g. `postgres` on the Supabase DB). Replace
-- '<password>' with a freshly generated secret -- never commit a real
-- password here; this file is a template, not a record of the live one.
-- Applied and verified live on 2026-09-14 (see conversation/plan history).

create role journal_writer with login password '<password>';

grant connect on database postgres to journal_writer;
grant usage on schema public to journal_writer;

grant insert on table
  journal_entries,
  trades,
  risk_events,
  audit_log
to journal_writer;

grant usage on sequence
  journal_entries_id_seq,
  trades_id_seq,
  risk_events_id_seq,
  audit_log_id_seq
to journal_writer;

-- Column-level SELECT on just `id`, for `INSERT ... RETURNING id`.
grant select (id) on journal_entries, trades, risk_events, audit_log
to journal_writer;

revoke update, delete, truncate on
  journal_entries, trades, risk_events, audit_log
from journal_writer;
