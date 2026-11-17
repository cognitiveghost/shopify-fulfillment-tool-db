"""A cut-off response (StoreUnreachable from a JSONDecodeError) is a retry, never a reset."""
import json

import pytest

from shared.stats_manager import STATS_REL, StatsManager, StatsManagerError
from shared.store import FileStore, StoreUnreachable
from shopify_tool.groups_manager import GROUPS, GroupsManager, GroupsManagerError


class CutOffStore(FileStore):
    """Every read-modify-write fails as a truncated response would, after the guard classified it."""

    def update_json(self, rel, fn):
        try:
            json.loads('{"total_sess')
        except json.JSONDecodeError as e:
            raise StoreUnreachable(f"update_json: {e}") from e


def test_cut_off_response_keeps_the_stats(tmp_path):
    store = CutOffStore(tmp_path)
    FileStore(tmp_path).write_json(STATS_REL, {"total_sessions": 41})

    with pytest.raises(StatsManagerError):
        StatsManager(store, max_retries=1).record_analysis("M", "s1", 3)

    assert FileStore(tmp_path).read_json(STATS_REL)["total_sessions"] == 41


def test_cut_off_response_keeps_the_groups(tmp_path):
    FileStore(tmp_path).write_json(GROUPS, {"version": "1.0", "groups": [{"id": "g", "name": "Wholesale"}]})

    with pytest.raises(GroupsManagerError):
        GroupsManager(CutOffStore(tmp_path)).create_group("Retail")

    assert FileStore(tmp_path).read_json(GROUPS)["groups"][0]["name"] == "Wholesale"
