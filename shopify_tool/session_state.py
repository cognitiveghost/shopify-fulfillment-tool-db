"""A session's saved state (analysis/current_state.pkl) and its stale check.

Every edit rewrites the whole state from one PC's memory, so a PC that loaded
it before another PC saved would silently undo that PC's edits (AUDIT-05-3).
Each PC keeps the stamp it last loaded or saved; a save against a different
file on disk is refused. ADR 0011.
"""

import io
from pathlib import Path

from shared.store import Store, StoreConflict, StoreNotFound
from shopify_tool import stock_ledger

STATE_FILE = "current_state.pkl"


class StaleSessionError(Exception):
    """Another PC saved this session's state after this PC loaded it."""


def session_rel(store: Store, session_path) -> str:
    """Store path (POSIX, relative to the server root) of a session folder.

    Callers hand in the absolute folder path the GUI holds. A folder that is
    the root itself gives "." (which the Store accepts as a path prefix).
    """
    return Path(session_path).relative_to(store.local_path("")).as_posix()


def _state_rel(store: Store, session_path) -> str:
    return f"{session_rel(store, session_path)}/analysis/{STATE_FILE}"


def state_stamp(store: Store, session_path):
    """Change token of the saved state, or None when there is none yet.

    ponytail: two same-size saves inside one file-time tick share a stamp;
    upgrade to a version token written with the state if that ever shows up.
    """
    try:
        return store.stamp(_state_rel(store, session_path))
    except StoreNotFound:
        return None


def is_stale(store: Store, session_path, loaded_stamp) -> bool:
    return state_stamp(store, session_path) != loaded_stamp


def save_state(store: Store, session_path, df, loaded_stamp):
    """Write df as the session's state, atomically; return the new stamp.

    Raises StaleSessionError, writing nothing, when the state on disk is not
    the one this PC loaded or last saved. The Store checks the stamp and
    writes under one lock, so two PCs can't both pass the check and both write.
    """
    buf = io.BytesIO()
    df.to_pickle(buf)
    try:
        return store.write_bytes_if(_state_rel(store, session_path), buf.getvalue(), loaded_stamp)
    except StoreConflict as e:
        raise StaleSessionError(str(e)) from e


def order_counts(df) -> dict:
    """The session_info.json order counts, by order (R1), for the browser."""
    total = df["Order_Number"].astype(str).str.strip().nunique()
    fulfillable = len(stock_ledger.fulfillable_orders(df))
    return {
        "total_orders": total,
        "fulfillable_orders": fulfillable,
        "not_fulfillable_orders": total - fulfillable,
    }
