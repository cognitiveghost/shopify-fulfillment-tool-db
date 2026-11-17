import httpx
import pytest
from postgrest.exceptions import APIError
from test_store_contract import _seed_overview

from shared.store import StoreConflict, StoreError, StoreNotFound
from shared.store_supabase import SupabaseStore
from supabase import create_client

pytestmark = pytest.mark.supabase

S = "Sessions/CLIENT_X/s1/"
HISTORY = "Clients/CLIENT_X/fulfillment_history.csv"


@pytest.fixture
def store(supabase_client, tmp_path):
    return SupabaseStore(supabase_client, pc="pc1", cache_dir=tmp_path)


def test_read_error_is_store_error_not_empty(store, monkeypatch):
    def boom(*a, **k):
        raise OSError("network down")

    monkeypatch.setattr(store.client, "table", boom)
    for call in (lambda: store.read_json(S + "a.json"), lambda: store.list_dir("Sessions/CLIENT_X"),
                 lambda: store.exists(S + "a.json")):
        with pytest.raises(StoreError) as e:
            call()
        assert not isinstance(e.value, StoreNotFound)


def test_anon_client_cannot_read_or_write(store, supabase_client):
    import os

    store.write_json(S + "a.json", {"secret": 1})
    anon = create_client(os.environ["SUPABASE_TEST_URL"], os.environ["SUPABASE_TEST_ANON_KEY"])
    with pytest.raises(APIError):  # anon holds no privileges at all
        anon.table("session_documents").select("*").execute()
    with pytest.raises(APIError):
        anon.table("session_documents").insert(
            {"client_id": "X", "session_name": "s1", "path": "p.json", "body": {}}).execute()
    with pytest.raises(APIError):
        anon.rpc("acquire_list_lock", {"p_client": "X", "p_session": "s1", "p_list": "l",
                                       "p_pc": "evil", "p_user": "x"}).execute()


def test_history_roundtrip_keeps_column_order(store):
    csv = "Order_Number,Execution_Date,Session\n1001,2026-01-02,s1\n1002,2026-01-03,s2\n"
    store.write_bytes(HISTORY, csv.encode())
    assert store.read_bytes(HISTORY).decode() == csv
    assert store.exists(HISTORY)


def test_history_stale_stamp_conflicts(store):
    store.write_bytes(HISTORY, b"Order_Number\n1\n")
    held = store.stamp(HISTORY)
    store.write_bytes_if(HISTORY, b"Order_Number\n1\n2\n", held)
    with pytest.raises(StoreConflict):
        store.write_bytes_if(HISTORY, b"Order_Number\n9\n", held)
    assert store.read_bytes(HISTORY) == b"Order_Number\n1\n2\n"


def test_history_past_the_row_cap_reads_whole(store):
    # max_rows (1000) caps every select; a short read here would be saved back as a loss
    csv = "Order_Number\n" + "".join(f"{n}\n" for n in range(1500))
    stamp = store.write_bytes_if(HISTORY, csv.encode(), None)
    assert stamp == store.stamp(HISTORY)
    assert store.read_bytes(HISTORY).decode() == csv


def test_session_listing_past_the_row_cap(store):
    names = [f"s{n:04}" for n in range(1100)]
    store.client.table("clients").insert({"client_id": "X"}).execute()
    store.client.table("sessions").insert([{"client_id": "X", "name": n} for n in names]).execute()
    assert store.list_dir("Sessions/CLIENT_X") == names


def test_history_missing_raises_not_found(store):
    with pytest.raises(StoreNotFound):
        store.read_bytes(HISTORY)


def test_json_write_if_stale_version_conflicts(store):
    store.write_json(S + "d.json", {"v": 1})
    held = store.stamp(S + "d.json")
    store.write_bytes_if(S + "d.json", b'{"v": 2}', held)
    with pytest.raises(StoreConflict):
        store.write_bytes_if(S + "d.json", b'{"v": 3}', held)
    assert store.read_json(S + "d.json") == {"v": 2}


def test_client_fields_and_archive_move(store):
    store.write_json("Clients/CLIENT_X/client_config.json", {"name": "X"})
    store.write_json(S + "session_info.json", {"s": 1})
    assert store.list_dir("Clients") == ["CLIENT_X", "_archive"]
    store.move("Clients/CLIENT_X", "Clients/_archive/CLIENT_X")
    store.move("Sessions/CLIENT_X", "Sessions/_archive/CLIENT_X")
    assert not store.exists("Clients/CLIENT_X") and store.exists("Clients/_archive/CLIENT_X")
    assert store.list_dir("Sessions/_archive") == ["CLIENT_X"]
    assert store.list_dir("Sessions/CLIENT_X") == []
    store.move("Clients/_archive/CLIENT_X", "Clients/CLIENT_X")
    assert store.exists("Clients/CLIENT_X")


def test_withdraw_and_restore_packing_list(store):
    store.write_json(S + "packing_lists/a.json", {"items": []})
    store.move(S + "packing_lists/a.json", S + "packing_lists/withdrawn/a.json")
    assert store.list_dir(S + "packing_lists") == ["withdrawn"]
    assert store.list_dir(S + "packing_lists/withdrawn") == ["a.json"]
    store.move(S + "packing_lists/withdrawn/a.json", S + "packing_lists/a.json")
    assert store.list_dir(S + "packing_lists") == ["a.json"]


def test_unsupported_move_refused(store):
    store.write_json(S + "x.json", {})
    with pytest.raises(ValueError):
        store.move(S + "x.json", S + "y.json")


def test_packing_state_summary_and_lock_listing(store):
    base = S + "packing/l1/"
    store.write_json(base + "packing_state.json", {"done": 1})
    store.write_json(base + "session_summary.json", {"total": 3})
    store.acquire_list_lock(base.rstrip("/"), "pc1", "ann")
    assert store.list_dir(S + "packing") == ["l1"]
    assert store.list_dir(base.rstrip("/")) == [".session.lock", "packing_state.json", "session_summary.json"]


def test_local_path_downloads_blob(store, tmp_path):
    store.write_bytes(S + "out/x.xlsx", b"data")
    p = store.local_path(S + "out/x.xlsx")
    assert p.read_bytes() == b"data" and tmp_path in p.parents
    assert store.local_path("") == tmp_path


def test_claim_dir_session_and_client(store):
    assert store.claim_dir("Clients/CLIENT_N") is True
    assert store.claim_dir("Clients/CLIENT_N") is False
    assert store.claim_dir("Sessions/CLIENT_N/2026_1") is True
    assert store.claim_dir("Sessions/CLIENT_N/2026_1") is False
    assert store.claim_dir("Sessions/CLIENT_N/2026_1/analysis") is True  # implicit subfolder


def test_local_path_of_a_folder_makes_no_call(store, tmp_path, monkeypatch):
    def boom(*a, **k):
        raise AssertionError("network call")

    monkeypatch.setattr(type(store), "_bucket", property(boom))  # any storage use would raise
    assert store.local_path("Clients") == tmp_path / "Clients"
    assert store.local_path("Sessions/CLIENT_X/s1") == tmp_path / "Sessions/CLIENT_X/s1"


def test_query_sessions_lists_live_sessions_with_info(store):
    store.write_json("Sessions/CLIENT_X/live/session_info.json", {"status": "active"})
    store.write_json("Sessions/CLIENT_X/archived/session_info.json", {"status": "active"})
    store.claim_dir("Sessions/CLIENT_X/no_info")  # a session row with no info yet
    # Store.move archives a whole client only, so flag this session's row directly.
    store.client.table("sessions").update({"archived": True}).match(
        {"client_id": "X", "name": "archived"}).execute()

    assert store.query_sessions("X") == [{"session_name": "live", "status": "active"}]
    assert store.query_sessions("NOBODY") == []


def test_packing_overview_skips_withdrawn_and_archived(store, supabase_client):
    _seed_overview(store)
    # the withdrawn list W (under s1) still has a started state and a lock: both must stay out
    store.write_json("Sessions/CLIENT_X/s1/packing/W/packing_state.json", {"worker_id": "w9", "progress": {}})
    store.acquire_list_lock("Sessions/CLIENT_X/s1/packing/W", "PC-C", "u")
    supabase_client.table("sessions").update({"archived": True}).eq("client_id", "X").eq("name", "s3").execute()

    assert [(r["session_name"], r["list_name"]) for r in store.packing_overview("X")] == [("s1", "U"), ("s2", "S")]


def test_packing_overview_api_error_raises(store, monkeypatch):
    def down(*a, **k):
        raise httpx.ConnectError("down")

    monkeypatch.setattr(store.client, "table", down)
    with pytest.raises(StoreError):
        store.packing_overview("X")


def test_packing_overview_request_count_is_fixed(store, monkeypatch):
    def count_calls():
        calls = []
        real = store.client.table

        def counting(name):
            calls.append(name)
            return real(name)

        with monkeypatch.context() as m:
            m.setattr(store.client, "table", counting)
            store.packing_overview("X")
        return len(calls)

    for i in range(3):
        store.write_json(f"Sessions/CLIENT_X/n{i}/packing_lists/U.json", {"total_orders": 1, "orders": []})
    few = count_calls()
    for i in range(3, 6):
        store.write_json(f"Sessions/CLIENT_X/n{i}/packing_lists/U.json", {"total_orders": 1, "orders": []})
    assert few == count_calls() <= 5


class _CountingBucket:
    def __init__(self, bucket, downloads):
        self._bucket = bucket
        self._downloads = downloads

    def download(self, rel):
        self._downloads.append(rel)
        return self._bucket.download(rel)

    def __getattr__(self, name):
        return getattr(self._bucket, name)


@pytest.fixture
def downloads(store, monkeypatch):
    calls = []
    real = type(store)._bucket.fget
    monkeypatch.setattr(type(store), "_bucket", property(lambda self: _CountingBucket(real(self), calls)))
    return calls


def test_local_path_downloads_unchanged_blob_once(store, downloads):
    store.write_bytes(S + "input/a.csv", b"1")
    assert store.local_path(S + "input/a.csv").read_bytes() == b"1"
    assert store.local_path(S + "input/a.csv").read_bytes() == b"1"
    assert downloads == [S + "input/a.csv"]


def test_local_path_without_a_stamp_always_downloads(store, downloads, monkeypatch):
    store.write_bytes(S + "input/a.csv", b"1")
    monkeypatch.setattr(store, "_blob_stamp", lambda rel: (None, None))
    store.local_path(S + "input/a.csv")
    store.local_path(S + "input/a.csv")
    assert downloads == [S + "input/a.csv"] * 2


def test_local_path_refetches_after_change_by_another_pc(store, supabase_client, tmp_path):
    store.write_bytes(S + "input/a.csv", b"1")
    assert store.local_path(S + "input/a.csv").read_bytes() == b"1"
    other_pc = SupabaseStore(supabase_client, pc="pc2", cache_dir=tmp_path / "other")
    other_pc.write_bytes(S + "input/a.csv", b"2")
    assert store.local_path(S + "input/a.csv").read_bytes() == b"2"


def test_local_path_failed_check_raises_store_error(store, monkeypatch):
    store.write_bytes(S + "input/a.csv", b"1")

    def down(*a, **k):
        raise httpx.ConnectError("down")

    monkeypatch.setattr(type(store), "_bucket", property(down))
    with pytest.raises(StoreError):
        store.local_path(S + "input/a.csv")
