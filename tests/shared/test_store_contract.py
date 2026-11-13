import threading
from datetime import UTC, timedelta

import pytest

from shared import store as store_mod
from shared.store import FileStore, StoreError, StoreNotFound

LIST = "Sessions/CLIENT_X/s1/packing/list1"


@pytest.fixture(params=["file"])  # add a backend by adding a param + a branch here
def store(request, tmp_path):
    return FileStore(tmp_path)


def test_json_roundtrip(store):
    store.write_json("a/b.json", {"k": [1, "é"]})
    assert store.read_json("a/b.json") == {"k": [1, "é"]}


def test_read_missing_raises(store):
    with pytest.raises(StoreNotFound):
        store.read_json("nope.json")
    with pytest.raises(StoreNotFound):
        store.read_bytes("nope.bin")


def test_bytes_roundtrip_exists_delete(store):
    store.write_bytes("d/x.bin", b"\x00\x01")
    assert store.read_bytes("d/x.bin") == b"\x00\x01" and store.exists("d/x.bin")
    store.delete("d/x.bin")
    assert not store.exists("d/x.bin")
    store.delete("d/x.bin")  # absent is fine
    store.write_bytes("d/y.bin", b"1")
    store.delete("d")
    assert not store.exists("d")


def test_update_json_concurrent(store):
    def work():
        for _ in range(50):
            store.update_json("c.json", lambda d: {"n": (d or {"n": 0})["n"] + 1})

    ts = [threading.Thread(target=work) for _ in range(2)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    assert store.read_json("c.json") == {"n": 100}
    assert store.list_dir("") == ["c.json"]


def test_list_dir_sorted_and_missing_empty(store):
    store.write_json("d/b.json", {})
    store.write_json("d/a.json", {})
    assert store.list_dir("d") == ["a.json", "b.json"]
    assert store.list_dir("missing") == []


def test_move_then_old_missing(store):
    store.write_json("a/x.json", {"v": 1})
    store.move("a/x.json", "b/c/x.json")
    assert not store.exists("a/x.json")
    assert store.read_json("b/c/x.json") == {"v": 1}


def test_claim_dir_once(store):
    assert store.claim_dir("Sessions/C/s_1") is True
    assert store.claim_dir("Sessions/C/s_1") is False


def test_local_path(store):
    store.write_bytes("f.xlsx", b"x")
    assert store.local_path("f.xlsx").read_bytes() == b"x"


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
    held = store.acquire_list_lock(LIST, "pc1", "ann")
    for age, taken in ((30, False), (121, True)):
        monkeypatch.setattr(store_mod, "_now", lambda: held.heartbeat_at + timedelta(seconds=age))
        cur = store.acquire_list_lock(LIST, "pc2", "bob")
        assert not cur.acquired  # acquire never takes over, even stale
        assert store.force_take_list_lock(LIST, "pc2", "bob", cur.heartbeat_at) is taken
    # wrong expected heartbeat is refused
    assert not store.force_take_list_lock(LIST, "pc3", "cy", held.heartbeat_at)


def test_heartbeat_after_takeover_false(store, monkeypatch):
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
    store.write_bytes("bad.json", b"{not json")
    with pytest.raises(StoreError) as e:
        store.read_json("bad.json")
    assert not isinstance(e.value, StoreNotFound)


def test_update_json_sidecar_lock_hidden_from_list_dir(store):
    store.update_json("d/c.json", lambda d: {"n": 1})
    assert store.list_dir("d") == ["c.json"]


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
    store.write_bytes("f", b"x")
    assert store.list_dir("f") == []


def test_stamp_changes_on_rewrite_and_missing_raises(store):
    import time

    with pytest.raises(StoreNotFound):
        store.stamp("s.json")
    store.write_json("s.json", {"v": 1})
    first = store.stamp("s.json")
    time.sleep(0.05)  # coarse filesystem timestamps
    store.write_json("s.json", {"v": 2, "more": "content"})
    assert store.stamp("s.json") != first
