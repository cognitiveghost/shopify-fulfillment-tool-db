-- Core tables. Every table: version/updated_at/updated_by, RLS for authenticated only.
create table public.clients (
  client_id text primary key,
  name text,
  archived boolean not null default false,
  config jsonb,
  shopify_config jsonb,
  packer_config jsonb
);

create table public.client_config_history (
  id bigserial primary key,
  client_id text not null references public.clients on delete cascade,
  kind text not null,
  body jsonb,
  saved_at timestamptz not null default now()
);
create index on public.client_config_history (client_id, kind, saved_at desc);

create table public.groups (
  id int primary key default 1 check (id = 1),
  body jsonb
);

create table public.fulfillment_history (
  client_id text not null references public.clients on delete cascade,
  row_no int not null,
  order_number text,
  row jsonb,
  primary key (client_id, row_no)
);
create index on public.fulfillment_history (client_id, order_number);

create table public.fulfillment_history_meta (
  client_id text primary key references public.clients on delete cascade
);

create table public.sessions (
  client_id text not null references public.clients on delete cascade,
  name text not null,
  status text,
  info jsonb,
  archived boolean not null default false,
  primary key (client_id, name)
);

create table public.session_documents (
  client_id text not null,
  session_name text not null,
  path text not null,
  body jsonb,
  primary key (client_id, session_name, path),
  foreign key (client_id, session_name) references public.sessions on delete cascade
);

create table public.packing_lists (
  client_id text not null,
  session_name text not null,
  name text not null,
  body jsonb,
  withdrawn boolean not null default false,
  fingerprint text,
  primary key (client_id, session_name, name),
  foreign key (client_id, session_name) references public.sessions on delete cascade
);

create table public.packing_state (
  client_id text not null,
  session_name text not null,
  list_name text not null,
  state jsonb,
  summary jsonb,
  primary key (client_id, session_name, list_name),
  foreign key (client_id, session_name) references public.sessions on delete cascade
);

-- no FK: a lock can be taken before/independently of the rows it guards
create table public.list_locks (
  client_id text not null,
  session_name text not null,
  list_name text not null,
  holder_pc text not null,
  holder_user text,
  heartbeat_at timestamptz not null default now(),
  acquired_at timestamptz not null default now(),
  primary key (client_id, session_name, list_name)
);
create index on public.list_locks (heartbeat_at);

create table public.workers (
  id int primary key default 1 check (id = 1),
  body jsonb
);

create table public.stats (
  id int primary key default 1 check (id = 1),
  body jsonb
);

-- bump updated_at on every update
create function public.touch_updated_at() returns trigger
language plpgsql security invoker set search_path = '' as $$
begin
  new.updated_at := now();
  return new;
end $$;

-- common columns, RLS, authenticated-only policy, trigger
do $$
declare t text;
begin
  foreach t in array array['clients','client_config_history','groups','fulfillment_history',
    'fulfillment_history_meta','sessions','session_documents','packing_lists','packing_state',
    'list_locks','workers','stats']
  loop
    execute format('alter table public.%I add column version bigint not null default 1,
      add column updated_at timestamptz not null default now(), add column updated_by text', t);
    execute format('alter table public.%I enable row level security', t);
    execute format('revoke all on public.%I from anon', t);
    execute format('create policy "authenticated all" on public.%I for all to authenticated
      using ((select auth.role()) = ''authenticated'')
      with check ((select auth.role()) = ''authenticated'')', t);
    execute format('create trigger touch before update on public.%I
      for each row execute function public.touch_updated_at()', t);
  end loop;
end $$;
revoke all on sequence public.client_config_history_id_seq from anon;
