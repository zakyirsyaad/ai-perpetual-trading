-- Generated from supabase/schema.sql by src/forward_schema.py (--only shadow_entries); do not edit by hand.
-- Adds the shadow_entries table to the existing 'long_forward' schema; every statement is safe to run again.

-- Hypothetical entries the live AI did not take, scored later against 1m candles.
-- `source` is shadow_side (a side the regime map does not allow) or ledger_block
-- (a setup bucket the ledger blocked). Observability only; nothing here places an order.
create table if not exists long_forward.shadow_entries (
    id bigint generated always as identity primary key,
    created_at timestamptz not null default now(),
    exchange text not null default 'bingx',
    decided_at timestamptz not null,
    symbol text not null,
    side text not null,
    bucket_key text not null,
    source text not null,
    ref_price double precision not null,
    stop_loss_pct double precision not null,
    floor_pct double precision not null,
    hold_minutes integer not null,
    gross_bps double precision,
    exit_reason text,
    evaluated_at timestamptz
);

create index if not exists shadow_entries_exchange_evaluated_decided_idx on long_forward.shadow_entries (exchange, evaluated_at, decided_at);

alter table long_forward.shadow_entries enable row level security;

grant usage on schema long_forward to service_role;
grant all on all tables in schema long_forward to service_role;
grant all on all sequences in schema long_forward to service_role;
