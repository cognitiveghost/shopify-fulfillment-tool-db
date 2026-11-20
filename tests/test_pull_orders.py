"""Pull orders: the store's open orders become the session's orders input."""

import logging

import pandas as pd
import pytest
from PySide6.QtCore import QThreadPool
from PySide6.QtWidgets import QApplication

from gui.pull_preview_dialog import PullPreviewDialog
from shopify_tool.order_sources import SHOPIFY_CSV_HEADERS, OrderSourceError

TOKEN = "shpat_secret_token_value"
SOURCE = {"platform": "shopify", "shop": "fulfil-trial.myshopify.com", "token": TOKEN}


def _orders(lines: int = 3) -> pd.DataFrame:
    rows = [
        {h: "" for h in SHOPIFY_CSV_HEADERS}
        | {
            "Name": f"#{1001 + i // 2}",
            "Lineitem sku": f"SKU-{i}",
            "Lineitem quantity": 1,
            "Shipping Method": "Standard",
        }
        for i in range(lines)
    ]
    return pd.DataFrame(rows, columns=list(SHOPIFY_CSV_HEADERS))


@pytest.fixture(scope="module", autouse=True)
def qapp():
    return QApplication.instance() or QApplication([])


@pytest.fixture
def main_window(tmp_path, monkeypatch):
    monkeypatch.setenv("FULFILLMENT_SERVER_PATH", str(tmp_path))
    from gui.main_window_pyside import MainWindow

    win = MainWindow()
    win.profile_manager.create_client_profile("acme", "Client Acme")
    win.current_client_id = "acme"
    win.load_client_config("acme")
    win.session_path = win.session_manager.create_session("acme")
    win.active_profile_config["order_source"] = dict(SOURCE)
    yield win
    win.close()


@pytest.fixture
def errors(monkeypatch):
    shown = []
    monkeypatch.setattr(
        "gui.file_handler.show_error", lambda _src, head, text: shown.append((head, text))
    )
    return shown


def _names(df: pd.DataFrame) -> list[str]:
    return list(df["Name"].unique())


def _stub_dialog(monkeypatch, answer) -> list:
    """Stub the preview: `answer(df)` gives the ticked names, None for Cancel. Returns the calls."""
    calls = []

    def ask(cls, parent, df, shop):
        calls.append(df)
        return answer(df)

    monkeypatch.setattr(PullPreviewDialog, "ask", classmethod(ask))
    return calls


@pytest.fixture(autouse=True)
def dialog_ticks_all(monkeypatch):
    return _stub_dialog(monkeypatch, _names)


def _pull(win):
    win.file_handler.pull_orders()
    # Fetch, then (after the preview) write: each is a worker whose result arrives on the event loop.
    for _ in range(3):
        QThreadPool.globalInstance().waitForDone()
        QApplication.processEvents()


def _input_path(win, session_path=None):
    from shopify_tool import session_state

    store = win.profile_manager.store
    session_path = session_path or win.session_path
    rel = f"{session_state.session_rel(store, session_path)}/input/orders_export.csv"
    return store.local_path(rel)


def _input(win) -> str:
    return _input_path(win).read_text(encoding="utf-8")


def test_pull_writes_input_and_runs_analysis(main_window, monkeypatch, errors):
    monkeypatch.setattr("gui.file_handler.fetch_orders", lambda source: _orders(3))

    _pull(main_window)

    assert not errors
    assert _input(main_window).splitlines()[0] == ",".join(SHOPIFY_CSV_HEADERS)
    assert main_window.orders_file_path.endswith("orders_export.csv")
    # Went down the picked-file route: the slot validated and counted it.
    assert main_window.orders_slot.is_valid
    assert main_window.orders_slot.rows == 3
    assert main_window.orders_slot.keys == 2


def test_pull_replaces_previous_input(main_window, monkeypatch, errors):
    pulls = iter([_orders(4), _orders(1)])
    monkeypatch.setattr("gui.file_handler.fetch_orders", lambda source: next(pulls))

    _pull(main_window)
    _pull(main_window)

    assert not errors
    assert len(_input(main_window).splitlines()) == 2  # header + the one line
    assert main_window.orders_slot.rows == 1


def test_pull_without_order_source_errors(main_window, monkeypatch, errors):
    main_window.active_profile_config.pop("order_source")
    monkeypatch.setattr(
        "gui.file_handler.fetch_orders", lambda source: pytest.fail("must not fetch")
    )

    _pull(main_window)

    assert [text for _head, text in errors] == [
        "No store connected for this client. Set it in Client settings."
    ]
    assert main_window.orders_file_path is None


def test_a_failed_fetch_is_shown_not_raised(main_window, monkeypatch, errors):
    def refuse(source):
        raise OrderSourceError("Shopify rejected the access token (HTTP 401).")

    monkeypatch.setattr("gui.file_handler.fetch_orders", refuse)

    _pull(main_window)

    assert errors == [
        ("Orders weren't pulled", "Shopify rejected the access token (HTTP 401).")
    ]
    assert main_window.orders_file_path is None


def test_token_not_logged(main_window, monkeypatch, errors, caplog):
    monkeypatch.setattr("gui.file_handler.fetch_orders", lambda source: _orders(2))
    caplog.set_level(logging.DEBUG)

    _pull(main_window)

    assert TOKEN not in caplog.text


def test_only_a_configured_client_offers_the_button(main_window):
    main_window.ui_manager.refresh_setup()
    assert main_window.setup_bridge.state["can_pull"] is True
    main_window.active_profile_config.pop("order_source")
    main_window.ui_manager.refresh_setup()
    assert main_window.setup_bridge.state["can_pull"] is False


def test_pull_writes_with_the_clients_delimiter(main_window, monkeypatch, errors):
    main_window.active_profile_config.setdefault("settings", {})["orders_csv_delimiter"] = ";"
    monkeypatch.setattr("gui.file_handler.fetch_orders", lambda source: _orders(3))

    _pull(main_window)

    assert not errors
    assert _input(main_window).splitlines()[0] == ";".join(SHOPIFY_CSV_HEADERS)
    assert main_window.orders_slot.is_valid
    assert main_window.orders_slot.rows == 3


def test_a_pull_finishing_after_the_session_changed_is_not_loaded(main_window, monkeypatch, errors):
    pulled_for = main_window.session_path
    other = main_window.session_manager.create_session("acme")

    def fetch_then_switch(source):
        main_window.session_path = other  # the user opened another session meanwhile
        return _orders(2)

    monkeypatch.setattr("gui.file_handler.fetch_orders", fetch_then_switch)

    _pull(main_window)

    assert main_window.orders_file_path is None
    assert not _input_path(main_window, pulled_for).exists()  # nothing written until the preview is confirmed


def test_no_open_orders_says_so_and_loads_nothing(main_window, monkeypatch, errors):
    shown = []
    monkeypatch.setattr("gui.file_handler.toast", lambda _src, text: shown.append(text))
    monkeypatch.setattr("gui.file_handler.fetch_orders", lambda source: _orders(0))

    _pull(main_window)

    assert not errors
    assert shown == ["No open unfulfilled orders in fulfil-trial.myshopify.com"]
    assert main_window.orders_file_path is None


def test_subset_writes_only_selected(main_window, monkeypatch, errors):
    shown = []
    monkeypatch.setattr("gui.file_handler.toast", lambda _src, text: shown.append(text))
    _stub_dialog(monkeypatch, lambda df: ["#1001"])
    monkeypatch.setattr("gui.file_handler.fetch_orders", lambda source: _orders(3))

    _pull(main_window)

    assert not errors
    written = pd.read_csv(_input_path(main_window), dtype=str)
    assert set(written["Name"]) == {"#1001"}
    assert len(written) == 2  # #1001's two lines
    assert shown == ["Pulled 1 orders (2 lines) from fulfil-trial.myshopify.com"]


def test_cancel_writes_nothing(main_window, monkeypatch, errors):
    monkeypatch.setattr("gui.file_handler.fetch_orders", lambda source: _orders(3))
    _pull(main_window)  # an earlier input, kept
    before = _input_path(main_window).read_bytes()

    _stub_dialog(monkeypatch, lambda df: None)
    monkeypatch.setattr("gui.file_handler.fetch_orders", lambda source: _orders(2))
    _pull(main_window)

    assert not errors
    assert _input_path(main_window).read_bytes() == before
    assert main_window.file_handler._pull_worker is None


def test_cancel_then_pull_again(main_window, monkeypatch, errors):
    calls = _stub_dialog(monkeypatch, lambda df: None)
    monkeypatch.setattr("gui.file_handler.fetch_orders", lambda source: _orders(3))

    _pull(main_window)
    _pull(main_window)

    assert not errors
    assert len(calls) == 2


def test_empty_shows_toast_no_dialog(main_window, monkeypatch, errors):
    shown = []
    monkeypatch.setattr("gui.file_handler.toast", lambda _src, text: shown.append(text))
    calls = _stub_dialog(monkeypatch, lambda df: [])
    monkeypatch.setattr("gui.file_handler.fetch_orders", lambda source: _orders(0))

    _pull(main_window)

    assert calls == []
    assert shown == ["No open unfulfilled orders in fulfil-trial.myshopify.com"]


def test_session_changed_during_dialog_writes_nothing(main_window, monkeypatch, errors):
    pulled_for = main_window.session_path
    other = main_window.session_manager.create_session("acme")

    def switch_then_tick_all(df):
        main_window.session_path = other  # the user opened another session while the preview was up
        return _names(df)

    _stub_dialog(monkeypatch, switch_then_tick_all)
    monkeypatch.setattr("gui.file_handler.fetch_orders", lambda source: _orders(2))

    _pull(main_window)

    assert main_window.orders_file_path is None
    assert not _input_path(main_window, pulled_for).exists()
    assert not _input_path(main_window, other).exists()
    assert main_window.file_handler._pull_worker is None
