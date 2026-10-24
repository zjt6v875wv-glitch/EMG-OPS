-- Phase 3: address enrichment, map points, published leaderboards.
alter table enrollments add column project text;              -- community-solar project from a status report
alter table enrollments add column address_source text;       -- file the service address came from
alter table enrollments add column origin text not null default 'payroll' check (origin in ('payroll','status_report'));

-- Enrollments created from a status report have no payroll sale date; written leaderboards ignore them.
create index enrollments_address on enrollments(address_source) where service_address is not null;

create table enrichment_batches (
  id uuid primary key default gen_random_uuid(),
  source_file_id uuid not null references source_files(id),
  kind text not null check (kind in ('status_report','address_mapping','coordinates')),
  vendor text,
  status text not null default 'previewed' check (status in ('previewed','applied','rejected')),
  summary jsonb not null default '{}',
  plan jsonb not null default '[]',                            -- per-row decision shown in the preview
  created_by text not null,
  created_at timestamptz not null default now(),
  applied_by text,
  applied_at timestamptz
);

create table leaderboard_runs (
  id uuid primary key default gen_random_uuid(),
  schedule_id uuid references leaderboard_schedules(id),
  cadence text not null,
  period_start date not null,
  period_end date not null,
  timezone text not null,
  filters jsonb not null default '{}',
  result jsonb not null,
  files jsonb not null default '[]',                           -- [{kind, file_name, storage_key, sha256}]
  published boolean not null default false,
  publish_result jsonb,
  created_by text not null,
  created_at timestamptz not null default now()
);
create unique index leaderboard_runs_once on leaderboard_runs(schedule_id, period_start) where schedule_id is not null;

alter table leaderboard_schedules add column run_hour int not null default 8 check (run_hour between 0 and 23);
alter table leaderboard_schedules add column run_weekday int not null default 0 check (run_weekday between 0 and 6);  -- weekly: 0 = Monday
alter table leaderboard_schedules add column top_n int not null default 10 check (top_n between 1 and 100);
alter table leaderboard_schedules add column groups text[] not null default '{agent,manager}';
