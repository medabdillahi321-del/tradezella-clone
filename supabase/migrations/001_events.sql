-- Copy of the local event log, in Supabase (PostgreSQL).
-- NOT APPLIED YET: run only after you approve the Supabase project and its cost.
--
-- Same columns as the local SQLite log. payload_json keeps the exact text that was
-- hashed, so the hash chain can be re-checked here too; payload is the same data
-- as jsonb for querying.

create table if not exists public.events (
    seq          bigint      not null unique,
    id           uuid        primary key,
    kind         text        not null,
    ts_utc       timestamptz not null,
    trading_day  date        not null,
    payload_json text        not null,
    payload      jsonb       generated always as (payload_json::jsonb) stored,
    idem_key     text        unique,
    prev_hash    text        not null,
    hash         text        not null,
    recorded_utc timestamptz not null,
    received_at  timestamptz not null default now(),
    constraint events_kind_namespace check (kind ~ '^(market|shadow|risk|review|system)\.[a-z0-9_.]+$')
);

create index if not exists events_kind_ts on public.events (kind, ts_utc);
create index if not exists events_day on public.events (trading_day);

-- Append-only, even for the database owner's API key.
create or replace function public.events_append_only() returns trigger
language plpgsql as $$
begin
    raise exception 'events are append-only';
end $$;

drop trigger if exists events_no_update on public.events;
create trigger events_no_update before update or delete on public.events
for each row execute function public.events_append_only();

drop trigger if exists events_no_truncate on public.events;
create trigger events_no_truncate before truncate on public.events
for each statement execute function public.events_append_only();

-- Row-level security on, with no policies: the public (publishable) key cannot
-- read or write this table. The sync on your computer uses the secret key, which
-- bypasses row-level security but is still stopped by the append-only trigger.
-- Keep the secret key only on your computer, never in the browser or in git.
alter table public.events enable row level security;
