-- Creates the schemas and tables the other scripts in this directory assume
-- already exist. Run once, FIRST, in the Supabase SQL Editor of a new project.
--
-- Order for a new project:
--   1. supabase_bootstrap.sql          (this file)
--   2. supabase_setup.sql              tickets: grants and row-level security
--   3. supabase_auxiliary_tables.sql   catalogue mirror (ZZ2_ENABLE_SUPABASE_MIRROR)
--   4. supabase_customer_history.sql   read access to the zoho schema
--
-- Then add "techSupport" and "zoho" to Project Settings > Data API >
-- Exposed schemas, or every request returns PGRST106.
--
-- Safe to re-run: everything is "if not exists".

-- -- Application schema --------------------------------------------------------
create schema if not exists "techSupport";

-- Columns match Ticket.to_supabase / Ticket.from_supabase in zz2/domain/models.py.
-- ticket_number is the row's identity, stored as "TKT-<digits>".
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

-- The server uses the service key for every write.
grant usage on schema "techSupport" to service_role;
grant all on all tables in schema "techSupport" to service_role;
grant usage, select on all sequences in schema "techSupport" to service_role;
alter default privileges in schema "techSupport" grant all on tables to service_role;
alter default privileges in schema "techSupport" grant usage, select on sequences to service_role;

-- -- Customer history (read-only for the app) -----------------------------------
-- In production these are filled by the ERP (Zoho) export, not by this app. Empty
-- tables are enough for the Tickets page to load; customer history then shows
-- "no data" instead of an error.
create schema if not exists zoho;

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

notify pgrst, 'reload schema';
