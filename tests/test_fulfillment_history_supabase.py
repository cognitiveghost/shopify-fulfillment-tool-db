"""History pruning sees the same sessions on both backends (spec §9, §10.8)."""

import pandas as pd
import pytest

from shared.store import FileStore
from shared.store_supabase import SupabaseStore
from shopify_tool import fulfillment_history as fh

pytestmark = pytest.mark.supabase

REL = f"Clients/CLIENT_M/{fh.FILE_NAME}"
SHIPS = pd.DataFrame({"Order_Number": ["#9"], "SKU": ["A"], "Order_Fulfillment_Status": ["Fulfillable"]})
# a: live, b: archived, c: history row but no folder, "": legacy row
ROWS = [("#1", "2026-01-01", "a"), ("#2", "2026-01-01", "b"), ("#3", "2026-01-01", "c"), ("#4", "2026-01-01", "")]


def _run(store):
    store.write_bytes(REL, pd.DataFrame(ROWS, columns=fh.COLUMNS).to_csv(index=False).encode("utf-8"))
    existing = fh.session_folders(store, "Sessions/CLIENT_M", "a")
    assert fh.record_session(store, REL, "a", SHIPS, today="2026-01-02", existing_sessions=existing)
    return set(fh.load(store, REL)["Session"])


def test_pruning_matches_between_backends(supabase_client, supabase_admin, tmp_path):
    # FileStore: an archived session's folder moves out of Sessions/CLIENT_M.
    file_store = FileStore(tmp_path / "files")
    file_store.write_json("Sessions/CLIENT_M/a/session_info.json", {})
    file_store.write_json("Sessions/CLIENT_M/b/session_info.json", {})
    file_store.move("Sessions/CLIENT_M/b", "Sessions/_archive/CLIENT_M/b")

    # Supabase: the same session carries archived = true, which the listing skips.
    sb = SupabaseStore(supabase_client, pc="pc1", cache_dir=tmp_path / "cache")
    sb.client.table("clients").insert({"client_id": "M"}).execute()
    sb.write_json("Sessions/CLIENT_M/a/session_info.json", {})
    sb.write_json("Sessions/CLIENT_M/b/session_info.json", {})
    supabase_admin.table("sessions").update({"archived": True}).match({"client_id": "M", "name": "b"}).execute()

    kept_files = _run(file_store)
    kept_supabase = _run(sb)
    assert kept_files == kept_supabase == {"a", ""}
