-- Vendor portal / API connectors that keep enrollment statuses current for leaderboards.
alter table enrollments drop constraint enrollments_origin_check;
alter table enrollments add constraint enrollments_origin_check check (origin in ('payroll','status_report','portal'));

create table connectors (
  id uuid primary key default gen_random_uuid(),
  name text not null unique,
  vendor text not null,
  kind text not null check (kind in ('portal','api')),
  config jsonb not null default '{}',          -- portal: {allowed_hosts, steps, lookback_days}; api: {provider, ...}
  mapping jsonb not null default '{}',         -- target field -> source column / JSON key
  run_hours int[] not null default '{6}',      -- local hours (business timezone) to run
  enabled boolean not null default false,
  credentials bytea,                           -- encrypted with CONNECTOR_SECRET_KEY; never returned by the API
  credentials_updated_at timestamptz,
  alert_destination text,                      -- leaderboard destination name for failure alerts
  created_by text not null,
  created_at timestamptz not null default now()
);

create table connector_runs (
  id uuid primary key default gen_random_uuid(),
  connector_id uuid not null references connectors(id) on delete cascade,
  trigger text not null check (trigger in ('schedule','manual')),
  status text not null default 'running' check (status in ('running','ok','failed')),
  started_at timestamptz not null default now(),
  finished_at timestamptz,
  source_file_id uuid references source_files(id),
  summary jsonb not null default '{}',
  error text,
  screenshot_key text,                         -- page at the moment of failure (admins only)
  created_by text not null
);
create index connector_runs_recent on connector_runs(connector_id, started_at desc);
