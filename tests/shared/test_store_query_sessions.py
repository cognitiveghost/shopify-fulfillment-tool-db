"""FileStore.query_sessions: live sessions of a client, read through a per-client index cache."""
import json
import os
from pathlib import Path

import pytest

from shared.store import FileStore, StoreError, _exclusive_lock


@pytest.fixture
def store(tmp_path):
    return FileStore(tmp_path)


def _session_info(store, name, **fields):
    store.write_json(f"Sessions/CLIENT_X/{name}/session_info.json", fields)


def _folder(store, name) -> Path:
    return store.local_path(f"Sessions/CLIENT_X/{name}")


def _bump_mtime(path: Path):
    """Move a folder's mtime forward so the test doesn't depend on filesystem timestamp granularity."""
    t = path.stat().st_mtime + 5
    os.utime(path, (t, t))


def _names(store, client="X"):
    return sorted(s["session_name"] for s in store.query_sessions(client))


def test_an_unknown_client_has_no_sessions(store):
    assert store.query_sessions("NOBODY") == []


def test_each_session_comes_back_as_its_info_plus_name(store):
    _session_info(store, "s1", status="active", comments="a")
    assert store.query_sessions("X") == [{"session_name": "s1", "status": "active", "comments": "a"}]


def test_a_new_session_appears(store):
    _session_info(store, "s1", status="active")
    assert _names(store) == ["s1"]
    _session_info(store, "s2", status="active")
    assert _names(store) == ["s1", "s2"]


def test_a_changed_session_info_is_reread_once_its_folder_mtime_moves(store):
    _session_info(store, "s1", status="active")
    store.query_sessions("X")  # index now remembers s1

    _session_info(store, "s1", status="completed")
    _bump_mtime(_folder(store, "s1"))

    assert store.query_sessions("X") == [{"session_name": "s1", "status": "completed"}]


def test_a_session_moved_out_of_the_client_folder_is_not_listed(store):
    _session_info(store, "s1", status="active")
    _session_info(store, "s2", status="active")
    store.query_sessions("X")

    store.move("Sessions/CLIENT_X/s2", "Sessions/_archive/CLIENT_X/s2")

    assert _names(store) == ["s1"]


def test_a_folder_without_session_info_is_skipped_until_it_has_one(store):
    _session_info(store, "s1", status="active")
    stray = store.local_path("Sessions/CLIENT_X/stray")
    stray.mkdir()
    assert _names(store) == ["s1"]

    _session_info(store, "stray", status="active")
    _bump_mtime(stray)
    assert _names(store) == ["s1", "stray"]


def test_an_unreadable_session_info_is_tried_again_without_a_folder_change(store):
    _session_info(store, "s1", status="active")
    _session_info(store, "s2", status="active")
    torn = _folder(store, "s2") / "session_info.json"
    torn.write_text("{torn", encoding="utf-8")
    assert _names(store) == ["s1"]

    torn.write_text(json.dumps({"status": "active"}), encoding="utf-8")  # folder mtime unchanged

    assert _names(store) == ["s1", "s2"]


def test_a_stale_legacy_index_is_not_served(store):
    _session_info(store, "s1", status="completed")
    index = store.local_path("Sessions/CLIENT_X/session_index.json")
    index.write_text(json.dumps([{"session_name": "s1", "status": "active"}]), encoding="utf-8")

    assert store.query_sessions("X") == [{"session_name": "s1", "status": "completed"}]


def test_a_listing_survives_an_index_lock_it_cannot_take(store):
    _session_info(store, "s1", status="active")
    store.query_sessions("X")  # builds the index and its lock file
    _session_info(store, "s2", status="active")  # a session the index doesn't know yet
    lock = store.local_path("Sessions/CLIENT_X/session_index.json.lock")
    lock.unlink()
    lock.mkdir()  # a directory where the lock file goes: the lock can't be taken

    assert _names(store) == ["s1", "s2"]


def test_a_client_folder_that_cannot_be_listed_is_an_error_not_empty(store):
    client_dir = store.local_path("Sessions/CLIENT_X")
    client_dir.parent.mkdir(parents=True)
    client_dir.write_text("not a folder", encoding="utf-8")

    with pytest.raises(StoreError):
        store.query_sessions("X")


def test_a_lock_that_was_never_taken_is_not_released(tmp_path, monkeypatch):
    """AUDIT-07-L4: unlocking a lock this call never took is not ours to do."""
    import fcntl

    calls = []

    def flock(fd, op):
        calls.append(op)
        if op == fcntl.LOCK_EX:
            raise OSError("lock refused")

    monkeypatch.setattr(fcntl, "flock", flock)
    with pytest.raises(OSError, match="lock refused"), _exclusive_lock(tmp_path / "x.lock"):
        pass
    assert calls == [fcntl.LOCK_EX]
