-- Run once in the Supabase SQL Editor for the project used by this app.
-- The custom schema must also remain listed in API Settings > Exposed schemas.

grant usage on schema "techSupport" to authenticated;
grant select, insert, update on table "techSupport".tickets to authenticated;
grant usage, select on all sequences in schema "techSupport" to authenticated;

alter table "techSupport".tickets enable row level security;

do $$
begin
  if not exists (
    select 1 from pg_policies
    where schemaname = 'techSupport'
      and tablename = 'tickets'
      and policyname = 'authenticated users can read tickets'
  ) then
    create policy "authenticated users can read tickets"
      on "techSupport".tickets for select
      to authenticated using (true);
  end if;

  if not exists (
    select 1 from pg_policies
    where schemaname = 'techSupport'
      and tablename = 'tickets'
      and policyname = 'authenticated users can create tickets'
  ) then
    create policy "authenticated users can create tickets"
      on "techSupport".tickets for insert
      to authenticated with check (true);
  end if;

  if not exists (
    select 1 from pg_policies
    where schemaname = 'techSupport'
      and tablename = 'tickets'
      and policyname = 'authenticated users can update tickets'
  ) then
    create policy "authenticated users can update tickets"
      on "techSupport".tickets for update
      to authenticated using (true) with check (true);
  end if;
end
$$;

-- UPDATE events include the primary-key identity needed by the browser.
alter table "techSupport".tickets replica identity default;

do $$
begin
  alter publication supabase_realtime add table "techSupport".tickets;
exception
  when duplicate_object then null;
end
$$;
