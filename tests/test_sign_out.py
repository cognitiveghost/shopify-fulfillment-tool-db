"""Sign out is in the overflow only in DB mode. Confirming forgets the sign-in and closes; cancelling does nothing."""

import pytest
from PySide6.QtWidgets import QApplication

from shared import db_auth
from shared.components.confirm_dialog import ConfirmDialog
from shared.store_mode import Mode


@pytest.fixture(scope="module", autouse=True)
def qapp():
    return QApplication.instance() or QApplication([])


@pytest.fixture
def window_in_mode(tmp_path, monkeypatch):
    """A real MainWindow whose store mode is forced, so the overflow is built for that mode."""
    monkeypatch.setenv("FULFILLMENT_SERVER_PATH", str(tmp_path))
    from gui import main_window_pyside
    from shopify_tool.profile_manager import ProfileManager

    def build(mode):
        monkeypatch.setattr(
            main_window_pyside,
            "open_profile_manager",
            lambda _parent: (mode, ProfileManager(require_connection=False)),
        )
        win = main_window_pyside.MainWindow()
        win.show()
        QApplication.processEvents()
        return win

    windows = []

    def factory(mode):
        win = build(mode)
        windows.append(win)
        return win

    yield factory
    for win in windows:
        win.close()


def _action_texts(win):
    return [action.text() for action in win.command_bar.overflow.actions()]


def _sign_out_action(win):
    return next(a for a in win.command_bar.overflow.actions() if a.text() == "Sign out")


def test_sign_out_item_in_db_mode(window_in_mode):
    assert "Sign out" in _action_texts(window_in_mode(Mode.SUPABASE))


@pytest.mark.parametrize("mode", [Mode.FILE, None])
def test_sign_out_item_absent_outside_db_mode(window_in_mode, mode):
    assert "Sign out" not in _action_texts(window_in_mode(mode))


def test_sign_out_confirm_forgets_and_closes(window_in_mode, monkeypatch):
    win = window_in_mode(Mode.SUPABASE)
    forgotten, closed = [], []
    monkeypatch.setattr(ConfirmDialog, "exec", lambda self: 1)
    monkeypatch.setattr(db_auth, "forget", lambda app_name: forgotten.append(app_name))
    monkeypatch.setattr(win, "close", lambda: closed.append(True) or True)

    _sign_out_action(win).trigger()

    assert forgotten == ["ShopifyTool-DB"]
    assert closed == [True]


def test_sign_out_cancel_does_nothing(window_in_mode, monkeypatch):
    win = window_in_mode(Mode.SUPABASE)
    forgotten, closed = [], []
    monkeypatch.setattr(ConfirmDialog, "exec", lambda self: 0)
    monkeypatch.setattr(db_auth, "forget", lambda app_name: forgotten.append(app_name))
    monkeypatch.setattr(win, "close", lambda: closed.append(True) or True)

    _sign_out_action(win).trigger()

    assert forgotten == []
    assert closed == []


def test_sign_out_keeps_the_sign_in_when_close_is_vetoed(window_in_mode, monkeypatch):
    win = window_in_mode(Mode.SUPABASE)
    forgotten = []
    monkeypatch.setattr(ConfirmDialog, "exec", lambda self: 1)
    monkeypatch.setattr(db_auth, "forget", lambda app_name: forgotten.append(app_name))
    monkeypatch.setattr(win, "close", lambda: False)

    _sign_out_action(win).trigger()

    assert forgotten == []
