-- EMG Ops core schema. Money is numeric (never float). Every table that
-- holds business facts is append-friendly; corrections are new rows.
create extension if not exists pgcrypto;
create extension if not exists citext;

create table users (
  id uuid primary key default gen_random_uuid(),
  email citext not null unique,
  name text not null,
  role text not null check (role in ('owner','admin','payroll','manager','agent')),
  person_name text,                          -- links manager/agent logins to their payroll identity
  password_hash text not null,
  active boolean not null default true,
  created_at timestamptz not null default now()
);

create table sessions (
  token_hash text primary key,
  user_id uuid not null references users(id) on delete cascade,
  created_at timestamptz not null default now(),
  expires_at timestamptz not null
);

create table audit_log (
  id bigserial primary key,
  at timestamptz not null default now(),
  actor text not null,
  action text not null,
  entity text not null,
  entity_id text,
  before jsonb,
  after jsonb,
  note text
);
create index audit_log_entity on audit_log(entity, entity_id);

-- People and identities ---------------------------------------------------
create table people (
  id uuid primary key default gen_random_uuid(),
  name text not null unique,                 -- canonical payroll identity; one debt ledger per person
  kind text not null default 'agent' check (kind in ('agent','manager','company')),
  email text,
  email_verified boolean not null default false,
  email_excluded boolean not null default false,
  email_status text,
  status text not null default 'active',
  merged_into text references people(name),  -- explicit merges only
  notes text,
  created_at timestamptz not null default now()
);

-- Versioned rules -----------------------------------------------------------
create table rule_sets (
  id uuid primary key default gen_random_uuid(),
  version integer not null unique,
  label text not null,
  config jsonb not null,                     -- full snapshot; see engine/emg/rules/model.py
  status text not null check (status in ('draft','active','superseded','discarded')),
  parent_version integer,
  change_note text,
  created_by text not null,
  created_at timestamptz not null default now(),
  activated_by text,
  activated_at timestamptz
);
create unique index rule_sets_one_active on rule_sets(status) where status = 'active';

-- Files and imports ----------------------------------------------------------
create table source_files (
  id uuid primary key default gen_random_uuid(),
  sha256 text not null unique,               -- identical uploads are stored once, unchanged
  file_name text not null,
  size_bytes bigint not null,
  mime text,
  storage_key text not null,
  uploaded_by text not null,
  uploaded_at timestamptz not null default now()
);

create table import_batches (
  id uuid primary key default gen_random_uuid(),
  source_file_id uuid not null references source_files(id),
  importer text not null,
  vendor text not null,
  week text,                                 -- payroll week the file belongs to (YYYY-Www)
  status text not null check (status in ('preview','committed','rejected','superseded')),
  summary jsonb not null default '{}',
  errors jsonb not null default '[]',
  warnings jsonb not null default '[]',
  excluded jsonb not null default '[]',
  created_by text not null,
  created_at timestamptz not null default now(),
  committed_by text,
  committed_at timestamptz
);

create table import_rows (
  id bigserial primary key,
  batch_id uuid not null references import_batches(id) on delete cascade,
  row_no integer not null,
  line_type text not null,
  vendor text not null,
  enrollment_id text,
  transaction_key text,
  payload jsonb not null,
  duplicate_of bigint,                       -- set on commit when the same money line already exists
  unique (batch_id, row_no)
);
create index import_rows_batch_type on import_rows(batch_id, line_type);

-- One row per money line ever committed; re-importing the same line is refused.
create table invoice_lines (
  id bigserial primary key,
  vendor text not null,
  line_type text not null,
  transaction_key text not null,
  enrollment_id text not null,
  invoice_number text,
  invoice_date date,
  amount numeric(14,4) not null default 0,
  import_row_id bigint not null references import_rows(id),
  created_at timestamptz not null default now(),
  unique (vendor, line_type, transaction_key)
);

create table enrollments (
  id uuid primary key default gen_random_uuid(),
  vendor text not null,
  enrollment_id text not null,
  customer_id text,
  customer_name text,
  phone text,
  service_address text,
  city text,
  state text,
  zip text,
  utility text,
  product text,
  premise text,
  raw_rep text,
  seller text,
  sale_date date,
  approval_date date,
  approval_date_source text check (approval_date_source in ('vendor','invoice_proxy')),
  invoice_date date,
  kwh numeric(14,3),
  units numeric(10,2),
  status text,
  status_reason text,
  lat double precision,
  lng double precision,
  geocode_status text,
  geocode_confidence real,
  updated_at timestamptz not null default now(),
  created_at timestamptz not null default now(),
  unique (vendor, enrollment_id)
);
create index enrollments_sale_date on enrollments(sale_date);
create index enrollments_approval_date on enrollments(approval_date);
create index enrollments_seller on enrollments(seller);

create table enrollment_status_history (
  id bigserial primary key,
  enrollment_pk uuid not null references enrollments(id) on delete cascade,
  status text not null,
  reason text,
  observed_at timestamptz not null default now(),
  import_batch_id uuid references import_batches(id),
  source_file text,
  source_row integer
);

-- Payroll ---------------------------------------------------------------------
create table payroll_runs (
  id uuid primary key default gen_random_uuid(),
  week text not null,
  revision integer not null,
  status text not null check (status in ('calculated','finalized','superseded','discarded')),
  rule_set_version integer not null,
  batch_ids uuid[] not null default '{}',
  inputs_hash text not null,
  totals jsonb not null default '{}',
  reconciliation jsonb not null default '{}',
  supersedes uuid references payroll_runs(id),
  correction_reason text,
  created_by text not null,
  created_at timestamptz not null default now(),
  finalized_by text,
  finalized_at timestamptz,
  finalize_key text unique,
  unique (week, revision)
);
create unique index payroll_runs_one_final on payroll_runs(week) where status = 'finalized';

create table run_lines (
  id bigserial primary key,
  run_id uuid not null references payroll_runs(id) on delete cascade,
  payee text not null,
  activity text not null,
  vendor text not null,
  rep text,
  source_rep text,
  enrollment_id text,
  transaction_id text,
  customer text,
  utility text,
  product text,
  kwh numeric(14,3),
  units numeric(10,2),
  rate numeric(12,6),
  amount numeric(16,6) not null,
  sale_date date,
  rule_id text,
  notes text
);
create index run_lines_run_payee on run_lines(run_id, payee);

create table run_people (
  run_id uuid not null references payroll_runs(id) on delete cascade,
  payee text not null,
  final_check numeric(14,2) not null,
  status text not null,
  data jsonb not null,
  primary key (run_id, payee)
);

create table run_chargebacks (
  id bigserial primary key,
  run_id uuid not null references payroll_runs(id) on delete cascade,
  payee text not null,
  vendor text not null,
  enrollment_id text not null,
  amount numeric(14,4) not null,
  verified boolean not null,
  data jsonb not null
);

create table run_items (
  id bigserial primary key,
  run_id uuid not null references payroll_runs(id) on delete cascade,
  kind text not null check (kind in ('nonpayable','pending','company_adjustment')),
  payee text,
  vendor text,
  enrollment_id text,
  data jsonb not null
);

-- One-week facts (never rules): advances, room charges, holds, protected checks...
create table week_adjustments (
  id uuid primary key default gen_random_uuid(),
  week text not null,
  person text not null,
  kind text not null check (kind in ('new_advance','room_charge','flight_charge','other_charge','collection','defer','protected_check','manual_hold','cash_credit','reserve')),
  amount numeric(14,2) not null default 0,
  method text,
  value numeric(14,4) not null default 0,
  debts text[] not null default '{}',
  creditor text not null default 'EMG',
  label text,
  note text,
  authorized_by text not null,
  external_ref text,                         -- e.g. transfer screenshot id; prevents duplicate advances
  created_by text not null,
  created_at timestamptz not null default now(),
  voided_by text,
  voided_at timestamptz
);
create unique index week_adjustments_ref on week_adjustments(kind, person, external_ref) where external_ref is not null and voided_at is null;

-- Debt ledgers: one account per (person, debt type, creditor). Balance = sum(amount).
create table ledger_entries (
  id bigserial primary key,
  person text not null,
  debt_type text not null check (debt_type in ('chargeback','advance','room','flight','other','reserve','unapplied_hold')),
  creditor text not null default 'EMG',
  week text not null,
  run_id uuid references payroll_runs(id),
  entry_type text not null check (entry_type in ('opening','assessed','deducted','payment_confirmed','reversal','adjustment')),
  amount numeric(14,2) not null,             -- +increases what the person owes, -reduces it
  reference text,
  note text,
  idempotency_key text not null unique,
  created_by text not null,
  created_at timestamptz not null default now()
);
create index ledger_entries_account on ledger_entries(person, debt_type, creditor);

-- Every payout made by a finalized run, used to reverse chargebacks exactly.
create table payout_evidence (
  id bigserial primary key,
  vendor text not null,
  enrollment_id text not null,
  payee text not null,
  activity text not null,
  amount numeric(16,6) not null,
  rate numeric(12,6),
  week text not null,
  run_id uuid references payroll_runs(id),
  source text not null check (source in ('payroll_run','historical_import','manual')),
  proof text,
  active boolean not null default true,
  created_by text not null,
  created_at timestamptz not null default now()
);
create index payout_evidence_ref on payout_evidence(vendor, enrollment_id) where active;

-- Chargeback references already reversed (prevents double deduction when a
-- vendor repeats a chargeback in a later file).
create table processed_chargebacks (
  vendor text not null,
  enrollment_id text not null,
  week text not null,
  run_id uuid references payroll_runs(id),
  source text not null check (source in ('payroll_run','historical_import')),
  note text,
  active boolean not null default true,
  created_at timestamptz not null default now()
);
create unique index processed_chargebacks_active on processed_chargebacks(vendor, enrollment_id) where active;

-- Calculated payroll is not a payment. Payments move separately.
create table payments (
  id uuid primary key default gen_random_uuid(),
  run_id uuid not null references payroll_runs(id),
  person text not null,
  amount numeric(14,2) not null,
  state text not null check (state in ('calculated','submitted','confirmed','failed','void')),
  provider_ref text,
  updated_by text,
  updated_at timestamptz not null default now(),
  unique (run_id, person)
);

create table review_items (
  id uuid primary key default gen_random_uuid(),
  scope_key text not null unique,            -- stable key so recalculation does not duplicate items
  week text,
  run_id uuid references payroll_runs(id) on delete set null,
  batch_id uuid references import_batches(id) on delete set null,
  kind text not null,
  message text not null,
  vendor text,
  enrollment_id text,
  person text,
  amount numeric(14,4) not null default 0,
  blocking boolean not null default true,
  status text not null default 'open' check (status in ('open','resolved','dismissed')),
  resolution text,
  resolved_by text,
  resolved_at timestamptz,
  data jsonb not null default '{}',
  created_at timestamptz not null default now()
);

-- Background jobs (Postgres-backed queue; worker uses SKIP LOCKED).
create table jobs (
  id bigserial primary key,
  kind text not null,
  payload jsonb not null default '{}',
  status text not null default 'queued' check (status in ('queued','running','done','failed')),
  attempts integer not null default 0,
  run_after timestamptz not null default now(),
  locked_by text,
  locked_at timestamptz,
  result jsonb,
  error text,
  created_by text,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);
create index jobs_queue on jobs(status, run_after);
