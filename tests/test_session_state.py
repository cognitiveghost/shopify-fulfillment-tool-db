"""session_state owns current_state.pkl: stamp, stale check, atomic save."""

import pandas as pd
import pytest

from shared import atomic_write
from shared.store import FileStore
from shopify_tool import session_state
from shopify_tool.session_state import StaleSessionError


def _df(*statuses, order_numbers=None):
    n = len(statuses)
    return pd.DataFrame(
        {
            "Order_Number": order_numbers or [f"#{1001 + i}" for i in range(n)],
            "SKU": [f"SKU-{i}" for i in range(n)],
            "Quantity": [1] * n,
            "Order_Fulfillment_Status": list(statuses),
        }
    )


@pytest.fixture
def session(tmp_path):
    """A session folder on a FileStore rooted at tmp_path: (store, session path)."""
    folder = tmp_path / "Sessions" / "CLIENT_A" / "2026-11-13_1"
    folder.mkdir(parents=True)
    return FileStore(tmp_path), folder


def test_a_session_without_state_has_no_stamp(session):
    store, path = session
    assert session_state.state_stamp(store, path) is None


def test_save_returns_the_stamp_now_on_disk(session):
    store, path = session
    stamp = session_state.save_state(store, path, _df("Fulfillable"), None)
    assert stamp == session_state.state_stamp(store, path)
    assert pd.read_pickle(path / "analysis" / "current_state.pkl").equals(_df("Fulfillable"))


def test_a_save_from_a_stale_stamp_is_refused_and_writes_nothing(session):
    store, path = session
    first = session_state.save_state(store, path, _df("Fulfillable"), None)
    session_state.save_state(store, path, _df("Not Fulfillable"), first)  # another PC
    with pytest.raises(StaleSessionError):
        session_state.save_state(store, path, _df("Fulfillable", "Fulfillable"), first)
    saved = pd.read_pickle(path / "analysis" / "current_state.pkl")
    assert saved["Order_Fulfillment_Status"].tolist() == ["Not Fulfillable"]


def test_state_created_by_another_pc_makes_a_none_stamp_stale(session):
    store, path = session
    session_state.save_state(store, path, _df("Fulfillable"), None)
    assert session_state.is_stale(store, path, None)
    with pytest.raises(StaleSessionError):
        session_state.save_state(store, path, _df("Fulfillable"), None)


def test_a_failed_write_keeps_the_old_state(session, monkeypatch):
    store, path = session
    stamp = session_state.save_state(store, path, _df("Fulfillable"), None)

    def share_went_away(*_a, **_k):
        raise OSError("The specified network name is no longer available")

    monkeypatch.setattr(pd.DataFrame, "to_pickle", share_went_away)
    with pytest.raises(OSError):
        session_state.save_state(store, path, _df("Not Fulfillable"), stamp)
    assert session_state.state_stamp(store, path) == stamp
    assert not list((path / "analysis").glob(".current_state_tmp_*"))


def test_a_save_leaves_no_temp_file(session):
    store, path = session
    stamp = session_state.save_state(store, path, _df("Fulfillable"), None)
    session_state.save_state(store, path, _df("Not Fulfillable"), stamp)
    pkl = sorted(p.name for p in (path / "analysis").glob("*.pkl"))
    assert pkl == ["current_state.pkl"]


def test_order_counts_count_orders_not_lines(tmp_path):
    # #1001 has one blocked SKU line, so the whole order is blocked (R1).
    df = _df(
        "Fulfillable", "Not Fulfillable", "Fulfillable",
        order_numbers=["#1001", "#1001", "#1002"],
    )
    assert session_state.order_counts(df) == {
        "total_orders": 2,
        "fulfillable_orders": 1,
        "not_fulfillable_orders": 1,
    }


def test_a_replace_blocked_by_a_reader_is_retried(session, monkeypatch):
    # Windows refuses the replace while another PC has the file open to read.
    store, path = session
    real_replace = atomic_write.os.replace
    calls = []

    def reader_has_it_open_once(src, dst):
        calls.append(dst)
        if len(calls) == 1:
            raise PermissionError("The process cannot access the file")
        real_replace(src, dst)

    monkeypatch.setattr(atomic_write.os, "replace", reader_has_it_open_once)
    monkeypatch.setattr(atomic_write.time, "sleep", lambda _s: None)

    stamp = session_state.save_state(store, path, _df("Fulfillable"), None)

    assert len(calls) == 2 and stamp == session_state.state_stamp(store, path)
