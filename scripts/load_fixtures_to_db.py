"""Load replay fixtures into a Supabase project, for a trial run of both apps.

    .venv/bin/python scripts/load_fixtures_to_db.py                              # guide server, default mode
    .venv/bin/python scripts/load_fixtures_to_db.py --fixtures tests/replay/fixtures [--client ALMA]

Two modes, both copying every file into the database through SupabaseStore, so the rows are what the apps
themselves write:

- Default: seeds a throwaway server folder with build_guide_server.py (anonymised fixtures only: ALMA and HERBAR,
  sessions with packing lists, packer settings, workers). Not idempotent: each run adds that day's sessions
  (`<date>_N`). Writes no sessions for the trial itself.
- `--fixtures PATH`: the 26 replay sessions of ALMA, HERBAR and LIDA (or only `--client`), as sessions with
  orders, stock and memory baseline but no analysis, so the trial runs analysis itself. Idempotent: the same
  session names and bytes on every run. Writes no workers or packer_config.json. PATH must be
  tests/replay/fixtures or a client folder below it; anything else is refused before any network call.

A full trial dataset is both runs. The local reset (supabase db reset, then both loads) is in
docs/trial-checklist.md.

Target: FULFILLMENT_DB_URL / FULFILLMENT_DB_KEY (shared/db_config.py). With SUPABASE_SERVICE_KEY set it uses
that key (local stack); otherwise it asks for an email and password and signs in.
"""

import argparse
import getpass
import json
import os
import socket
import sys
import tempfile
from pathlib import Path

import pandas as pd

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from scripts.build_guide_server import build
from shared import db_config
from shared.store_supabase import SupabaseStore
from shopify_tool import core
from shopify_tool.profile_manager import ProfileManager
from supabase import create_client

FIXTURES = REPO / "tests" / "replay" / "fixtures"
_DERIVED = {"session_index.json", "registry_index.json"}


def wanted(rel: str) -> bool:
    """Data files only: no lock files, logs, connection probes or indexes the database derives."""
    name = rel.rsplit("/", 1)[-1]
    return not (rel.startswith(("Logs/", ".")) or name.endswith(".lock") or name in _DERIVED)


def load(store, root: Path) -> int:
    files = sorted(p.relative_to(root).as_posix() for p in root.rglob("*") if p.is_file())
    files = [rel for rel in files if wanted(rel)]
    for rel in files:
        store.write_bytes(rel, (root / rel).read_bytes())
    return len(files)


def stock_csv_bytes(df: pd.DataFrame, config: dict) -> bytes:
    """A stock frame as the client's own CSV: internal columns renamed back to the client's headers."""
    inverse = {internal: header for header, internal in config["column_mappings"]["stock"].items()}
    return df[["SKU", "Product_Name", "Stock"]].rename(columns=inverse).to_csv(index=False).encode("utf-8")


def _read_json(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


def _write_json(path: Path, doc: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(doc, ensure_ascii=False, indent=2), encoding="utf-8")


def _client_dirs(fixtures: Path, client: str | None) -> list[Path]:
    if any(fixtures.glob("*/meta.json")):
        dirs = [fixtures]
    else:
        dirs = sorted(p for p in fixtures.iterdir() if p.is_dir() and any(p.glob("*/meta.json")))
    if client is not None:
        dirs = [d for d in dirs if d.name == client]
        if not dirs:
            raise ValueError(f"no fixture client {client!r} under {fixtures}")
    return dirs


def _latest_config(client_dir: Path) -> dict:
    latest = max(client_dir.glob("*/meta.json"))
    return _read_json(client_dir / _read_json(latest)["config"])


def _write_session(root: Path, client_id: str, folder: Path, config: dict) -> None:
    meta = _read_json(folder / "meta.json")
    baseline = _read_json(folder / "memory_baseline.json")
    session = root / "Sessions" / f"CLIENT_{client_id}" / folder.name
    _write_json(
        session / "session_info.json",
        {
            "created_by_tool": "shopify",
            "created_at": meta["created_at"],
            "client_id": client_id,
            "session_name": folder.name,
            "status": "active",
            "pc_name": "fixture-loader",
            "orders_file": "orders_export.csv",
            "stock_file": "inventory.csv",
            "analysis_completed": False,
            "packing_lists_generated": [],
            "stock_exports_generated": [],
            "statistics": {"total_orders": 0, "total_items": 0, "packing_lists_count": 0, "packing_lists": []},
            "comments": "",
            "last_modified": meta["created_at"],
        },
    )
    (session / "input").mkdir(parents=True, exist_ok=True)
    (session / "input" / "orders_export.csv").write_bytes((folder / "orders.csv").read_bytes())
    (session / "input" / "inventory.csv").write_bytes(
        stock_csv_bytes(core.baseline_stock_df(baseline), config)
    )
    (session / "analysis").mkdir(parents=True, exist_ok=True)
    (session / "analysis" / "memory_baseline.json").write_bytes((folder / "memory_baseline.json").read_bytes())


def build_fixture_server(root: Path, fixtures: Path = FIXTURES, client: str | None = None) -> int:
    """Write the fixture sessions into an empty `root` in the server's layout; return how many sessions."""
    root = Path(root)
    pm = ProfileManager(base_path=str(root))
    sessions = 0
    for client_dir in _client_dirs(Path(fixtures), client):
        client_id = client_dir.name
        config = _latest_config(client_dir)
        config.setdefault("inventory_memory", {})["enabled"] = True
        pm.create_client_profile(client_id, client_id)
        pm.save_shopify_config(client_id, config, backup=False)
        for meta in sorted(client_dir.glob("*/meta.json")):
            _write_session(root, client_id, meta.parent, config)
            sessions += 1
    return sessions


def _is_fixture_path(arg: str) -> bool:
    path, root = Path(arg).resolve(), FIXTURES.resolve()
    return path == root or (path.is_relative_to(root) and any(path.glob("*/meta.json")))


def _client():
    service_key = os.environ.get("SUPABASE_SERVICE_KEY")
    if service_key:
        return create_client(db_config.SUPABASE_URL, service_key)
    client = create_client(db_config.SUPABASE_URL, db_config.SUPABASE_PUBLISHABLE_KEY)
    client.auth.sign_in_with_password({"email": input("Email: "), "password": getpass.getpass()})
    return client


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Load replay fixtures into Supabase.")
    parser.add_argument("--fixtures", metavar="PATH", help="load the replay sessions from tests/replay/fixtures")
    parser.add_argument("--client", metavar="ID", help="with --fixtures: only this client folder")
    args = parser.parse_args(argv)
    if args.client and not args.fixtures:
        parser.error("--client needs --fixtures")
    if args.fixtures and not _is_fixture_path(args.fixtures):
        print(f"refusing: {args.fixtures} is not under tests/replay/fixtures", file=sys.stderr)
        sys.exit(2)

    print(f"target {db_config.SUPABASE_URL}")
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp) / "server"
        if args.fixtures:
            build_fixture_server(root, Path(args.fixtures).resolve(), args.client)
        else:
            build(root)
        store = SupabaseStore(_client(), pc=f"loader-{socket.gethostname()}", cache_dir=Path(tmp) / "cache")
        print(f"loaded {load(store, root)} files")


if __name__ == "__main__":
    main()
