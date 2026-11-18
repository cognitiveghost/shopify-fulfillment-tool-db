from shared.stats_manager import STATS_REL, StatsManager
from shared.store import Delta, FileStore

ARGS = {
    "client_id": "M",
    "session_id": "s1",
    "worker_id": "001",
    "orders_count": 3,
    "items_count": 5,
    "metadata": None,
    "timestamp": "2026-11-17T10:00:00+02:00",
}


class SpyStore(FileStore):
    def __init__(self, root):
        super().__init__(root)
        self.updates = []

    def update_json(self, rel, fn):
        self.updates.append((rel, fn))
        return super().update_json(rel, fn)


def test_record_packing_uses_a_delta(tmp_path):
    store = SpyStore(tmp_path)

    StatsManager(store).record_packing("M", "s1", "001", 3, 5)

    [(rel, fn)] = store.updates
    assert rel == STATS_REL
    assert isinstance(fn, Delta)
    assert fn.kind == "stats.record_packing"
    assert fn.args["timestamp"]


def test_replayed_record_packing_counts_once(tmp_path):
    store = FileStore(tmp_path)
    replay = Delta("stats.record_packing", ARGS, op_id="x")

    store.update_json(STATS_REL, replay)
    store.update_json(STATS_REL, replay)

    doc = store.read_json(STATS_REL)
    assert doc["total_orders_packed"] == 3
    assert doc["total_sessions"] == 1
    assert len(doc["packing_history"]) == 1
    assert doc["packing_history"][0]["timestamp"] == ARGS["timestamp"]


def test_record_packing_online_matches_before(tmp_path):
    store = FileStore(tmp_path)

    StatsManager(store).record_packing("M", "s1", "001", 3, 5)

    doc = store.read_json(STATS_REL)
    assert doc["total_orders_packed"] == 3
    assert doc["total_orders_analyzed"] == 0
    assert doc["total_sessions"] == 1
    assert doc["total_labels_printed"] == 0
    assert doc["by_client"] == {"M": {"orders_analyzed": 0, "orders_packed": 3, "sessions": 1}}
    assert doc["version"] == "2.0"
    [record] = doc["packing_history"]
    record.pop("timestamp")
    assert record == {
        "client_id": "M",
        "session_id": "s1",
        "worker_id": "001",
        "orders_count": 3,
        "items_count": 5,
    }
