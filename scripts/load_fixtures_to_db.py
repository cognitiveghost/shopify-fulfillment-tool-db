"""Load anonymised replay fixtures into a Supabase project, for a trial run of both apps.

    .venv/bin/python scripts/load_fixtures_to_db.py

Seeds a throwaway server folder with build_guide_server.py (anonymised fixtures only: ALMA and HERBAR, sessions
with packing lists, packer settings, workers), then copies every file into the database through SupabaseStore,
so the rows are what the apps themselves write. Rerunning overwrites the same paths, plus new sessions of today.

Target: FULFILLMENT_DB_URL / FULFILLMENT_DB_KEY (shared/db_config.py). With SUPABASE_SERVICE_KEY set it uses
that key (local stack); otherwise it asks for an email and password and signs in.
"""

import getpass
import os
import socket
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from scripts.build_guide_server import build
from shared import db_config
from shared.store_supabase import SupabaseStore
from supabase import create_client

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


def _client():
    service_key = os.environ.get("SUPABASE_SERVICE_KEY")
    if service_key:
        return create_client(db_config.SUPABASE_URL, service_key)
    client = create_client(db_config.SUPABASE_URL, db_config.SUPABASE_PUBLISHABLE_KEY)
    client.auth.sign_in_with_password({"email": input("Email: "), "password": getpass.getpass()})
    return client


def main() -> None:
    print(f"target {db_config.SUPABASE_URL}")
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp) / "server"
        build(root)
        store = SupabaseStore(_client(), pc=f"loader-{socket.gethostname()}", cache_dir=Path(tmp) / "cache")
        print(f"loaded {load(store, root)} files")


if __name__ == "__main__":
    main()
