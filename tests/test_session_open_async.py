"""Opening a session reads it on a worker; the window applies the result on the UI thread."""

import threading

import pandas as pd
import pytest
from PySide6.QtWidgets import QApplication
from test_session_restore import (  # noqa: F401  main_window is a fixture
    _session_with_inputs,
    main_window,
)

from gui import session_open
from shared.store import StoreError
from shopify_tool import session_state


@pytest.fixture(scope="module", autouse=True)
def qapp():
    return QApplication.instance() or QApplication([])


def _session_with_analysis(win, order="#1001"):
    path = _session_with_inputs(win)
    df = pd.DataFrame(
        {
            "Order_Number": [order],
            "Order_Fulfillment_Status": ["Fulfillable"],
            "Shipping_Provider": ["DHL"],
            "SKU": ["A"],
            "Quantity": [2],
            "System_note": [""],
            "Internal_Tags": ["[]"],
            "Lot_Details": [None],
            "Stock": [5],
            "Final_Stock": [3.0],
        }
    )
    session_state.save_state(win.profile_manager.store, path, df, None)
    return path


def test_open_session_returns_before_read(main_window, qtbot, monkeypatch):  # noqa: F811
    path = _session_with_analysis(main_window)
    release = threading.Event()
    real_read = session_open.read_session

    def blocked_read(*args, **kwargs):
        release.wait(5)
        return real_read(*args, **kwargs)

    monkeypatch.setattr(session_open, "read_session", blocked_read)
    main_window.open_session(path)

    assert main_window.analysis_results_df is None
    release.set()
    qtbot.waitUntil(lambda: main_window.analysis_results_df is not None, timeout=5000)
    assert main_window.analysis_results_df["Order_Number"].tolist() == ["#1001"]


def test_open_session_discards_stale_result(main_window, qtbot, monkeypatch):  # noqa: F811
    first = _session_with_analysis(main_window, order="#1001")
    second = _session_with_analysis(main_window, order="#2002")
    release_first = threading.Event()
    real_read = session_open.read_session

    def read_first_last(store, session_manager, path, config):
        if path == first:
            release_first.wait(5)
        return real_read(store, session_manager, path, config)

    monkeypatch.setattr(session_open, "read_session", read_first_last)
    main_window.open_session(first)
    main_window.open_session(second)
    qtbot.waitUntil(lambda: main_window.analysis_results_df is not None, timeout=5000)

    release_first.set()
    qtbot.waitUntil(lambda: not main_window._client_load_workers, timeout=5000)
    assert main_window.session_path == second
    assert main_window.analysis_results_df["Order_Number"].tolist() == ["#2002"]


def test_open_session_error_clears_loading(main_window, qtbot, monkeypatch):  # noqa: F811
    path = _session_with_analysis(main_window)
    shown = []

    def failing_read(*_args, **_kwargs):
        raise StoreError("the server is gone")

    monkeypatch.setattr(session_open, "read_session", failing_read)
    monkeypatch.setattr(
        "gui.main_window_pyside.show_error", lambda *args, **kwargs: shown.append(args)
    )
    main_window.open_session(path)

    qtbot.waitUntil(lambda: len(shown) == 1, timeout=5000)
    assert main_window.session_path is None
    assert not main_window.command_bar.loading_label.isVisible()


def test_read_session_reads_info_once(main_window, monkeypatch):  # noqa: F811
    path = _session_with_analysis(main_window)
    session_manager = main_window.session_manager
    real_get_info = session_manager.get_session_info
    calls = []

    def counting_get_info(session_path):
        calls.append(session_path)
        return real_get_info(session_path)

    monkeypatch.setattr(session_manager, "get_session_info", counting_get_info)
    loaded = session_open.read_session(
        main_window.profile_manager.store, session_manager, path, main_window.active_profile_config
    )

    assert len(calls) == 1
    assert loaded.analysis_df is not None


def test_read_session_downloads_stock_once(main_window, monkeypatch):  # noqa: F811
    path = _session_with_analysis(main_window)
    store = main_window.profile_manager.store
    stock_calls = []
    for name in ("local_path", "read_bytes"):
        real = getattr(store, name)

        def counting(rel, *args, _real=real, _name=name, **kwargs):
            if rel.endswith("inventory.csv"):
                stock_calls.append(_name)
            return _real(rel, *args, **kwargs)

        monkeypatch.setattr(store, name, counting)
    loaded = session_open.read_session(store, main_window.session_manager, path, main_window.active_profile_config)

    assert loaded.stock_path is not None
    assert len(stock_calls) == 1


def test_apply_session_does_not_reread_session_info(main_window, monkeypatch):  # noqa: F811
    path = _session_with_analysis(main_window)
    session_manager = main_window.session_manager
    loaded = session_open.read_session(
        main_window.profile_manager.store, session_manager, path, main_window.active_profile_config
    )
    reads = []
    real_get_info = session_manager.get_session_info
    monkeypatch.setattr(
        session_manager, "get_session_info", lambda p: reads.append(p) or real_get_info(p)
    )
    main_window._apply_session(path, loaded)

    assert reads == []
    assert main_window.session_facts is not None


def test_load_existing_session_still_synchronous(main_window):  # noqa: F811
    path = _session_with_analysis(main_window)
    main_window.load_existing_session(path)

    assert main_window.analysis_results_df is not None
