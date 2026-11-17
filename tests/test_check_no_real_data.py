import pandas as pd
import pytest

from scripts.check_no_real_data import accepted, offenders, person_values
from scripts.load_fixtures_to_db import build_fixture_server, load
from scripts.seed_dev_store import FAKE_NAMES
from shared.store_supabase import SupabaseStore

SESSION = "Sessions/CLIENT_ALMA/2026-09-30_6"


def test_accepted_rule():
    words = ["VIP"]

    assert accepted("", "Customer", words)
    assert accepted("nan", "Customer", words)
    assert accepted("Customer", "Customer", words)
    assert accepted("VIP", "Note", words)
    assert accepted(FAKE_NAMES[0], "Customer", [])
    assert not accepted("Ivan Petrov", "Customer", words)
    assert not accepted("call Ivan VIP", "Note", words)


def test_walker_matches_keys_case_insensitively():
    doc = {"orders": [{"Customer": "a", "notes": "b", "system_note": "c", "x": {"NOTES": "d"}}]}

    assert list(person_values(doc)) == [("Customer", "a"), ("notes", "b"), ("NOTES", "d")]


@pytest.mark.supabase
def test_check_passes_after_fixture_load(supabase_client, tmp_path):
    root = tmp_path / "s"
    build_fixture_server(root)
    load(SupabaseStore(supabase_client, pc="t", cache_dir=tmp_path / "c"), root)

    assert offenders(supabase_client) == []


@pytest.mark.supabase
def test_check_fails_on_a_real_name(supabase_client, tmp_path):
    root = tmp_path / "s"
    build_fixture_server(root)
    store = SupabaseStore(supabase_client, pc="t", cache_dir=tmp_path / "c")
    load(store, root)
    store.write_json(f"{SESSION}/packing_lists/X.json",
                     {"orders": [{"order_number": "1", "Customer": "Ivan Petrov"}]})

    found = offenders(supabase_client)

    assert len(found) == 1
    assert "Ivan Petrov" in found[0]


@pytest.mark.supabase
def test_check_reads_order_csvs(supabase_client, tmp_path):
    root = tmp_path / "s"
    build_fixture_server(root)
    store = SupabaseStore(supabase_client, pc="t", cache_dir=tmp_path / "c")
    load(store, root)
    rel = f"{SESSION}/input/orders_export.csv"
    csv_path = tmp_path / "orders.csv"
    csv_path.write_bytes(store.read_bytes(rel))
    df = pd.read_csv(csv_path, dtype=str, keep_default_na=False, encoding="utf-8-sig")
    df.loc[0, "Shipping Name"] = "Ivan Petrov"
    df.to_csv(csv_path, index=False)
    store.write_bytes(rel, csv_path.read_bytes())

    found = offenders(supabase_client)

    assert len(found) == 1
    assert "Ivan Petrov" in found[0]
