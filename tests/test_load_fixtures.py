from scripts.load_fixtures_to_db import load, wanted


def test_wanted_skips_locks_logs_probes_and_derived_indexes():
    assert wanted("Clients/CLIENT_ALMA/shopify_config.json")
    assert wanted("Sessions/CLIENT_ALMA/2026-11-15_1/analysis/current_state.pkl")
    assert not wanted("Sessions/CLIENT_ALMA/2026-11-15_1/session_info.json.lock")
    assert not wanted("Sessions/CLIENT_ALMA/session_index.json")
    assert not wanted("Logs/ShopifyTool-DB/x.log")
    assert not wanted(".connection_test")


def test_load_writes_every_wanted_file_through_the_store(tmp_path):
    (tmp_path / "Workers").mkdir()
    (tmp_path / "Workers" / "workers.json").write_text("{}")
    (tmp_path / "Workers" / "workers.json.lock").write_text("")

    class Recorder:
        def __init__(self):
            self.written = {}

        def write_bytes(self, rel, data):
            self.written[rel] = data

    store = Recorder()
    assert load(store, tmp_path) == 1
    assert store.written == {"Workers/workers.json": b"{}"}
