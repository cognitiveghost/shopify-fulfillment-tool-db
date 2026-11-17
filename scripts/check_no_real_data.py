"""Check that the Supabase trial data holds no real customer name or note (spec A2).

    .venv/bin/python scripts/check_no_real_data.py

Exit 0 and print `ok` when every Customer and Notes value is anonymised; exit 1 listing up to 20 offenders
otherwise. Reads only through the signed-in client from load_fixtures_to_db._client(); the only local file it
writes is a scratch copy of an orders CSV, in a temp dir that is removed afterwards.
"""

import sys
import tempfile
from collections.abc import Iterator
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path[:0] = [str(REPO), str(REPO / "tests" / "replay")]
from anonymise import mask_text, rule_words

from scripts.load_fixtures_to_db import _client
from scripts.seed_dev_store import FAKE_NAMES
from shared.store import StoreNotFound
from shared.store_supabase import SupabaseStore
from shopify_tool.core import read_orders_csv

FIELDS = ("Customer", "Notes")
PLACEHOLDER = {"Customer": "Customer", "Notes": "Note"}
_KEYS = {"customer": "Customer", "notes": "Notes"}
# table: (value column, primary key); the key's first column is client_id
TABLES = {
    "session_documents": ("body", ("client_id", "session_name", "path")),
    "packing_lists": ("body", ("client_id", "session_name", "name")),
    "fulfillment_history": ("row", ("client_id", "row_no")),
}
SHOWN = 20


def accepted(value, placeholder: str, words: list[str]) -> bool:
    text = "" if value is None else str(value)
    if text.strip() in ("", "nan") or text in FAKE_NAMES:
        return True
    return mask_text(text, placeholder, words) == text


def person_values(doc) -> Iterator[tuple[str, object]]:
    if isinstance(doc, dict):
        for key, value in doc.items():
            if key.lower() in _KEYS:
                yield key, value
            yield from person_values(value)
    elif isinstance(doc, list):
        for item in doc:
            yield from person_values(item)


def _select(client, table: str, columns: str, keys: tuple[str, ...]) -> list[dict]:
    def query():
        q = client.table(table).select(columns, count="exact")
        for key in keys:
            q = q.order(key)
        return q

    return SupabaseStore._all(query)


def _words(client) -> tuple[dict[str, dict[str, list[str]]], dict[str, dict]]:
    configs = {r["client_id"]: r["shopify_config"] or {}
               for r in _select(client, "clients", "client_id,shopify_config", ("client_id",))}
    return {cid: {field: rule_words(cfg, field) for field in FIELDS} for cid, cfg in configs.items()}, configs


def _json_offenders(label: str, doc, words: dict[str, list[str]]) -> list[str]:
    out = []
    for key, value in person_values(doc):
        field = _KEYS[key.lower()]
        if value == key:  # a column mapping entry in shopify_config, e.g. "Notes": "Notes"
            continue
        if not accepted(value, PLACEHOLDER[field], words[field]):
            out.append(f"{label}: {key}={value}")
    return out


def _csv_offenders(client, configs: dict, words: dict) -> list[str]:
    out = []
    with tempfile.TemporaryDirectory() as tmp:
        store = SupabaseStore(client, pc="check", cache_dir=Path(tmp))
        for session in _select(client, "sessions", "client_id,name", ("client_id", "name")):
            cid = session["client_id"]
            rel = f"Sessions/CLIENT_{cid}/{session['name']}/input/orders_export.csv"
            try:
                data = store.read_bytes(rel)
            except StoreNotFound:
                continue
            path = Path(tmp) / "orders.csv"
            path.write_bytes(data)
            config = configs[cid]
            df = read_orders_csv(path, config["column_mappings"])
            for header, internal in config["column_mappings"]["orders"].items():
                if internal not in FIELDS or header not in df:
                    continue
                for value in df[header]:
                    if not accepted(value, PLACEHOLDER[internal], words[cid][internal]):
                        out.append(f"{rel}: {header}={value}")
    return out


def offenders(client) -> list[str]:
    words, configs = _words(client)
    out = []
    for table, (column, keys) in TABLES.items():
        for row in _select(client, table, ",".join((*keys, column)), keys):
            label = f"{table} " + "/".join(str(row[k]) for k in keys)
            out += _json_offenders(label, row[column], words[row["client_id"]])
    return out + _csv_offenders(client, configs, words)


def main() -> None:
    found = offenders(_client())
    if found:
        for line in found[:SHOWN]:
            print(line)
        print(f"{len(found)} offender(s)")
        sys.exit(1)
    print("ok")


if __name__ == "__main__":
    main()
