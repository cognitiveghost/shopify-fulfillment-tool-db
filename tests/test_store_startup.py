"""Startup picks one storage mode per run (spec 7.3), and the window says when it fell back to the file share."""

from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from PySide6.QtWidgets import QApplication

import gui.main_window_pyside as window_module
from shared.store import FileStore
from shared.store_mode import Mode
from shopify_tool.profile_manager import resolve_base_path


@pytest.fixture
def startup(monkeypatch, tmp_path):
    """Stands in for open_store and ProfileManager; no share, no network, no sign-in."""
    seen = SimpleNamespace(open_calls=[], profile_calls=[], result=None, store=object())

    def fake_open_store(app_name, **kwargs):
        seen.open_calls.append(kwargs)
        return seen.result

    def fake_profile_manager(**kwargs):
        seen.profile_calls.append(kwargs)
        return Mock()

    monkeypatch.setattr(window_module, "open_store", fake_open_store)
    monkeypatch.setattr(window_module, "ProfileManager", fake_profile_manager)
    monkeypatch.setattr(window_module, "get_persistent_data_path", lambda name: str(tmp_path / name))
    monkeypatch.delenv("FULFILLMENT_STORE_MODE", raising=False)
    return seen


@pytest.mark.parametrize("mode", [Mode.FILE, Mode.NONE, Mode.OFFLINE_BUFFER])
def test_without_supabase_the_profile_manager_opens_as_before(startup, mode):
    startup.result = (mode, None)

    chosen, _manager = window_module.open_profile_manager(None)

    assert chosen is mode
    assert startup.profile_calls == [{"require_connection": False}]


def test_supabase_hands_its_store_to_the_profile_manager(startup):
    startup.result = (Mode.SUPABASE, startup.store)

    chosen, _manager = window_module.open_profile_manager(None)

    assert chosen is Mode.SUPABASE
    assert startup.profile_calls == [{"store": startup.store, "require_connection": False}]
    opened = startup.open_calls[0]
    assert opened["fallback_root"] == resolve_base_path()
    assert opened["cache_dir"].name == "db_cache"


def test_the_file_switch_never_asks_for_a_store(startup, monkeypatch):
    monkeypatch.setenv("FULFILLMENT_STORE_MODE", "File")

    chosen, _manager = window_module.open_profile_manager(None)

    assert chosen is None
    assert startup.open_calls == []
    assert startup.profile_calls == [{"require_connection": False}]


@pytest.fixture
def window_on_file_server(tmp_path, monkeypatch):
    """A real MainWindow over a throwaway share; the store mode is the test's choice."""
    monkeypatch.setenv("FULFILLMENT_SERVER_PATH", str(tmp_path / "server"))
    monkeypatch.setattr(window_module, "get_persistent_data_path", lambda name: str(tmp_path / name))
    monkeypatch.delenv("FULFILLMENT_STORE_MODE", raising=False)
    QApplication.instance() or QApplication([])

    def open_window(mode):
        monkeypatch.setattr(
            window_module, "open_store",
            lambda app_name, **kwargs: (mode, FileStore(tmp_path / "server") if mode is Mode.FILE else None),
        )
        win = window_module.MainWindow()
        win.show()
        QApplication.processEvents()
        return win

    yield open_window


def test_the_window_says_when_it_fell_back_to_the_file_server(window_on_file_server):
    win = window_on_file_server(Mode.FILE)
    try:
        assert win.store_mode is Mode.FILE
        assert not win.error_banner.isHidden()
        assert win.error_banner.headline() == "Database unreachable: working from file server"
    finally:
        win.close()


def test_no_store_at_all_is_a_degraded_window_without_the_fallback_banner(window_on_file_server):
    win = window_on_file_server(Mode.NONE)
    try:
        assert win.store_mode is Mode.NONE
        assert win.error_banner.isHidden()
    finally:
        win.close()
