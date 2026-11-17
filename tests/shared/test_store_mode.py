import itertools

import pytest

from shared import db_auth, store_mode
from shared.store import FileStore
from shared.store_mode import Mode, choose_mode


@pytest.mark.parametrize(
    ("pending", "ok", "fallback", "expected"),
    [
        (True, True, True, Mode.SUPABASE),
        (True, True, False, Mode.SUPABASE),
        (True, False, True, Mode.OFFLINE_BUFFER),
        (True, False, False, Mode.OFFLINE_BUFFER),
        (False, True, True, Mode.SUPABASE),
        (False, True, False, Mode.SUPABASE),
        (False, False, True, Mode.FILE),
        (False, False, False, Mode.NONE),
    ],
)
def test_choose_mode_table(pending, ok, fallback, expected):
    assert choose_mode(outbox_pending=pending, supabase_ok=ok, fallback_ok=fallback) is expected


def test_choose_mode_pending_outbox_never_file():
    assert choose_mode(outbox_pending=True, supabase_ok=False, fallback_ok=True) is Mode.OFFLINE_BUFFER
    for ok, fb in itertools.product((True, False), repeat=2):
        assert choose_mode(outbox_pending=True, supabase_ok=ok, fallback_ok=fb) is not Mode.FILE


def test_open_store_falls_back_to_file(monkeypatch, tmp_path):
    monkeypatch.setattr(store_mode.db_auth, "connect", lambda app, prompt: None)
    mode, store = store_mode.open_store("T", fallback_root=str(tmp_path), cache_dir=tmp_path, prompt=lambda: None)
    assert mode is Mode.FILE and isinstance(store, FileStore)


def test_open_store_none_without_either(monkeypatch, tmp_path):
    monkeypatch.setattr(store_mode.db_auth, "connect", lambda app, prompt: None)
    mode, store = store_mode.open_store(
        "T", fallback_root=str(tmp_path / "missing"), cache_dir=tmp_path, prompt=lambda: None)
    assert (mode, store) == (Mode.NONE, None)


def test_open_store_pending_outbox_offline_without_store(monkeypatch, tmp_path):
    monkeypatch.setattr(store_mode.db_auth, "connect", lambda app, prompt: None)
    mode, store = store_mode.open_store(
        "T", fallback_root=str(tmp_path), cache_dir=tmp_path, prompt=lambda: None, outbox_pending=True)
    assert (mode, store) == (Mode.OFFLINE_BUFFER, None)


def test_open_store_lets_sign_in_cancelled_through(monkeypatch, tmp_path):
    def cancelled(app, prompt):
        raise db_auth.SignInCancelled()

    monkeypatch.setattr(store_mode.db_auth, "connect", cancelled)
    with pytest.raises(db_auth.SignInCancelled):
        store_mode.open_store("T", fallback_root=str(tmp_path), cache_dir=tmp_path, prompt=lambda _error: None)


def test_open_store_supabase_when_signed_in(monkeypatch, tmp_path):
    monkeypatch.setattr(store_mode.db_auth, "connect", lambda app, prompt: object())
    mode, store = store_mode.open_store(
        "T", fallback_root=str(tmp_path), cache_dir=tmp_path, prompt=lambda: None, pc="pcA")
    assert mode is Mode.SUPABASE and store.pc == "pcA"
