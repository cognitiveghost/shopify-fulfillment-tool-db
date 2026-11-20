import threading
from datetime import UTC, timedelta

import pytest

from shared import store as store_mod
from shared.store import FileStore, StoreConflict, StoreError, StoreNotFound

LIST = "Sessions/CLIENT_X/s1/packing/list1"


# JSON paths must route (shared/store_routes.py) on both backends: session docs are the generic home.
S = "Sessions/CLIENT_X/s1/"


@pytest.fixture(params=["file", pytest.param("supabase", marks=pytest.mark.supabase)])
def store(request, tmp_path):
    if request.param == "file":
        return FileStore(tmp_path)
    from shared.store_supabase import SupabaseStore

    return SupabaseStore(request.getfixturevalue("supabase_client"), pc="pc1", cache_dir=tmp_path)


def test_json_roundtrip(store):
    store.write_json(S + "a/b.json", {"k": [1, "é"]})
    assert store.read_json(S + "a/b.json") == {"k": [1, "é"]}


def test_read_missing_raises(store):
    with pytest.raises(StoreNotFound):
        store.read_json(S + "nope.json")
    with pytest.raises(StoreNotFound):
        store.read_bytes(S + "nope.bin")


def test_bytes_roundtrip_exists_delete(store):
    store.write_bytes(S + "d/x.bin", b"\x00\x01")
    assert store.read_bytes(S + "d/x.bin") == b"\x00\x01" and store.exists(S + "d/x.bin")
    store.delete(S + "d/x.bin")
    assert not store.exists(S + "d/x.bin")
    store.delete(S + "d/x.bin")  # absent is fine
    store.write_bytes(S + "d/y.bin", b"1")
    store.delete(S + "d")
    assert not store.exists(S + "d")


def test_exists_raises_on_a_failed_look(store, monkeypatch):
    if not isinstance(store, FileStore):
        pytest.skip("FileStore failure injection")

    def denied(self, *a, **k):
        raise PermissionError("access denied")

    monkeypatch.setattr(store_mod.Path, "stat", denied)
    with pytest.raises(StoreError):  # never "absent"
        store.exists(S + "d/x.bin")


def test_update_json_concurrent(store):
    rounds = 50 if isinstance(store, FileStore) else 12  # two writers in lockstep; CAS retries are bounded

    def work():
        for _ in range(rounds):
            store.update_json(S + "c.json", lambda d: {"n": (d or {"n": 0})["n"] + 1})

    ts = [threading.Thread(target=work) for _ in range(2)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    assert store.read_json(S + "c.json") == {"n": 2 * rounds}
    assert store.list_dir(S.rstrip("/")) == ["c.json"]


def test_list_dir_sorted_and_missing_empty(store):
    store.write_json(S + "d/b.json", {})
    store.write_json(S + "d/a.json", {})
    assert store.list_dir(S + "d") == ["a.json", "b.json"]
    assert store.list_dir(S + "missing") == []


def test_move_then_old_missing(store):
    store.write_json(S + "packing_lists/x.json", {"v": 1})
    store.move(S + "packing_lists/x.json", S + "packing_lists/withdrawn/x.json")
    assert not store.exists(S + "packing_lists/x.json")
    assert store.read_json(S + "packing_lists/withdrawn/x.json") == {"v": 1}


def test_claim_dir_once(store):
    assert store.claim_dir("Sessions/CLIENT_C/s_1") is True
    assert store.claim_dir("Sessions/CLIENT_C/s_1") is False


def test_local_path(store):
    store.write_bytes(S + "f.xlsx", b"x")
    assert store.local_path(S + "f.xlsx").read_bytes() == b"x"


def test_lock_second_pc_refused(store):
    assert store.acquire_list_lock(LIST, "pc1", "ann").acquired
    info = store.acquire_list_lock(LIST, "pc2", "bob")
    assert not info.acquired and info.holder_pc == "pc1" and info.holder_user == "ann"
    assert info.heartbeat_at is not None


def test_lock_same_pc_reenters(store):
    store.acquire_list_lock(LIST, "pc1", "ann")
    info = store.acquire_list_lock(LIST, "pc1", "ann")
    assert info.acquired and info.holder_pc == "pc1"


def test_release_only_by_holder(store):
    store.acquire_list_lock(LIST, "pc1", "ann")
    store.release_list_lock(LIST, "pc2")
    assert not store.acquire_list_lock(LIST, "pc2", "bob").acquired
    store.release_list_lock(LIST, "pc1")
    assert store.acquire_list_lock(LIST, "pc2", "bob").acquired


def test_force_take_only_when_stale(store, monkeypatch):
    if not isinstance(store, FileStore):
        pytest.skip("patches the local clock; Supabase has its own test")
    held = store.acquire_list_lock(LIST, "pc1", "ann")
    for age, taken in ((30, False), (121, True)):
        monkeypatch.setattr(store_mod, "_now", lambda age=age: held.heartbeat_at + timedelta(seconds=age))
        cur = store.acquire_list_lock(LIST, "pc2", "bob")
        assert not cur.acquired  # acquire never takes over, even stale
        assert store.force_take_list_lock(LIST, "pc2", "bob", cur.heartbeat_at) is taken
    # wrong expected heartbeat is refused
    assert not store.force_take_list_lock(LIST, "pc3", "cy", held.heartbeat_at)


def test_heartbeat_after_takeover_false(store, monkeypatch):
    if not isinstance(store, FileStore):
        pytest.skip("patches the local clock; Supabase has its own test")
    held = store.acquire_list_lock(LIST, "pc1", "ann")
    assert store.heartbeat_list_lock(LIST, "pc1")
    monkeypatch.setattr(store_mod, "_now", lambda: held.heartbeat_at + timedelta(seconds=500))
    hb = store.acquire_list_lock(LIST, "pc2", "bob").heartbeat_at
    assert store.force_take_list_lock(LIST, "pc2", "bob", hb)
    assert not store.heartbeat_list_lock(LIST, "pc1")
    assert store.heartbeat_list_lock(LIST, "pc2")


@pytest.mark.parametrize("rel", ["../x", "/x", "a\\b", "a/../b"])
def test_rejects_bad_rel(store, rel):
    with pytest.raises(ValueError):
        store.read_json(rel)
    with pytest.raises(ValueError):
        store.write_bytes(rel, b"")


def test_base_store_methods_not_implemented():
    s = store_mod.Store()
    with pytest.raises(NotImplementedError):
        s.read_json("a")
    with pytest.raises(NotImplementedError):
        s.acquire_list_lock("a", "pc", "u")
    with pytest.raises(NotImplementedError):
        s.write_bytes_if("a", b"", None)


def test_lock_file_compat_with_packer_format(store, tmp_path):
    if not isinstance(store, FileStore):
        pytest.skip("raw lock-file format is FileStore-specific")
    store_mod.LOCK_READ_RETRY_SECONDS = 0  # keep the unreadable-lock case fast
    # naive-timestamp / unreadable / invalid lock files, as a production Packer PC may leave them
    lock = tmp_path / LIST / ".session.lock"
    lock.parent.mkdir(parents=True)
    lock.write_text("{broken")
    info = store.acquire_list_lock(LIST, "pc1", "ann")
    assert not info.acquired and info.heartbeat_at is not None  # unreadable counts as held
    lock.write_text('{"locked_by": "pcX"}')  # invalid -> free
    assert store.acquire_list_lock(LIST, "pc1", "ann").acquired
    import json
    body = json.loads(lock.read_text())
    assert {"locked_by", "user_name", "lock_time", "process_id", "app_version", "heartbeat",
            "worker_id", "worker_name"} <= body.keys()
    body["heartbeat"] = "2020-01-01T00:00:00"  # naive -> read as UTC
    lock.write_text(json.dumps(body))
    assert store.acquire_list_lock(LIST, "pc2", "bob").heartbeat_at.utcoffset().total_seconds() == 0


def test_lock_ops_on_absent_lock(store):
    assert not store.heartbeat_list_lock(LIST, "pc1")
    store.release_list_lock(LIST, "pc1")
    from datetime import datetime
    assert not store.force_take_list_lock(LIST, "pc1", "a", datetime.now(UTC))


def test_corrupt_json_raises_store_error(store):
    if not isinstance(store, FileStore):
        pytest.skip("raw bytes only reach a file backend")
    store.write_bytes(S + "bad.json", b"{not json")
    with pytest.raises(StoreError) as e:
        store.read_json(S + "bad.json")
    assert not isinstance(e.value, StoreNotFound)


def test_update_json_retries_a_failed_read(tmp_path, monkeypatch):
    # A sharing violation on the share must not drop the update (AUDIT-02-5).
    store = FileStore(tmp_path)
    store.write_json(S + "r.json", {"n": 1})
    monkeypatch.setattr(store_mod, "LOCK_READ_RETRY_SECONDS", 0)
    real_read, failed = store.read_json, []

    def flaky_read(rel):
        if not failed:
            failed.append(True)
            raise StoreError(rel)
        return real_read(rel)

    monkeypatch.setattr(store, "read_json", flaky_read)
    assert store.update_json(S + "r.json", lambda d: {"n": d["n"] + 1}) == {"n": 2}
    monkeypatch.setattr(store, "read_json", lambda rel: (_ for _ in ()).throw(StoreError(rel)))
    with pytest.raises(StoreError):
        store.update_json(S + "r.json", lambda d: d)


def test_update_json_sidecar_lock_hidden_from_list_dir(store):
    store.update_json(S + "d/c.json", lambda d: {"n": 1})
    assert store.list_dir(S + "d") == ["c.json"]


def test_session_lock_visible_in_list_dir(store):
    store.acquire_list_lock(LIST, "pc1", "ann")
    assert ".session.lock" in store.list_dir(LIST)


def test_lock_same_pc_other_process_refused(store, tmp_path):
    if not isinstance(store, FileStore):
        pytest.skip("raw lock-file edit is FileStore-specific")
    store.acquire_list_lock(LIST, "pc1", "ann")
    lock = tmp_path / LIST / ".session.lock"
    import json
    body = json.loads(lock.read_text())
    body["process_id"] += 1
    lock.write_text(json.dumps(body))
    assert not store.acquire_list_lock(LIST, "pc1", "ann").acquired
    assert not store.heartbeat_list_lock(LIST, "pc1")
    store.release_list_lock(LIST, "pc1")
    assert lock.exists()


def test_list_dir_on_a_file_is_empty(store):
    store.write_bytes(S + "f", b"x")
    assert store.list_dir(S + "f") == []


def test_stamp_changes_on_rewrite_and_missing_raises(store):
    import time

    with pytest.raises(StoreNotFound):
        store.stamp(S + "s.json")
    store.write_json(S + "s.json", {"v": 1})
    first = store.stamp(S + "s.json")
    time.sleep(0.05)  # coarse filesystem timestamps
    store.write_json(S + "s.json", {"v": 2, "more": "content"})
    assert store.stamp(S + "s.json") != first


def test_write_bytes_if_none_writes_only_when_absent(store):
    stamp = store.write_bytes_if(S + "st/state.pkl", b"one", None)
    assert store.read_bytes(S + "st/state.pkl") == b"one"
    assert stamp == store.stamp(S + "st/state.pkl")
    with pytest.raises(StoreConflict):
        store.write_bytes_if(S + "st/state.pkl", b"two", None)
    assert store.read_bytes(S + "st/state.pkl") == b"one"


def test_write_bytes_if_wrong_stamp_conflicts_and_writes_nothing(store):
    store.write_bytes(S + "w.bin", b"kept")
    with pytest.raises(StoreConflict):
        store.write_bytes_if(S + "w.bin", b"lost", ("not", "the", "stamp"))
    assert store.read_bytes(S + "w.bin") == b"kept"


def test_write_bytes_if_right_stamp_writes_and_returns_new_stamp(store):
    store.write_bytes(S + "r.bin", b"a")
    held = store.stamp(S + "r.bin")
    new = store.write_bytes_if(S + "r.bin", b"bbb", held)
    assert store.read_bytes(S + "r.bin") == b"bbb"
    assert new != held and new == store.stamp(S + "r.bin")


def test_lock_info_reads_without_taking(store):
    assert store.lock_info(LIST) is None
    store.acquire_list_lock(LIST, "pc1", "ann")
    info = store.lock_info(LIST)
    assert (info.holder_pc, info.holder_user) == ("pc1", "ann") and info.heartbeat_at is not None
    assert store.lock_info(LIST).holder_pc == "pc1"  # reading did not change the holder
    store.release_list_lock(LIST, "pc1")
    assert store.lock_info(LIST) is None


def test_virtual_session_folders_exist_when_they_have_children(store):
    store.write_json(S + "session_info.json", {"status": "active"})
    assert not store.exists(S + "packing_lists") and not store.exists(S + "packing/list1")
    store.write_json(S + "packing_lists/list1.json", {"orders": []})
    store.write_json(S + "packing/list1/packing_state.json", {"progress": {}})
    assert store.exists(S + "packing_lists") and store.exists(S + "packing")
    assert store.exists(S + "packing/list1")


def _seed_overview(store):
    c = "Sessions/CLIENT_X/"
    store.write_json(c + "s1/session_info.json", {"pc_name": "PC-A"})
    store.write_json(c + "s1/packing_lists/U.json", {"created_at": "t1", "total_orders": 2, "total_items": 3, "orders": []})
    store.write_json(c + "s1/packing_lists/withdrawn/W.json", {"orders": []})
    store.write_json(c + "s2/packing_lists/S.json", {"created_at": "t1", "total_orders": 2, "total_items": 3, "orders": []})
    store.write_json(c + "s2/packing/S/packing_state.json",
                     {"worker_id": "w1", "progress": {"total_orders": 2}, "skipped_orders": ["9"], "orders": [1]})
    store.acquire_list_lock(c + "s2/packing/S", "PC-B", "u")
    store.write_json(c + "s3/packing_lists/F.json", {"created_at": "t1", "total_orders": 2, "total_items": 3, "orders": []})
    store.write_json(c + "s3/packing/F/session_summary.json", {"total_orders": 2, "completed_orders": 2})


def test_packing_overview_rows(store):
    _seed_overview(store)
    rows = store.packing_overview("X")
    assert [(r["session_name"], r["list_name"]) for r in rows] == [("s1", "U"), ("s2", "S"), ("s3", "F")]
    u, s, f = rows
    assert (u["started"], u["state"], u["summary"], u["lock"]) == (False, None, None, None)
    assert u["session_info"] == {"pc_name": "PC-A"}
    assert u["packing_list"] == {"created_at": "t1", "total_orders": 2, "total_items": 3}
    assert s["started"] is True
    assert s["state"] == {"worker_id": "w1", "progress": {"total_orders": 2}, "skipped_orders": ["9"]}
    assert s["lock"].holder_pc == "PC-B"
    assert s["session_info"] is None
    assert s["work_rel"] == "Sessions/CLIENT_X/s2/packing/S"
    assert f["summary"] == {"total_orders": 2, "completed_orders": 2}
    assert f["state"] is None


def test_packing_overview_unknown_client_empty(store):
    assert store.packing_overview("NOPE") == []


def test_packing_lists_lists_xlsx_with_json(store):
    store.write_json(S + "packing_lists/DHL.json", {"orders": []})
    store.write_bytes(S + "packing_lists/DHL.xlsx", b"xlsx")
    store.write_json(S + "packing_lists/withdrawn/OLD.json", {"orders": []})
    store.write_bytes(S + "packing_lists/withdrawn/OLD.xlsx", b"xlsx")
    assert store.list_dir(S + "packing_lists") == ["DHL.json", "DHL.xlsx", "withdrawn"]
    assert store.list_dir(S + "packing_lists/withdrawn") == ["OLD.json", "OLD.xlsx"]


def test_packing_lists_split_live_and_withdrawn_in_name_order(store):
    store.write_json(S + "packing_lists/b.json", {"orders": []})
    store.write_json(S + "packing_lists/a.json", {"orders": []})
    store.write_json(S + "packing_lists/withdrawn/c.json", {"orders": []})
    assert store.list_dir(S + "packing_lists") == ["a.json", "b.json", "withdrawn"]
    assert store.list_dir(S + "packing_lists/withdrawn") == ["c.json"]
