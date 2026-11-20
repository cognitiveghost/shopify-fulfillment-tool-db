"""An analysis run flushes pending writes on its worker, and reads history once."""

import threading
from types import SimpleNamespace
from unittest.mock import Mock

import pandas as pd
import pytest
from PySide6.QtCore import QCoreApplication, QThreadPool

from gui import actions_handler
from gui.actions_handler import ActionsHandler
from shared.store import FileStore
from shopify_tool import core, fulfillment_history

WAIT_TEXT = "Your last changes are still being saved. Run the analysis again in a moment."


def _window(queue, threadpool):
    return SimpleNamespace(
        session_path="/sessions/2026-09-30_1",
        current_client_id="acme",
        _analysis_running=False,
        _analysis_step=0,
        _analysis_cancelling=False,
        active_profile_config={},
        stock_file_path="/d/stock.csv",
        orders_file_path="/d/orders.csv",
        session_manager=Mock(),
        profile_manager=Mock(),
        threadpool=threadpool,
        command_bar=Mock(),
        ui_manager=Mock(),
        sync_inventory_memory=Mock(),
        write_queue=queue,
    )


def _drain(mw):
    """Wait for the worker, then let its queued signals reach the GUI slots."""
    assert mw.threadpool.waitForDone(5000)
    for _ in range(3):
        QCoreApplication.processEvents()


@pytest.fixture
def gui(monkeypatch, qapp):
    toast, show_error = Mock(), Mock()
    monkeypatch.setattr(actions_handler, "toast", toast)
    monkeypatch.setattr(actions_handler, "show_error", show_error)
    return SimpleNamespace(toast=toast, show_error=show_error)


def test_flush_runs_off_ui_thread(monkeypatch, gui):
    flushed_on = []
    queue = SimpleNamespace(
        flush=lambda timeout: flushed_on.append(threading.current_thread()) or True,
        pending=list,
    )
    monkeypatch.setattr(core, "run_full_analysis", Mock(return_value=(False, core.CANCELLED, None, None)))
    mw = _window(queue, QThreadPool())
    ActionsHandler(mw).run_analysis()
    _drain(mw)

    assert flushed_on, "the run never flushed the write queue"
    assert flushed_on[0] is not threading.main_thread()


def test_flush_timeout_still_stops_analysis(monkeypatch, gui):
    run = Mock()
    monkeypatch.setattr(core, "run_full_analysis", run)
    queue = SimpleNamespace(flush=lambda timeout: False, pending=lambda: [("S", "history")])
    mw = _window(queue, QThreadPool())
    ActionsHandler(mw).run_analysis()
    _drain(mw)

    run.assert_not_called()
    gui.toast.assert_called_once()
    assert gui.toast.call_args.args[1] == WAIT_TEXT
    gui.show_error.assert_not_called()


def _legacy_inputs(tmp_path, monkeypatch):
    """A legacy-mode run (no profile): history lives in tmp_path."""
    history = tmp_path / "fulfillment_history.csv"
    pd.DataFrame(columns=fulfillment_history.COLUMNS).to_csv(history, index=False)
    monkeypatch.setattr(fulfillment_history, "get_persistent_data_path", lambda _name: history)
    monkeypatch.setattr(
        core,
        "load_session_signals",
        lambda _pm, _cid: (pd.DataFrame(columns=["Order_Number", "Execution_Date", "Session"]), None),
    )
    orders = tmp_path / "orders.csv"
    pd.DataFrame(
        [{"Name": "#1", "Lineitem sku": "A1", "Lineitem quantity": 1, "Shipping Method": "Standard"}]
    ).to_csv(orders, index=False)
    stock = tmp_path / "stock.csv"
    pd.DataFrame([{"SKU": "A1", "Product_Name": "Widget", "Stock": 5}]).to_csv(stock, index=False)
    config = {
        "settings": {"repeat_detection_days": 1},
        "column_mappings": {
            "orders": {
                "Name": "Order_Number",
                "Lineitem sku": "SKU",
                "Lineitem quantity": "Quantity",
                "Shipping Method": "Shipping_Method",
            },
            "stock": {"SKU": "SKU", "Product_Name": "Product_Name", "Stock": "Stock"},
        },
    }
    return stock, orders, config


def test_history_read_once(tmp_path, monkeypatch):
    stock, orders, config = _legacy_inputs(tmp_path, monkeypatch)
    reads = []
    real_read = FileStore.read_bytes

    def counting_read(self, rel):
        if rel.endswith("fulfillment_history.csv"):
            reads.append(rel)
        return real_read(self, rel)

    monkeypatch.setattr(FileStore, "read_bytes", counting_read)

    ok, msg, *_ = core.run_full_analysis(
        str(stock), str(orders), str(tmp_path / "out"), ",", ",", config
    )

    assert ok, msg
    assert len(reads) == 1
