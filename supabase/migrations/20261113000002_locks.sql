-- Lock + history RPCs. security invoker: RLS applies to the caller.
create function public.acquire_list_lock(
  p_client text, p_session text, p_list text, p_pc text, p_user text,
  p_stale_seconds int default 120) returns jsonb
language plpgsql security invoker set search_path = '' as $$
declare r public.list_locks;
begin
  -- single statement: free or same PC wins; a stale holder is NOT taken here
  insert into public.list_locks as l (client_id, session_name, list_name, holder_pc, holder_user)
  values (p_client, p_session, p_list, p_pc, p_user)
  on conflict (client_id, session_name, list_name) do update
    set heartbeat_at = now(), holder_user = excluded.holder_user
    where l.holder_pc = excluded.holder_pc
  returning * into r;
  if found then
    return jsonb_build_object('acquired', true, 'holder_pc', r.holder_pc,
      'holder_user', r.holder_user, 'heartbeat_at', r.heartbeat_at);
  end if;
  select * into r from public.list_locks
   where client_id = p_client and session_name = p_session and list_name = p_list;
  return jsonb_build_object('acquired', false, 'holder_pc', r.holder_pc,
    'holder_user', r.holder_user, 'heartbeat_at', r.heartbeat_at);
end $$;

create function public.force_take_list_lock(
  p_client text, p_session text, p_list text, p_pc text, p_user text,
  p_expected_heartbeat timestamptz, p_stale_seconds int default 120) returns boolean
language plpgsql security invoker set search_path = '' as $$
begin
  update public.list_locks
     set holder_pc = p_pc, holder_user = p_user, heartbeat_at = now(), acquired_at = now()
   where client_id = p_client and session_name = p_session and list_name = p_list
     and heartbeat_at = p_expected_heartbeat
     and heartbeat_at < now() - make_interval(secs => p_stale_seconds);
  return found;
end $$;

create function public.heartbeat_list_lock(
  p_client text, p_session text, p_list text, p_pc text) returns boolean
language plpgsql security invoker set search_path = '' as $$
begin
  update public.list_locks set heartbeat_at = now()
   where client_id = p_client and session_name = p_session and list_name = p_list
     and holder_pc = p_pc;
  return found;
end $$;

create function public.release_list_lock(
  p_client text, p_session text, p_list text, p_pc text) returns void
language sql security invoker set search_path = '' as $$
  delete from public.list_locks
   where client_id = p_client and session_name = p_session and list_name = p_list
     and holder_pc = p_pc;
$$;

-- p_rows: json array of objects; row_no = position, order_number taken from each object.
create function public.replace_fulfillment_history(
  p_client text, p_rows jsonb, p_expected_version bigint) returns bigint
language plpgsql security invoker set search_path = '' as $$
declare v bigint;
begin
  insert into public.fulfillment_history_meta (client_id, version)
  values (p_client, 0) on conflict do nothing;
  select version into v from public.fulfillment_history_meta
   where client_id = p_client for update;
  if v is distinct from p_expected_version then
    raise exception 'fulfillment_history version mismatch: have %, expected %', v, p_expected_version
      using errcode = '40001';
  end if;
  delete from public.fulfillment_history where client_id = p_client;
  insert into public.fulfillment_history (client_id, row_no, order_number, row)
  select p_client, o::int, e->>'order_number', e
    from jsonb_array_elements(p_rows) with ordinality as t(e, o);
  update public.fulfillment_history_meta set version = v + 1 where client_id = p_client;
  return v + 1;
end $$;

revoke execute on all functions in schema public from public, anon;
grant execute on function public.acquire_list_lock, public.force_take_list_lock,
  public.heartbeat_list_lock, public.release_list_lock, public.replace_fulfillment_history
  to authenticated;
