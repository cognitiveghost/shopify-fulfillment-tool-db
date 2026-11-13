begin;
select plan(35);

-- one row per table, as authenticated
select set_config('request.jwt.claims','{"role":"authenticated"}',true);
set local role authenticated;
select lives_ok($$insert into clients(client_id,name) values ('C1','Client 1')$$, 'clients insert');
select lives_ok($$insert into client_config_history(client_id,kind,body) values ('C1','config','{}')$$, 'client_config_history insert');
select lives_ok($$insert into groups(body) values ('{}')$$, 'groups insert');
select lives_ok($$insert into fulfillment_history(client_id,row_no,order_number,row) values ('C1',1,'#1','{}')$$, 'fulfillment_history insert');
select lives_ok($$insert into fulfillment_history_meta(client_id,version) values ('C1',1)$$, 'fulfillment_history_meta insert');
select lives_ok($$insert into sessions(client_id,name,status,info) values ('C1','S1','active','{}')$$, 'sessions insert');
select lives_ok($$insert into session_documents(client_id,session_name,path,body) values ('C1','S1','analysis/analysis_data.json','{}')$$, 'session_documents insert');
select lives_ok($$insert into packing_lists(client_id,session_name,name,body) values ('C1','S1','L1','{}')$$, 'packing_lists insert');
select lives_ok($$insert into packing_state(client_id,session_name,list_name,state) values ('C1','S1','L1','{}')$$, 'packing_state insert');
select lives_ok($$insert into list_locks(client_id,session_name,list_name,holder_pc,holder_user,heartbeat_at,acquired_at) values ('C1','S1','LX','pc','u',now(),now())$$, 'list_locks insert');
select lives_ok($$insert into workers(body) values ('{}')$$, 'workers insert');
select lives_ok($$insert into stats(body) values ('{}')$$, 'stats insert');

select is((select count(*)::int from clients), 1, 'authenticated selects clients');
select is((select count(*)::int from fulfillment_history_meta), 1, 'authenticated selects meta');
select is((select count(*)::int from stats), 1, 'authenticated selects stats');

-- anon is denied everywhere
reset role;
set local role anon;
select throws_ok('select 1 from clients', '42501', null, 'anon denied clients');
select throws_ok('select 1 from client_config_history', '42501', null, 'anon denied client_config_history');
select throws_ok('select 1 from groups', '42501', null, 'anon denied groups');
select throws_ok('select 1 from fulfillment_history', '42501', null, 'anon denied fulfillment_history');
select throws_ok('select 1 from fulfillment_history_meta', '42501', null, 'anon denied fulfillment_history_meta');
select throws_ok('select 1 from sessions', '42501', null, 'anon denied sessions');
select throws_ok('select 1 from session_documents', '42501', null, 'anon denied session_documents');
select throws_ok('select 1 from packing_lists', '42501', null, 'anon denied packing_lists');
select throws_ok('select 1 from packing_state', '42501', null, 'anon denied packing_state');
select throws_ok('select 1 from list_locks', '42501', null, 'anon denied list_locks');
select throws_ok('select 1 from workers', '42501', null, 'anon denied workers');
select throws_ok('select 1 from stats', '42501', null, 'anon denied stats');
reset role;

-- lock rules
set local role authenticated;
select is((select (acquire_list_lock('C1','S1','L1','pcA','alice'))->>'acquired'), 'true', 'first acquire wins');
select is((select (acquire_list_lock('C1','S1','L1','pcB','bob'))->>'acquired'), 'false', 'other PC denied');
select is((select (acquire_list_lock('C1','S1','L1','pcA','alice'))->>'acquired'), 'true', 'same PC re-acquires');
select is(force_take_list_lock('C1','S1','L1','pcB','bob', now()), false, 'force_take with fresh heartbeat refused');
update list_locks set heartbeat_at = now() - interval '121 seconds' where list_name = 'L1';
select is(force_take_list_lock('C1','S1','L1','pcB','bob', now() - interval '121 seconds'), true, 'force_take stale + matching heartbeat');
select is(heartbeat_list_lock('C1','S1','L1','pcA'), false, 'old holder heartbeat false after take-over');

-- history replace
select is(replace_fulfillment_history('C1','[{"order_number":"#9"}]'::jsonb, 1), 2::bigint, 'replace with right version');
select throws_ok($$select replace_fulfillment_history('C1','[]'::jsonb, 1)$$, '40001', null, 'replace with wrong version raises');

select * from finish();
rollback;
