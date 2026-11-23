-- Demo customer history for development and presentations. NOT for production:
-- in production these tables are filled by the ERP (Zoho) export.
--
-- Run in the Supabase SQL Editor after supabase_bootstrap.sql. Safe to re-run:
-- it deletes and recreates exactly the customers and items listed below.
--
-- The names match the clients used by scripts/seed_demo_tickets.py, so the ticket
-- form's customer picker and history panel line up with the demo tickets.
-- Two customers are shaped to trip the returns alert on the ticket form:
--   Auto Center Paulista Ltda  latest return rate above 15%
--   Blindados Morumbi          returns in most recent months
--
-- Remove everything again with the DELETE statements at the end of this file.

begin;

create temporary table demo_customers (name text primary key, volume int, return_pct numeric)
  on commit drop;
insert into demo_customers values
  ('Multisom',                   140, 0.04),
  ('Som e Cia',                  120, 0.05),
  ('Auto Center Paulista Ltda',   70, 0.22),
  ('Car Audio SP',                85, 0.06),
  ('Prime Acessórios',            95, 0.03),
  ('Vitória Som Automotivo',      60, 0.05),
  ('Garage 77',                   45, 0.02),
  ('JR Instalações',              50, 0.07),
  ('Premium Car Tech',            40, 0.04),
  ('Blindados Morumbi',           25, 0.12),
  ('Elite Car Som',               30, 0.03),
  ('Cliente final - WhatsApp',    35, 0.08);

delete from zoho.customer_performance_semesters where customer_name in (select name from demo_customers);
delete from zoho.customer_performance_months    where customer_name in (select name from demo_customers);

-- Semesters: 2025-S1 to the current one. Volume grows each semester; a small
-- deterministic jitter per customer keeps the numbers from looking generated.
insert into zoho.customer_performance_semesters
  (customer_name, period_year, semester, period_start, period_end,
   sales_quantity, returns_quantity, returns_rate, bcy_total)
select c.name, s.y, s.sem, s.start, (s.start + interval '6 months' - interval '1 day')::date,
       q.sales, q.returns, round(q.returns / q.sales, 4), q.sales * 1890
from demo_customers c
cross join (values (2025, 1, date '2025-01-01', 0.70),
                   (2025, 2, date '2025-07-01', 0.85),
                   (2026, 1, date '2026-01-01', 1.00),
                   (2026, 2, date '2026-07-01', 0.75)) as s(y, sem, start, growth)
cross join lateral (
  select greatest(1, round(c.volume * s.growth * (0.9 + (abs(hashtext(c.name || s.y || s.sem)) % 20) / 100.0))) as sales
) v
cross join lateral (
  select v.sales::numeric as sales,
         round(v.sales * c.return_pct * (0.7 + (abs(hashtext(c.name || s.sem || s.y)) % 60) / 100.0))::numeric as returns
) q;

-- Months: the last 12, newest first in the app.
insert into zoho.customer_performance_months
  (customer_name, period_year, month, sales_quantity, returns_quantity, returns_rate, bcy_total)
select c.name, extract(year from m.d)::int, extract(month from m.d)::int,
       q.sales, q.returns, round(q.returns / q.sales, 4), q.sales * 1890
from demo_customers c
cross join generate_series(date_trunc('month', current_date) - interval '11 months',
                           date_trunc('month', current_date), interval '1 month') as m(d)
cross join lateral (
  select greatest(1, round(c.volume / 6.0 * (0.6 + (abs(hashtext(c.name || m.d::text)) % 80) / 100.0))) as sales
) v
cross join lateral (
  select v.sales::numeric as sales,
         (case
            when c.name = 'Auto Center Paulista Ltda' and m.d = date_trunc('month', current_date)
              then greatest(2, round(v.sales * 0.25))
            when c.name = 'Blindados Morumbi' and m.d >= date_trunc('month', current_date) - interval '3 months'
              then greatest(1, round(v.sales * 0.2))
            else round(v.sales * c.return_pct * (abs(hashtext(m.d::text || c.name)) % 100) / 60.0)
          end)::numeric as returns
) q;

-- Item sales: one row per kit per semester, matching the demo tickets' kits.
create temporary table demo_items (sku text primary key, name text, volume int) on commit drop;
insert into demo_items values
  ('ITZ-NTG4',   'CarPlay interface Mercedes NTG4',      210),
  ('ITZ-NTG3',   'CarPlay interface Mercedes NTG3',       90),
  ('IT3-NTG5',   'CarPlay interface Mercedes NTG5',      180),
  ('IT3-NBT',    'CarPlay interface BMW NBT',            175),
  ('ITZ-CCC',    'CarPlay interface BMW CCC',             55),
  ('IT3-AUDI',   'CarPlay interface Audi MMI',           150),
  ('ITZ-LR15-A', 'CarPlay interface Land Rover 2015+',    95),
  ('IT3-PCM31A', 'CarPlay interface Porsche PCM 3.1',     40),
  ('ZW-RAM',     'Wireless CarPlay RAM Uconnect',        110),
  ('ZW-GM',      'Wireless CarPlay GM MyLink',           160),
  ('IT3-VOLVO',  'CarPlay interface Volvo Sensus',        70),
  ('ITZ-TOY',    'CarPlay interface Toyota',             130),
  ('ITZ-JGC',    'CarPlay interface Jeep Grand Cherokee', 45),
  ('ITZ-SYNC2',  'CarPlay interface Ford SYNC 2',         80);

delete from zoho.sales_by_item where item_sku in (select sku from demo_items);

insert into zoho.sales_by_item
  (item_sku, item_name, quantity_sold, period_start, period_end, semester, source)
select i.sku, i.name,
       round(i.volume * s.growth * (0.85 + (abs(hashtext(i.sku || s.label)) % 30) / 100.0)),
       s.start, (s.start + interval '6 months' - interval '1 day')::date, s.label, 'demo'
from demo_items i
cross join (values ('2025-S1', date '2025-01-01', 0.70),
                   ('2025-S2', date '2025-07-01', 0.85),
                   ('2026-S1', date '2026-01-01', 1.00),
                   ('2026-S2', date '2026-07-01', 0.75)) as s(label, start, growth);

commit;

notify pgrst, 'reload schema';

-- To remove the demo data later:
-- delete from zoho.customer_performance_semesters where customer_name in ('Multisom','Som e Cia','Auto Center Paulista Ltda','Car Audio SP','Prime Acessórios','Vitória Som Automotivo','Garage 77','JR Instalações','Premium Car Tech','Blindados Morumbi','Elite Car Som','Cliente final - WhatsApp');
-- delete from zoho.customer_performance_months    where customer_name in ('Multisom','Som e Cia','Auto Center Paulista Ltda','Car Audio SP','Prime Acessórios','Vitória Som Automotivo','Garage 77','JR Instalações','Premium Car Tech','Blindados Morumbi','Elite Car Som','Cliente final - WhatsApp');
-- delete from zoho.sales_by_item where source = 'demo';
