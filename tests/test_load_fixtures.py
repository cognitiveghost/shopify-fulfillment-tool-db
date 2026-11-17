import json
import os

import pandas as pd
import pytest

from scripts import load_fixtures_to_db
from scripts.load_fixtures_to_db import (
    FIXTURES,
    build_fixture_server,
    load,
    main,
    stock_csv_bytes,
    wanted,
)
from shared.store import FileStore
from shared.store_supabase import SupabaseStore
from shopify_tool import core

CLIENTS = ("ALMA", "HERBAR", "LIDA")


def _read_json(path):
    return json.loads(path.read_text(encoding="utf-8"))


def _fixture_config(client_id):
    return _read_json(next((FIXTURES / client_id / "config").glob("*.json")))


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


def test_fixture_server_has_26_sessions_in_3_clients(tmp_path):
    n = build_fixture_server(tmp_path / "s")

    store = FileStore(tmp_path / "s")
    counts = {c: store.query_sessions(c) for c in CLIENTS}
    assert n == 26
    assert sum(len(v) for v in counts.values()) == 26
    for client_id, sessions in counts.items():
        assert sessions, client_id
        fixture_names = {p.parent.name for p in (FIXTURES / client_id).glob("*/meta.json")}
        for info in sessions:
            assert info["session_name"] in fixture_names
            assert info["pc_name"] == "fixture-loader"
            rel = f"Sessions/CLIENT_{client_id}/{info['session_name']}"
            for name in ("session_info.json", "input/orders_export.csv", "input/inventory.csv",
                         "analysis/memory_baseline.json"):
                assert store.exists(f"{rel}/{name}"), f"{rel}/{name}"


def test_fixture_server_is_deterministic(tmp_path):
    build_fixture_server(tmp_path / "a")
    build_fixture_server(tmp_path / "b")

    def sessions_snapshot(root):
        base = root / "Sessions"
        return {p.relative_to(root).as_posix(): p.read_bytes() for p in base.rglob("*") if p.is_file()}

    first, second = sessions_snapshot(tmp_path / "a"), sessions_snapshot(tmp_path / "b")
    assert first and first == second


def test_fixture_server_one_client(tmp_path):
    build_fixture_server(tmp_path / "s", client="HERBAR")

    assert os.listdir(tmp_path / "s" / "Sessions") == ["CLIENT_HERBAR"]
    assert len(FileStore(tmp_path / "s").query_sessions("HERBAR")) == 6


def test_stock_csv_uses_client_headers(tmp_path):
    baseline = _read_json(next((FIXTURES / "ALMA").glob("*/memory_baseline.json")))
    config = _fixture_config("ALMA")
    df = core.baseline_stock_df(baseline)

    data = stock_csv_bytes(df, config)

    assert data.decode("utf-8").splitlines()[0] == "Артикул,Име,Наличност"
    path = tmp_path / "inventory.csv"
    path.write_bytes(data)
    sep = core.resolve_delimiter(str(path), "auto", "stock")
    back = pd.read_csv(path, delimiter=sep, encoding="utf-8-sig", dtype={"Артикул": str}).rename(
        columns=config["column_mappings"]["stock"]
    )
    assert dict(zip(back["SKU"], back["Stock"])) == dict(zip(df["SKU"].astype(str), df["Stock"]))


def test_fixtures_refuses_outside_path(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(load_fixtures_to_db, "_client", lambda: pytest.fail("reached the network"))

    with pytest.raises(SystemExit) as exc:
        main(["--fixtures", str(tmp_path)])

    assert exc.value.code == 2
    assert "refusing" in capsys.readouterr().err


def test_fixtures_refuses_escaping_path(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(load_fixtures_to_db, "_client", lambda: pytest.fail("reached the network"))
    link = tmp_path / "link"
    link.symlink_to(FIXTURES.parent, target_is_directory=True)

    for arg in (str(FIXTURES / ".." / ".."), str(link)):
        with pytest.raises(SystemExit) as exc:
            main(["--fixtures", arg])
        assert exc.value.code == 2
        assert "refusing" in capsys.readouterr().err


@pytest.mark.supabase
def test_fixture_load_into_supabase_is_idempotent(supabase_client, tmp_path):
    root = tmp_path / "s"
    build_fixture_server(root)
    store = SupabaseStore(supabase_client, pc="t", cache_dir=tmp_path / "c")

    for _ in range(2):
        load(store, root)
        assert sum(len(store.query_sessions(c)) for c in CLIENTS) == 26
