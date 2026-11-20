"""Tag edits and their undo log nothing at WARNING+ and show no dialog on a real window over Supabase (DB trial)."""

import logging
import time

import pandas as pd
import pytest
from PySide6.QtWidgets import QApplication
from test_undo_roundtrip import COMPARED, EDITS, FRAME

import gui.main_window_pyside as window_module
import gui.session_browser_widget as browser_module
from shared.store_mode import Mode
from shared.store_supabase import SupabaseStore
from shopify_tool import session_state
from shopify_tool.profile_manager import ProfileManager

pytestmark = pytest.mark.supabase

TAG_EDITS = [e for e in EDITS if e[0] in ("bulk_add_tag", "bulk_remove_tag", "add_internal_tag", "remove_internal_tag")]


def _drain(win):
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline:
        QApplication.processEvents()
        if win.write_queue.flush(timeout=0.2):
            break
    QApplication.processEvents()


@pytest.mark.parametrize(("name", "edit"), TAG_EDITS, ids=[e[0] for e in TAG_EDITS])
def test_tag_edit_and_undo_log_nothing(supabase_client, qapp, tmp_path, monkeypatch, caplog, name, edit):
    store = SupabaseStore(supabase_client, pc="pc1", cache_dir=tmp_path / "cache")
    manager = ProfileManager(str(tmp_path / "config.ini"), store=store, require_connection=False)
    monkeypatch.setattr(window_module, "open_profile_manager", lambda _p: (Mode.SUPABASE, manager))
    monkeypatch.setattr(browser_module.SessionBrowserWidget, "USE_ASYNC", False)
    dialogs = []
    monkeypatch.setattr(window_module, "show_error", lambda *a, **k: dialogs.append(a[1]))

    win = window_module.MainWindow()
    try:
        win.show()
        QApplication.processEvents()
        win.profile_manager.create_client_profile("acme", "Client Acme")
        win.current_client_id = "acme"
        win.load_client_config("acme")
        path = win.session_manager.create_session("acme")
        frame = FRAME.assign(System_note="", Shipping_Provider="DHL", Warehouse_Name="W1")
        session_state.save_state(store, path, frame, None)
        win.load_existing_session(path)
        before = win.analysis_results_df[COMPARED].reset_index(drop=True).copy()

        caplog.clear()
        caplog.set_level(logging.WARNING)
        edit(win.actions_handler)
        _drain(win)
        changed = win.analysis_results_df[COMPARED].reset_index(drop=True)
        assert not changed.equals(before), "the edit did nothing"
        win.undo_last_operation()
        _drain(win)

        assert [r.getMessage() for r in caplog.records if r.levelno >= logging.WARNING] == []
        assert dialogs == []
        after = win.analysis_results_df[COMPARED].reset_index(drop=True)
        pd.testing.assert_frame_equal(after, before, check_dtype=False)
    finally:
        win.close()
