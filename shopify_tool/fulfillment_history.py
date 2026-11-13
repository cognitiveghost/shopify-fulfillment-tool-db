"""Per-client fulfillment history: which orders each session currently ships.

One row per order per session (ADR 0012). A session's rows are replaced
whenever its state is saved, so history follows what the session ships, not
what its run proposed (AUDIT-02-3, -4). Rows written before sessions were
recorded have Session "" and keep the old date rule in detection.

The file is never written over when it can't be read (AUDIT-02-6), and every
write re-reads and then writes only if the file is still the one it read, so
two PCs don't lose each other's rows (AUDIT-02-7). A conflict re-reads and
merges again.
"""

import io
import logging
import os
from datetime import datetime
from pathlib import Path

import pandas as pd

from shared.store import FileStore, Store, StoreConflict, StoreError, StoreNotFound
from shopify_tool import stock_ledger
from shopify_tool.utils import get_persistent_data_path

logger = logging.getLogger(__name__)

COLUMNS = ["Order_Number", "Execution_Date", "Session"]
FILE_NAME = "fulfillment_history.csv"
WRITE_ATTEMPTS = 5


class HistoryUnreadable(Exception):
    """The history file exists but can't be read; it must not be written over."""


def history_location(profile_manager, client_id) -> tuple[Store, str]:
    """The store and rel path of the history file: the client's, or the local app-data one."""
    if profile_manager and client_id:
        store = profile_manager.store
        client_file = Path(profile_manager.get_client_directory(client_id)) / FILE_NAME
        return store, client_file.relative_to(store.local_path("")).as_posix()
    local = Path(get_persistent_data_path(FILE_NAME))
    return FileStore(local.parent), local.name


def _empty() -> pd.DataFrame:
    return pd.DataFrame(columns=COLUMNS)


def load(store: Store, rel: str) -> pd.DataFrame:
    try:
        raw = store.read_bytes(rel)
    except StoreNotFound:
        return _empty()
    except StoreError as e:  # the share, not the file's content
        raise HistoryUnreadable(f"{rel}: {e}") from e
    try:
        df = pd.read_csv(io.BytesIO(raw), dtype=str, keep_default_na=False, encoding="utf-8-sig")
    except pd.errors.EmptyDataError:
        return _empty()  # a 0-byte file holds nothing to lose
    except Exception as e:  # ParserError, UnicodeDecodeError
        raise HistoryUnreadable(f"{rel}: {e}") from e
    if not isinstance(df.index, pd.RangeIndex):
        # Every row has more fields than the header: pandas made the first
        # column the index, and the data would be written back shifted.
        raise HistoryUnreadable(f"{rel}: rows have more fields than the header")
    if "Order_Number" not in df.columns:
        raise HistoryUnreadable(f"{rel}: no Order_Number column")
    df = df.reindex(columns=COLUMNS, fill_value="").fillna("")
    df["Order_Number"] = df["Order_Number"].str.strip()
    return df


def merge_earliest(history_df, new_rows) -> pd.DataFrame:
    """Earliest date per order; NaT sorts last (moved from core, unchanged)."""
    combined = pd.concat([history_df, new_rows])
    key = pd.to_datetime(combined["Execution_Date"], errors="coerce", format="mixed")
    combined = combined.assign(_k=key).sort_values("_k", na_position="last")
    return combined.drop_duplicates(subset=["Order_Number"], keep="first").drop(columns="_k")


def _replace(history, session, ships, today) -> pd.DataFrame:
    if session is None:
        legacy = history["Session"] == ""
        new = pd.DataFrame({"Order_Number": sorted(ships), "Execution_Date": today, "Session": ""})
        return pd.concat([history[~legacy], merge_earliest(history[legacy], new)])
    mine = history["Session"] == session
    first_seen = dict(zip(history.loc[mine, "Order_Number"], history.loc[mine, "Execution_Date"]))
    new = pd.DataFrame(
        {
            "Order_Number": sorted(ships),
            "Execution_Date": [first_seen.get(o) or today for o in sorted(ships)],
            "Session": session,
        }
    )
    return pd.concat([history[~mine], new])


def _csv_bytes(df: pd.DataFrame) -> bytes:
    return df.to_csv(index=False, columns=COLUMNS).encode("utf-8")


def session_folders(sessions_dir: Path, current: str | None) -> set[str] | None:
    """The names of the session folders in `sessions_dir`, from one listing.

    None when the folder can't be listed, or when `current` is given and is
    not among them: a listing that misses the session being saved is of the
    wrong or an unreachable folder, and must not prune anything.
    """
    try:
        with os.scandir(sessions_dir) as it:
            names = {entry.name for entry in it if entry.is_dir()}
    except OSError:
        logger.warning(f"Could not list session folders in {sessions_dir}", exc_info=True)
        return None
    if current is not None and current not in names:
        logger.warning(f"Session folder listing of {sessions_dir} lacks {current!r}; not pruning history")
        return None
    return names


def _stamp(store: Store, rel: str):
    try:
        return store.stamp(rel)
    except StoreNotFound:
        return None


def record_session(store: Store, rel: str, session, df, today=None, existing_sessions: set[str] | None = None) -> bool:
    """Replace `session`'s rows with the orders df ships. False = not written.

    `existing_sessions`, given by the run only: rows of a session whose folder
    is gone are dropped (AUDIT-07-L1). Rows without a session, and rows of
    sessions that still exist (abandoned ones too), stay. ADR 0012 rule 5:
    nothing is pruned by age.
    """
    today = today or datetime.now().astimezone().strftime("%Y-%m-%d")
    try:
        ships = stock_ledger.fulfillable_orders(df)
        for _ in range(WRITE_ATTEMPTS):
            # Stamp before reading: a write by another PC after this line makes
            # the stamp stale, so the write below conflicts and re-reads.
            # ponytail: same stamp caveat as session_state.state_stamp (same-size writes in one file-time tick).
            loaded = _stamp(store, rel)
            try:
                history = load(store, rel)
            except HistoryUnreadable:
                logger.warning(f"Fulfillment history unreadable; not writing over it: {rel}", exc_info=True)
                return False
            if existing_sessions is not None:
                gone = (history["Session"] != "") & ~history["Session"].isin(existing_sessions)
                if gone.any():
                    logger.info(f"Dropping {int(gone.sum())} history rows of deleted sessions")
                history = history[~gone]
            try:
                store.write_bytes_if(rel, _csv_bytes(_replace(history, session, ships, today)), loaded)
                return True
            except StoreConflict:
                continue  # another PC wrote since our read: merge onto its rows
        logger.warning(f"Fulfillment history kept changing; not recorded for {session!r}")
        return False
    except Exception:  # StoreError, anything pandas raises: never fail the caller
        logger.exception(f"Could not record fulfillment history for {session!r}")
        return False
