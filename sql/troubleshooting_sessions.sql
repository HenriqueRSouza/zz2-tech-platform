-- Executed troubleshooting sessions (one technician walking one kit's tree).
-- Run once in the Supabase SQL Editor. Idempotent.
--
-- Written only by the backend with the service key. Browsers get no direct
-- access: RLS is enabled and no policy is granted, so the anon/authenticated
-- roles see nothing even with the publishable key.

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

-- No foreign key to "techSupport".kits: that table is a mirror that is pruned
-- and rebuilt by generation, and a cascade from it would silently delete
-- session history whenever a kit is renamed.

create index if not exists troubleshooting_sessions_kit_created_idx
  on "techSupport".troubleshooting_sessions (kit_key, created_at desc);
create index if not exists troubleshooting_sessions_ticket_idx
  on "techSupport".troubleshooting_sessions (ticket_number) where ticket_number <> '';
create index if not exists troubleshooting_sessions_tech_created_idx
  on "techSupport".troubleshooting_sessions (technician, created_at desc);

alter table "techSupport".troubleshooting_sessions enable row level security;
revoke all on "techSupport".troubleshooting_sessions from anon, authenticated;
grant all on "techSupport".troubleshooting_sessions to service_role;
