-- Run once in the Supabase SQL Editor for the project configured in config.json.
-- The `zoho` schema must also be listed under API Settings > Exposed schemas.
--
-- Customer performance data is read only by server.py with the server-only
-- secret/service key. Browser roles intentionally receive no access here.

grant usage on schema zoho to service_role;
grant select on table zoho.customer_performance_semesters to service_role;
grant select on table zoho.customer_performance_months to service_role;
grant select on table zoho.sales_by_item to service_role;

-- Make PostgREST pick up the permission immediately.
notify pgrst, 'reload schema';
