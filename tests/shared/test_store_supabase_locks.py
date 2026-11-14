import threading
from datetime import UTC, datetime, timedelta

import pytest
from conftest import make_user_client

from shared.store_supabase import SupabaseStore

pytestmark = pytest.mark.supabase

LIST = "Sessions/CLIENT_X/s1/packing/list1"


@pytest.fixture
def store(supabase_client, tmp_path):
    return SupabaseStore(supabase_client, pc="pc1", cache_dir=tmp_path)


def test_acquire_race_one_winner(supabase_client, supabase_user, tmp_path):
    results = []

    def go(i):
        # own client (own connection) per PC, as on real machines
        s = SupabaseStore(make_user_client(*supabase_user), pc=f"pc{i}", cache_dir=tmp_path)
        results.append(s.acquire_list_lock(LIST, f"pc{i}", f"user{i}"))

    ts = [threading.Thread(target=go, args=(i,)) for i in range(10)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    assert len(results) == 10
    winners = [r for r in results if r.acquired]
    assert len(winners) == 1
    assert {r.holder_pc for r in results} == {winners[0].holder_pc}  # losers all see the winner


def _backdate(admin, seconds):
    old = (datetime.now(UTC) - timedelta(seconds=seconds)).isoformat()
    admin.table("list_locks").update({"heartbeat_at": old}).eq("list_name", "list1").execute()


def test_force_take_only_when_stale_server_clock(store, supabase_admin):
    store.acquire_list_lock(LIST, "pc1", "ann")
    for age, taken in ((30, False), (121, True)):
        _backdate(supabase_admin, age)
        cur = store.acquire_list_lock(LIST, "pc2", "bob")
        assert not cur.acquired and cur.holder_pc == "pc1"  # acquire never takes over, even stale
        assert store.force_take_list_lock(LIST, "pc2", "bob", cur.heartbeat_at) is taken
    assert store.acquire_list_lock(LIST, "pc2", "bob").acquired


def test_force_take_refused_with_wrong_expected_heartbeat(store, supabase_admin):
    store.acquire_list_lock(LIST, "pc1", "ann")
    _backdate(supabase_admin, 500)
    assert not store.force_take_list_lock(LIST, "pc2", "bob", datetime(2020, 1, 1, tzinfo=UTC))


def test_heartbeat_after_takeover_false(store, supabase_admin):
    store.acquire_list_lock(LIST, "pc1", "ann")
    _backdate(supabase_admin, 500)
    hb = store.acquire_list_lock(LIST, "pc2", "bob").heartbeat_at
    assert store.force_take_list_lock(LIST, "pc2", "bob", hb)
    assert not store.heartbeat_list_lock(LIST, "pc1")
    assert store.heartbeat_list_lock(LIST, "pc2")
    store.release_list_lock(LIST, "pc1")  # not the holder: no effect
    assert not store.acquire_list_lock(LIST, "pc1", "ann").acquired


def test_lock_dir_must_be_a_packing_list(store):
    with pytest.raises(ValueError):
        store.acquire_list_lock("Sessions/CLIENT_X/s1", "pc1", "ann")
