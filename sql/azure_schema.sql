-- Tech Support Platform — schema for Azure Database for PostgreSQL.
--
-- Replaces supabase_bootstrap.sql, supabase_setup.sql, supabase_auxiliary_tables.sql,
-- supabase_customer_history.sql and troubleshooting_sessions.sql. Idempotent: safe to
-- run again on every deploy.
--
--   psql "$ZZ2_DATABASE_URL" -f sql/azure_schema.sql
--   (or: python scripts/apply_schema.py)
--
-- There is no PostgREST, no row-level security and no browser access: only the
-- application server connects, with its own login. Authorization lives in the app.

-- gen_random_uuid() is built into PostgreSQL 13+; no extension needed.

create schema if not exists "techSupport";
create schema if not exists zoho;

-- -- Users (replaces Supabase Auth) ------------------------------------------
-- password_hash is null for accounts that only sign in with Microsoft.
create table if not exists "techSupport".app_users (
  id              uuid primary key default gen_random_uuid(),
  email           text not null,
  display_name    text not null default '',
  password_hash   text,
  role            text not null default 'tech',
  permissions     jsonb not null default '[]'::jsonb,
  active          boolean not null default true,
  microsoft_oid   text,
  created_at      timestamptz not null default now(),
  updated_at      timestamptz not null default now(),
  last_sign_in_at timestamptz,
  constraint app_users_permissions_is_array check (jsonb_typeof(permissions) = 'array')
);
create unique index if not exists app_users_email_key on "techSupport".app_users (lower(email));
create unique index if not exists app_users_microsoft_oid_key
  on "techSupport".app_users (microsoft_oid) where microsoft_oid is not null;

-- -- Tickets -----------------------------------------------------------------
create table if not exists "techSupport".tickets (
  id            bigint generated always as identity primary key,
  ticket_number text not null unique,
  ticket_date   date not null default current_date,
  tech          text,
  client        text,
  product       text,
  vehicle       text,
  issue         text,
  resolution    text,
  status        text,
  serial        text,
  notes         text,
  created_at    timestamptz not null default now()
);
create index if not exists tickets_ticket_date_idx on "techSupport".tickets (ticket_date);
create index if not exists tickets_client_lower_idx on "techSupport".tickets (lower(client));

-- -- Kit catalogue mirror ------------------------------------------------------
create table if not exists "techSupport".kits (
  kit_key text primary key,
  vehicle text not null default '',
  switch_button text not null default '',
  audio_method text not null default '',
  notes text not null default '',
  dip_image text not null default '',
  wiring_image text not null default '',
  misc_image text not null default '',
  manual text not null default '',
  local_updated_at bigint,
  sync_generation bigint not null,
  synced_at timestamptz not null default now()
);
create table if not exists "techSupport".troubleshooting (
  kit_key text primary key references "techSupport".kits(kit_key) on delete cascade,
  tree jsonb not null default '[]'::jsonb,
  sync_generation bigint not null,
  synced_at timestamptz not null default now()
);
create table if not exists "techSupport".blueprints (
  id text primary key,
  name text not null,
  troubleshooting jsonb not null default '[]'::jsonb,
  created_at bigint,
  updated_at bigint,
  sync_generation bigint not null,
  synced_at timestamptz not null default now()
);
create table if not exists "techSupport".manual_assets (
  kit_key text not null references "techSupport".kits(kit_key) on delete cascade,
  filename text not null,
  local_path text not null, -- Blob path: <kit_key>/<filename>
  size_bytes bigint not null default 0,
  sha256 text not null default '',
  sync_generation bigint not null,
  synced_at timestamptz not null default now(),
  primary key (kit_key, filename)
);
create index if not exists kits_sync_generation_idx on "techSupport".kits(sync_generation);
create index if not exists troubleshooting_sync_generation_idx
  on "techSupport".troubleshooting(sync_generation);
create index if not exists blueprints_sync_generation_idx
  on "techSupport".blueprints(sync_generation);
create index if not exists manual_assets_sync_generation_idx
  on "techSupport".manual_assets(sync_generation);

-- -- Troubleshooting sessions ----------------------------------------------------
create table if not exists "techSupport".troubleshooting_sessions (
  id               text primary key,
  kit_key          text not null,
  path             jsonb not null default '[]'::jsonb,
  checked_steps    jsonb not null default '[]'::jsonb,
  outcome          text not null default 'pending'
                   check (outcome in ('pending','resolved','unresolved','escalated','abandoned')),
  ticket_number    text not null default '',
  notes            text not null default '' check (char_length(notes) <= 4000),
  technician       text not null,
  technician_name  text not null default '',
  started_at       timestamptz,
  created_at       timestamptz not null default now(),
  updated_at       timestamptz not null default now(),
  duration_seconds integer check (duration_seconds is null or duration_seconds >= 0),
  constraint troubleshooting_sessions_path_is_array check (jsonb_typeof(path) = 'array'),
  constraint troubleshooting_sessions_checked_is_array check (jsonb_typeof(checked_steps) = 'array')
);
create index if not exists troubleshooting_sessions_kit_created_idx
  on "techSupport".troubleshooting_sessions (kit_key, created_at desc);
create index if not exists troubleshooting_sessions_ticket_idx
  on "techSupport".troubleshooting_sessions (ticket_number) where ticket_number <> '';
create index if not exists troubleshooting_sessions_tech_created_idx
  on "techSupport".troubleshooting_sessions (technician, created_at desc);

-- -- Customer history (read-only, loaded from Zoho) ------------------------------
create table if not exists zoho.customer_performance_semesters (
  customer_name    text not null,
  period_year      int  not null,
  semester         int  not null,
  period_start     date,
  period_end       date,
  sales_quantity   numeric not null default 0,
  returns_quantity numeric not null default 0,
  returns_rate     numeric,
  bcy_total        numeric,
  primary key (customer_name, period_year, semester)
);
create table if not exists zoho.customer_performance_months (
  customer_name    text not null,
  period_year      int  not null,
  month            int  not null,
  sales_quantity   numeric not null default 0,
  returns_quantity numeric not null default 0,
  returns_rate     numeric,
  bcy_total        numeric,
  primary key (customer_name, period_year, month)
);
create table if not exists zoho.sales_by_item (
  item_sku      text not null,
  item_name     text,
  quantity_sold numeric not null default 0,
  period_start  date not null,
  period_end    date,
  semester      text,
  source        text,
  primary key (item_sku, period_start)
);
