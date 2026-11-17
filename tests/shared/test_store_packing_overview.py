"""FileStore-only cases of Store.packing_overview (the rest is in test_store_contract.py)."""

import pytest

from shared.store import FileStore, StoreError

CLIENT = "Sessions/CLIENT_X"


def test_legacy_barcodes_row(tmp_path):
    store = FileStore(tmp_path)
    store.write_json(f"{CLIENT}/s1/barcodes/packing_state.json", {"packing_list_name": "Old"})
    (row,) = store.packing_overview("X")
    assert row["list_name"] == "Old"
    assert row["work_rel"] == f"{CLIENT}/s1/barcodes"
    assert row["packing_list"] is None
    assert row["started"] is True


def test_total_orders_from_orders_length(tmp_path):
    store = FileStore(tmp_path)
    store.write_json(f"{CLIENT}/s1/packing_lists/L.json", {"orders": [1, 2, 3]})
    (row,) = store.packing_overview("X")
    assert row["packing_list"]["total_orders"] == 3


def test_corrupt_document_raises(tmp_path):
    store = FileStore(tmp_path)
    store.write_json(f"{CLIENT}/s1/packing_lists/L.json", {"orders": []})
    store.write_bytes(f"{CLIENT}/s1/packing/L/packing_state.json", b"{")
    with pytest.raises(StoreError):
        store.packing_overview("X")


def test_failed_listing_raises(tmp_path, monkeypatch):
    store = FileStore(tmp_path)
    store.write_json(f"{CLIENT}/s1/packing_lists/L.json", {"orders": []})

    def denied(self, rel):
        raise StoreError(rel)

    monkeypatch.setattr(FileStore, "list_dir", denied)
    with pytest.raises(StoreError):
        store.packing_overview("X")
