"""Reading a past session's files, off the UI thread.

read_session touches the store and the session manager, never a widget. The
window applies its result in MainWindow._apply_session on the UI thread.
"""

import io
import logging
from dataclasses import dataclass
from typing import Any

import pandas as pd

from shared.store import StoreNotFound
from shopify_tool import core, session_state
from shopify_tool.analysis import recalculate_statistics
from shopify_tool.undo_manager import read_session_history

logger = logging.getLogger(__name__)


@dataclass
class SessionLoad:
    session_info: dict | None
    stock_path: str | None  # store.local_path of input/<stock_file>, None if absent
    analysis_df: pd.DataFrame | None
    analysis_stats: dict | None
    state_stamp: Any  # session_state.state_stamp, taken BEFORE the analysis read (ADR 0011)
    lot_table: Any  # core.session_lot_table, only when analysis_df is not None
    undo_history: dict | None


def read_session(store, session_manager, session_path: str, config: dict | None) -> SessionLoad:
    """Everything opening a session reads from the store, in one pass."""
    session_info = session_manager.get_session_info(session_path)
    loaded = SessionLoad(session_info, None, None, None, None, None, read_session_history(store, session_path))
    if not session_info:
        return loaded
    loaded.stock_path = _stock_local_path(store, session_manager, session_path, session_info)
    loaded.state_stamp = session_state.state_stamp(store, session_path)
    found = _read_analysis(store, session_path)
    if found is not None:
        loaded.analysis_df, loaded.analysis_stats = found
        loaded.lot_table = core.session_lot_table(store, session_path, config)
    return loaded


def _stock_local_path(store, session_manager, session_path: str, session_info: dict) -> str | None:
    """The stock file the session ran on, by the name session_info records (inventory.csv if none)."""
    try:
        input_dir = session_manager.get_input_dir(session_path)
    except Exception:
        logger.exception("Could not resolve the session input directory")
        return None
    rel = f"{session_state.session_rel(store, input_dir)}/{session_info.get('stock_file') or 'inventory.csv'}"
    if not store.exists(rel):
        return None
    # The slot's validator reads the file by path, so it gets the store's local path.
    return str(store.local_path(rel))


def _read_analysis(store, session_path: str) -> tuple[pd.DataFrame, dict] | None:
    """The session's analysis, or None. Priority: current_state.pkl, then the run's report.

    A failure is logged and read as no analysis, as it always was. The state
    file wins: it reflects the latest edits (AUDIT-07-H2 for the report).
    """
    try:
        analysis_dir = f"{session_state.session_rel(store, session_path)}/analysis"
        found = _read_state_pickle(store, analysis_dir)
        if found is None:
            found = _read_report(store, analysis_dir)
        return found
    except Exception:
        logger.exception("Failed to load session analysis")
        return None


def _read_state_pickle(store, analysis_dir: str) -> tuple[pd.DataFrame, dict] | None:
    try:
        # Written by this app; any signed-in PC can write the bucket, so a planted pickle would run code.
        df = pd.read_pickle(io.BytesIO(store.read_bytes(f"{analysis_dir}/current_state.pkl")))  # noqa: S301
        stats = _read_stats(store, analysis_dir, df)
    except StoreNotFound:
        return None
    except Exception as e:
        logger.warning(f"Failed to load pickle, trying the run's report: {e}")
        return None
    logger.info(f"Loaded {len(df)} rows from current_state.pkl")
    return df, stats


def _read_stats(store, analysis_dir: str, df: pd.DataFrame) -> dict:
    try:
        return store.read_json(f"{analysis_dir}/analysis_stats.json")
    except StoreNotFound:
        logger.info("Statistics file not found - recalculating")
        return recalculate_statistics(df)


def _read_report(store, analysis_dir: str) -> tuple[pd.DataFrame, dict] | None:
    """The run's report, gated on analysis_data.json. Not current_state.xlsx (AUDIT-07-H2)."""
    if not store.exists(f"{analysis_dir}/analysis_data.json"):
        logger.warning(f"Analysis data not found: {analysis_dir}/analysis_data.json")
        return None
    for name in ("fulfillment_analysis.xlsx", "analysis_report.xlsx"):
        try:
            raw = store.read_bytes(f"{analysis_dir}/{name}")
        except StoreNotFound:
            continue
        logger.info(f"Loading analysis from original report: {analysis_dir}/{name}")
        df = pd.read_excel(io.BytesIO(raw))
        logger.info(f"Loaded {len(df)} rows from session")
        return df, recalculate_statistics(df)
    logger.warning(f"Analysis report not found: {analysis_dir}/fulfillment_analysis.xlsx")
    return None
