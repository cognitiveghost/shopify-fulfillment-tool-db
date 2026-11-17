"""scripts/seed_fallback_share.py: refusals, reset safety and the seeded layout (spec A5)."""

import pytest

from scripts.seed_fallback_share import MARKER, refusal, seed
from shared.store import FileStore


@pytest.fixture(scope="module")
def seeded(tmp_path_factory):
    dest = tmp_path_factory.mktemp("share") / "share"
    seed(str(dest))
    return dest


def test_refuses_production_unc():
    assert refusal(r"\\192.168.88.101\_Fulfilment_\0UFulfilment", False) is not None
    assert refusal("//192.168.88.101/x", False) is not None
    assert refusal(r"\\192.168.88.101\SHARE", False) is not None


def test_refuses_server_dot(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    assert refusal(str(tmp_path / "Desktop" / "Server." / "x"), False) is not None


def test_refuses_non_empty_without_reset(tmp_path):
    (tmp_path / "junk.txt").write_text("x")
    assert refusal(str(tmp_path), False) is not None


def test_reset_without_marker_refuses_and_keeps_files(tmp_path):
    keep = tmp_path / "keep.txt"
    keep.write_text("mine")
    with pytest.raises(ValueError):
        seed(str(tmp_path), reset=True)
    assert keep.read_text() == "mine"


def test_seed_layout_reads_through_filestore(seeded):
    store = FileStore(seeded)
    assert len(store.query_sessions("ALMA")) == 3
    assert len(store.query_sessions("HERBAR")) == 2

    sessions = [p for p in seeded.glob("Sessions/CLIENT_*/*") if p.is_dir()]
    assert len(sessions) == 5
    for session in sessions:
        packing_lists = list((session / "packing_lists").glob("*.json"))
        assert len(packing_lists) == 1
        store.read_json(packing_lists[0].relative_to(seeded).as_posix())

    assert (seeded / MARKER).exists()


def test_reset_with_marker_reseeds(tmp_path):
    dest = tmp_path / "share"
    seed(str(dest))
    (dest / "junk.txt").write_text("x")

    seed(str(dest), reset=True)

    assert not (dest / "junk.txt").exists()
    assert (dest / MARKER).exists()
