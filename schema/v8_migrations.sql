create extension if not exists pgcrypto;

create table if not exists circuit_breaker_events (
    id uuid default gen_random_uuid() primary key,
    timestamp timestamptz default now(),
    trigger_level text not null,
    portfolio_drawdown double precision,
    action_taken text not null,
    details jsonb,
    created_at timestamptz default now()
);

create index if not exists idx_circuit_breaker_events_ts
    on circuit_breaker_events (coalesce(created_at, timestamp) desc);

create table if not exists health_snapshots (
    id uuid default gen_random_uuid() primary key,
    timestamp timestamptz default now(),
    component text not null,
    status text not null,
    details jsonb,
    latency_ms integer,
    created_at timestamptz default now()
);

create index if not exists idx_health_snapshots_component_ts
    on health_snapshots (component, coalesce(created_at, timestamp) desc);
