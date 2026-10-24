-- Phase 2: report versions and email distribution.
create table report_versions (
  id uuid primary key default gen_random_uuid(),
  run_id uuid not null references payroll_runs(id),
  week text not null,
  person text,                               -- null for package-level artifacts
  kind text not null check (kind in ('agent_pdf','combined_pdf','reports_zip','master_xlsx','package_zip','payable_csv','paychex_csv','aeg_workbook','aeg_invoice_pdf','leaderboard_png','leaderboard_pdf')),
  revision integer not null default 1,       -- payroll revision label (Rev 2 after a correction)
  file_name text not null,
  storage_key text not null,
  sha256 text not null,
  size_bytes bigint not null,
  created_by text not null,
  created_at timestamptz not null default now(),
  current boolean not null default true
);
create unique index report_versions_current on report_versions(run_id, coalesce(person, ''), kind) where current;

create table email_exclusions (
  person text primary key,
  reason text not null,
  created_by text not null,
  created_at timestamptz not null default now()
);

create table email_sends (
  id uuid primary key default gen_random_uuid(),
  run_id uuid not null references payroll_runs(id),
  person text not null,
  recipient text not null,
  report_version_id uuid not null references report_versions(id),
  report_sha256 text not null,
  idempotency_key text not null unique,      -- run + person + report hash: retries never send twice
  status text not null check (status in ('authorized','sending','sent','delivered','bounced','failed','unknown')),
  provider text not null,
  provider_message_id text,
  error text,
  authorized_by text not null,
  authorized_at timestamptz not null default now(),
  sent_at timestamptz,
  updated_at timestamptz not null default now()
);
