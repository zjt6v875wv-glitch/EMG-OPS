-- Phase 3: geocoding cache, territories/projects, capacity, leaderboard schedules.
create table geocode_cache (
  address_key text primary key,
  query text not null,
  lat double precision,
  lng double precision,
  confidence real,
  match_type text,                           -- rooftop | street | zip_centroid | none
  provider text not null,
  status text not null check (status in ('matched','ambiguous','unmatched','manual')),
  candidates jsonb not null default '[]',
  reviewed_by text,
  updated_at timestamptz not null default now()
);

create table territories (
  id uuid primary key default gen_random_uuid(),
  name text not null,
  kind text not null check (kind in ('territory','project')),
  vendor text,
  geojson jsonb not null,                    -- Polygon / MultiPolygon geometry
  capacity_kwh numeric(16,2),
  capacity_enabled boolean not null default false,
  active boolean not null default true,
  source_file_id uuid references source_files(id),
  created_by text not null,
  created_at timestamptz not null default now()
);

create table capacity_allocations (
  id uuid primary key default gen_random_uuid(),
  territory_id uuid not null references territories(id),
  vendor text not null,
  enrollment_id text not null,
  kwh numeric(14,3) not null,
  basis text not null check (basis in ('actual','estimate')),
  allocated_by text not null,
  allocated_at timestamptz not null default now(),
  released_by text,
  released_at timestamptz,
  note text
);
create unique index capacity_one_active on capacity_allocations(vendor, enrollment_id) where released_at is null;

create table leaderboard_schedules (
  id uuid primary key default gen_random_uuid(),
  name text not null,
  cadence text not null check (cadence in ('daily','weekly')),
  metrics text[] not null,
  filters jsonb not null default '{}',
  timezone text not null default 'America/New_York',
  destination jsonb,                         -- null = generate only; publishing needs an explicit destination
  enabled boolean not null default false,
  publish_enabled boolean not null default false,
  last_run_at timestamptz,
  created_by text not null,
  created_at timestamptz not null default now()
);
