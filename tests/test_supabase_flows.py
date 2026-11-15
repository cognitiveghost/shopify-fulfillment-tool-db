"""Fulfilment's store flows on a real SupabaseStore: client, session claim, analysis, withdraw, archive."""
import json
import sys
import threading
from pathlib import Path

import pytest
from conftest import make_user_client

sys.path.insert(0, str(Path(__file__).parent / "replay"))
from replay import first_difference, replay_session

from shared.store_supabase import SupabaseStore
from shopify_tool.profile_manager import ProfileManager
from shopify_tool.session_manager import SessionManager

pytestmark = pytest.mark.supabase

REPLAY = Path(__file__).parent / "replay" / "fixtures" / "ALMA" / "2026-09-30_6"


@pytest.fixture
def env(supabase_client, tmp_path):
    store = SupabaseStore(supabase_client, pc="pc1", cache_dir=tmp_path / "cache")
    pm = ProfileManager(store=store)
    assert pm.create_client_profile("ZED", "Zed Ltd")
    return store, pm, SessionManager(pm, store)


def test_two_concurrent_claims_of_one_session_name_one_wins(env, supabase_user, tmp_path):
    store = env[0]
    rel = "Sessions/CLIENT_ZED/2026-11-14_1"
    store.write_json(f"{rel}/session_info.json", {"status": "active"})  # ensures the sessions row
    wins = []

    def go(i):
        s = SupabaseStore(make_user_client(*supabase_user), pc=f"pc{i}", cache_dir=tmp_path / f"c{i}")
        wins.append(s.claim_dir("Sessions/CLIENT_ZED/2026-11-14_2"))

    ts = [threading.Thread(target=go, args=(i,)) for i in range(2)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    assert sorted(wins) == [False, True]


def test_new_session_has_its_layout_folders_before_any_file_is_written(env):
    # Supabase folders are implicit: input/ is empty until the orders file is copied into it
    _store, _pm, sm = env
    session_path = sm.create_session("ZED")
    assert sm.get_input_dir(session_path).name == "input"
    assert sm.get_analysis_dir(session_path).name == "analysis"


def test_create_session_analysis_roundtrip_withdraw_archive(env, tmp_path):
    store, pm, sm = env
    session_path = sm.create_session("ZED")
    name = Path(session_path).name
    assert [s["session_name"] for s in sm.list_client_sessions("ZED")] == [name]

    analysis, _ = replay_session(REPLAY, tmp_path / "work")
    rel = f"Sessions/CLIENT_ZED/{name}"
    store.write_json(f"{rel}/analysis/analysis_data.json", analysis)
    expected = json.loads((REPLAY / "expected" / "analysis_data.json").read_text(encoding="utf-8"))
    assert first_difference(expected, store.read_json(f"{rel}/analysis/analysis_data.json")) is None

    store.write_json(f"{rel}/packing_lists/L1.json", {"items": []})
    store.move(f"{rel}/packing_lists/L1.json", f"{rel}/packing_lists/withdrawn/L1.json")
    assert store.list_dir(f"{rel}/packing_lists") == ["withdrawn"]
    assert store.list_dir(f"{rel}/packing_lists/withdrawn") == ["L1.json"]

    assert "ZED" in pm.list_clients()
    pm.archive_client("ZED")
    assert "ZED" not in pm.list_clients()
    assert sm.list_client_sessions("ZED") == []
