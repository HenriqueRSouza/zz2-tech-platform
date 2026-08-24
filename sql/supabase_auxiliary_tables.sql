-- Local-authoritative auxiliary data mirrored to Supabase for backup/reporting.
-- Run once in the Supabase SQL Editor.

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

-- PDF contents live in the private zz2-manuals Storage bucket. This table
-- mirrors metadata and the object path for catalogue backup/reporting.
create table if not exists "techSupport".manual_assets (
  kit_key text not null references "techSupport".kits(kit_key) on delete cascade,
  filename text not null,
  local_path text not null, -- Storage object path: <kit_key>/<filename>
  size_bytes bigint not null default 0,
  sha256 text not null default '',
  sync_generation bigint not null,
  synced_at timestamptz not null default now(),
  primary key (kit_key, filename)
);

grant usage on schema "techSupport" to authenticated;
grant select on "techSupport".kits to authenticated;
grant select on "techSupport".troubleshooting to authenticated;
grant select on "techSupport".blueprints to authenticated;
grant select on "techSupport".manual_assets to authenticated;

alter table "techSupport".kits enable row level security;
alter table "techSupport".troubleshooting enable row level security;
alter table "techSupport".blueprints enable row level security;
alter table "techSupport".manual_assets enable row level security;

do $$
declare
  item record;
begin
  for item in select * from (values
    ('kits'), ('troubleshooting'), ('blueprints'), ('manual_assets')
  ) as names(table_name)
  loop
    if not exists (
      select 1 from pg_policies
      where schemaname='techSupport' and tablename=item.table_name
        and policyname=('authenticated users can read ' || item.table_name)
    ) then
      execute format(
        'create policy %I on "techSupport".%I for select to authenticated using (true)',
        'authenticated users can read ' || item.table_name,
        item.table_name
      );
    end if;
  end loop;
end
$$;

create index if not exists kits_sync_generation_idx
  on "techSupport".kits(sync_generation);
create index if not exists troubleshooting_sync_generation_idx
  on "techSupport".troubleshooting(sync_generation);
create index if not exists blueprints_sync_generation_idx
  on "techSupport".blueprints(sync_generation);
create index if not exists manual_assets_sync_generation_idx
  on "techSupport".manual_assets(sync_generation);
