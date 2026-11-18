"""
Unified Statistics Manager for Shopify Tool and Packing Tool

Canonical version. This module lives in shopify-fulfillment-tool/shared/ and
is mirrored into packing-tool/shared/ by packing-tool/scripts/sync_shared.py.
Never hand-edit the packing-tool copy. See shared/README.md.

Manages centralized statistics stored on the file server in
Stats/global_stats.json:
- Centralized storage on file server
- File locking for concurrent access from multiple PCs
- Separate tracking for analysis (Shopify Tool) and packing operations (Packing Tool)
- Per-client statistics breakdown
- Thread-safe and process-safe operations

Usage:
    # In Shopify Tool
    stats_manager = StatsManager(store)
    stats_manager.record_analysis(
        client_id="M",
        session_id="2025-11-05_1",
        orders_count=150,
        metadata={...}
    )

    # In Packing Tool
    stats_manager = StatsManager(store)
    stats_manager.record_packing(
        client_id="M",
        session_id="2025-11-05_1",
        worker_id="001",
        orders_count=142,
        items_count=450,
        metadata={...}
    )
"""

import time
from datetime import datetime
from typing import Any

from shared.file_lock import FileLockError  # noqa: F401  re-exported; tests check it is the shared class
from shared.metadata_utils import get_current_timestamp, parse_timestamp
from shared.store import (
    Delta,
    FileStore,
    Store,
    StoreError,
    StoreNotFound,
    StoreUnreachable,
    delta,
)

STATS_REL = "Stats/global_stats.json"


def _default_stats() -> dict[str, Any]:
    """Get default statistics structure."""
    return {
        "total_orders_analyzed": 0,
        "total_orders_packed": 0,
        "total_sessions": 0,
        "total_labels_printed": 0,
        "by_client": {},
        "analysis_history": [],
        "packing_history": [],
        "label_print_history": [],
        "last_updated": get_current_timestamp(),
        "version": "2.0",
    }


def _stats_with_defaults(stats: Any) -> dict[str, Any]:
    """Stats as read, with missing keys filled in; defaults if absent or not a dict."""
    if not isinstance(stats, dict):
        return _default_stats()
    default = _default_stats()
    for key in default:
        if key not in stats:
            stats[key] = default[key]
    return stats


@delta("stats.record_packing")
def _record_packing_delta(
    current, *, client_id, session_id, worker_id, orders_count, items_count, metadata, timestamp
) -> dict[str, Any]:
    stats = _stats_with_defaults(current)
    stats["total_orders_packed"] += orders_count
    stats["total_sessions"] += 1

    if client_id not in stats["by_client"]:
        stats["by_client"][client_id] = {
            "orders_analyzed": 0,
            "orders_packed": 0,
            "sessions": 0,
        }

    stats["by_client"][client_id]["orders_packed"] += orders_count
    stats["by_client"][client_id]["sessions"] += 1

    record = {
        "timestamp": timestamp,
        "client_id": client_id,
        "session_id": session_id,
        "worker_id": worker_id,
        "orders_count": orders_count,
        "items_count": items_count,
    }

    if metadata:
        record["metadata"] = metadata

    stats["packing_history"].append(record)

    if len(stats["packing_history"]) > 1000:
        stats["packing_history"] = stats["packing_history"][-1000:]

    stats["last_updated"] = timestamp
    return stats


class StatsManagerError(Exception):
    """Base exception for StatsManager errors."""


class StatsManager:
    """
    Unified statistics manager for both Shopify Tool and Packing Tool.

    Manages centralized statistics stored in Stats/global_stats.json on the
    file server. Provides thread-safe and process-safe operations using
    file locking.

    Structure of global_stats.json:
    {
        "total_orders_analyzed": 5420,      # From Shopify Tool
        "total_orders_packed": 4890,        # From Packing Tool
        "total_sessions": 312,
        "total_labels_printed": 88,
        "by_client": {
            "M": {
                "orders_analyzed": 2100,
                "orders_packed": 1950,
                "sessions": 145,
                "labels_printed": 12
            }
        },
        "analysis_history": [...],          # Shopify Tool records
        "packing_history": [...],           # Packing Tool records
        "label_print_history": [...],       # Shopify Tool label prints
        "last_updated": "2025-11-05T14:30:00+02:00",
        "version": "2.0"
    }

    Attributes:
        store (Store): server store holding Stats/global_stats.json
        max_retries (int): Maximum number of retry attempts for file operations
        retry_delay (float): Delay in seconds between retries
    """

    def __init__(
        self,
        store: Store,
        max_retries: int = 5,
        retry_delay: float = 0.1
    ):
        """
        Initialize the StatsManager.

        Args:
            store: The server store (FileStore on the 0UFulfilment directory)
            max_retries: Maximum number of retry attempts for store errors
            retry_delay: Delay in seconds between retry attempts
        """
        self.store = store
        self.max_retries = max_retries
        self.retry_delay = retry_delay

    def _get_default_stats(self) -> dict[str, Any]:
        """Get default statistics structure."""
        return _default_stats()

    def _with_defaults(self, stats: Any) -> dict[str, Any]:
        """Stats as read, with missing keys filled in; defaults if absent or not a dict."""
        return _stats_with_defaults(stats)

    def _with_retries(self, what: str, op):
        """Run op(); a store error is retried, then raised as StatsManagerError."""
        for attempt in range(self.max_retries):
            try:
                return op()
            except (StoreError, OSError) as e:
                if attempt == self.max_retries - 1:
                    raise StatsManagerError(f"Failed to {what} after {self.max_retries} attempts: {e}") from e
                time.sleep(self.retry_delay * (attempt + 1))

    @staticmethod
    def _unreadable(e: StoreError) -> bool:
        """Invalid JSON (a ValueError under StoreError) counts as no stats, as it always has.

        A cut-off response (StoreUnreachable) is not: resetting on it would wipe the stats.
        """
        return isinstance(e.__cause__, ValueError) and not isinstance(e, StoreUnreachable)

    def _load_stats(self) -> dict[str, Any]:
        """Load statistics; absent or unreadable JSON gives defaults."""
        def read():
            try:
                return self.store.read_json(STATS_REL)
            except StoreNotFound:
                return None
            except StoreError as e:
                if self._unreadable(e):
                    return None
                raise

        return self._with_defaults(self._with_retries("load stats", read))

    def _atomic_update(self, update_func) -> None:
        """Read-modify-write of the stats under the Store's lock (Store.update_json)."""
        def fn(current):
            stats = self._with_defaults(current)
            update_func(stats)
            stats["last_updated"] = get_current_timestamp()
            return stats

        self._update_doc(fn)

    def _update_doc(self, fn) -> None:
        """Run fn(current) -> new stats under the Store's lock, with retries and unreadable-JSON recovery."""
        def update():
            try:
                self.store.update_json(STATS_REL, fn)
            except StoreError as e:
                if not self._unreadable(e):
                    raise
                # Unreadable JSON: start over from defaults, as the old code did.
                self.store.write_json(STATS_REL, self._get_default_stats())
                self.store.update_json(STATS_REL, fn)

        self._with_retries("update stats", update)

    def record_analysis(
        self,
        client_id: str,
        session_id: str,
        orders_count: int,
        metadata: dict[str, Any] | None = None
    ) -> None:
        """Record an analysis completion from Shopify Tool.

        Args:
            client_id: Client identifier (e.g., "M", "A", "B")
            session_id: Session identifier — derive with
                shared.session_id.derive_session_id(session_path)
            orders_count: Number of orders analyzed
            metadata: Optional additional metadata (e.g., fulfillable_orders,
                courier_breakdown)
        """
        def update(stats):
            stats["total_orders_analyzed"] += orders_count

            if client_id not in stats["by_client"]:
                stats["by_client"][client_id] = {
                    "orders_analyzed": 0,
                    "orders_packed": 0,
                    "sessions": 0,
                }

            stats["by_client"][client_id]["orders_analyzed"] += orders_count

            record = {
                "timestamp": get_current_timestamp(),
                "client_id": client_id,
                "session_id": session_id,
                "orders_count": orders_count,
            }

            if metadata:
                record["metadata"] = metadata

            stats["analysis_history"].append(record)

            if len(stats["analysis_history"]) > 1000:
                stats["analysis_history"] = stats["analysis_history"][-1000:]

        self._atomic_update(update)

    def record_packing(
        self,
        client_id: str,
        session_id: str,
        worker_id: str | None,
        orders_count: int,
        items_count: int,
        metadata: dict[str, Any] | None = None
    ) -> None:
        """Record a packing session completion from Packing Tool.

        Args:
            client_id: Client identifier (e.g., "M", "A", "B")
            session_id: Session identifier — derive with
                shared.session_id.derive_session_id(session_path)
            worker_id: Worker identifier (e.g., "001", "002")
            orders_count: Number of orders packed
            items_count: Number of items packed
            metadata: Optional additional metadata (e.g., duration, start_time, end_time)
        """
        args = {
            "client_id": client_id,
            "session_id": session_id,
            "worker_id": worker_id,
            "orders_count": orders_count,
            "items_count": items_count,
            "metadata": metadata,
            "timestamp": get_current_timestamp(),
        }
        self._update_doc(Delta("stats.record_packing", args))

    def get_global_stats(self) -> dict[str, Any]:
        """Get global statistics summary."""
        stats = self._load_stats()
        return {
            "total_orders_analyzed": stats.get("total_orders_analyzed", 0),
            "total_orders_packed": stats.get("total_orders_packed", 0),
            "total_sessions": stats.get("total_sessions", 0),
            "last_updated": stats.get("last_updated"),
        }

    def get_client_stats(self, client_id: str) -> dict[str, Any]:
        """Get statistics for a specific client."""
        stats = self._load_stats()
        if client_id not in stats.get("by_client", {}):
            return {"orders_analyzed": 0, "orders_packed": 0, "sessions": 0}
        return stats["by_client"][client_id].copy()

    def get_all_clients_stats(self) -> dict[str, dict[str, Any]]:
        """Get statistics for all clients."""
        stats = self._load_stats()
        return stats.get("by_client", {}).copy()

    def get_analysis_history(
        self,
        client_id: str | None = None,
        limit: int | None = None
    ) -> list[dict[str, Any]]:
        """Get analysis history with optional filtering (newest first)."""
        stats = self._load_stats()
        history = stats.get("analysis_history", [])

        if client_id:
            history = [h for h in history if h.get("client_id") == client_id]

        history.sort(key=lambda h: h.get("timestamp", ""), reverse=True)

        if limit:
            history = history[:limit]

        return history

    def get_packing_history(
        self,
        client_id: str | None = None,
        worker_id: str | None = None,
        limit: int | None = None
    ) -> list[dict[str, Any]]:
        """Get packing history with optional filtering (newest first)."""
        stats = self._load_stats()
        history = stats.get("packing_history", [])

        if client_id:
            history = [h for h in history if h.get("client_id") == client_id]

        if worker_id:
            history = [h for h in history if h.get("worker_id") == worker_id]

        history.sort(key=lambda h: h.get("timestamp", ""), reverse=True)

        if limit:
            history = history[:limit]

        return history

    def record_label_print(
        self,
        client_id: str,
        sku: str,
        copies: int,
    ) -> None:
        """Record a label print event from the SKU Label widget."""
        def update(stats):
            stats["total_labels_printed"] += copies

            if client_id not in stats["by_client"]:
                stats["by_client"][client_id] = {
                    "orders_analyzed": 0,
                    "orders_packed": 0,
                    "sessions": 0,
                    "labels_printed": 0,
                }
            client = stats["by_client"][client_id]
            if "labels_printed" not in client:
                client["labels_printed"] = 0
            client["labels_printed"] += copies

            record = {
                "timestamp": get_current_timestamp(),
                "client_id": client_id,
                "sku": sku,
                "copies": copies,
            }
            stats["label_print_history"].append(record)

            if len(stats["label_print_history"]) > 1000:
                stats["label_print_history"] = stats["label_print_history"][-1000:]

        self._atomic_update(update)

    def get_label_print_history(
        self,
        client_id: str | None = None,
        start_date: datetime | None = None,
        end_date: datetime | None = None,
        limit: int | None = None,
    ) -> list[dict[str, Any]]:
        """Get label print history with optional filtering.

        start_date/end_date may be naive or timezone-aware (the GUI builds
        them from a QDate, which has no timezone concept). Naive values are
        assumed to be in the local timezone — the same convention
        get_current_timestamp() uses when writing records — so comparing
        them against the stored (timezone-aware) timestamps never raises
        "can't compare offset-naive and offset-aware datetimes".
        """
        stats = self._load_stats()
        history = stats.get("label_print_history", [])

        if client_id:
            history = [h for h in history if h.get("client_id") == client_id]

        if start_date:
            if start_date.tzinfo is None:
                start_date = start_date.astimezone()
            filtered = []
            for h in history:
                ts = parse_timestamp(h["timestamp"])
                if ts is not None and ts >= start_date:
                    filtered.append(h)
            history = filtered

        if end_date:
            end_dt = end_date.replace(hour=23, minute=59, second=59, microsecond=999999)
            if end_dt.tzinfo is None:
                end_dt = end_dt.astimezone()
            filtered = []
            for h in history:
                ts = parse_timestamp(h["timestamp"])
                if ts is not None and ts <= end_dt:
                    filtered.append(h)
            history = filtered

        history.sort(key=lambda h: h.get("timestamp", ""), reverse=True)

        if limit:
            history = history[:limit]

        return history

    def get_label_stats(
        self,
        client_id: str | None = None,
    ) -> dict[str, Any]:
        """Get label printing summary statistics."""
        history = self.get_label_print_history(client_id=client_id)

        sku_counts: dict[str, int] = {}
        for record in history:
            sku = record.get("sku", "Unknown")
            sku_counts[sku] = sku_counts.get(sku, 0) + record.get("copies", 1)

        total = sum(sku_counts.values())
        top_sku = max(sku_counts, key=sku_counts.get) if sku_counts else None

        return {
            "total_labels_printed": total,
            "unique_skus": len(sku_counts),
            "top_sku": top_sku,
            "sku_breakdown": sku_counts,
        }

    def reset_stats(self) -> None:
        """Reset all statistics to default values.

        WARNING: This will delete all historical data. Use with caution.
        """
        def reset(stats):
            stats.clear()
            stats.update(self._get_default_stats())

        self._atomic_update(reset)


if __name__ == "__main__":
    import tempfile as _tempfile

    with _tempfile.TemporaryDirectory() as tmp:
        manager = StatsManager(FileStore(tmp))

        manager.record_analysis(
            client_id="M",
            session_id="2025-11-05_1",
            orders_count=150,
            metadata={"fulfillable_orders": 142, "courier_breakdown": {"DHL": 80, "DPD": 62}},
        )
        manager.record_packing(
            client_id="M",
            session_id="2025-11-05_1",
            worker_id="001",
            orders_count=142,
            items_count=450,
            metadata={"duration_seconds": 9000},
        )
        manager.record_label_print(client_id="M", sku="SKU-1", copies=3)

        stats = manager._load_stats()
        assert stats["total_orders_analyzed"] == 150
        assert stats["total_orders_packed"] == 142
        assert stats["total_labels_printed"] == 3
        assert stats["by_client"]["M"]["sessions"] == 1
        assert stats["version"] == "2.0"

        global_stats = manager.get_global_stats()
        assert global_stats["total_orders_analyzed"] == 150
        assert global_stats["total_orders_packed"] == 142

        print("stats_manager self-check OK")
