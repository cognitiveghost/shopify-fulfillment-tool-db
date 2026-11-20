"""Benchmark the DB trial's hot paths on the local Supabase stack or on a file store.

    FULFILLMENT_DB_URL=http://127.0.0.1:54321 SUPABASE_SERVICE_KEY=... \\
        .venv/bin/python scripts/bench_db.py --store supabase --rtt-ms 0
    .venv/bin/python scripts/bench_db.py --store file

Prints one Markdown table: action | store | wall ms | HTTP requests | est. hosted ms.

- Supabase: reset and load the stack first (docs/trial-checklist.md "Reset"). The script never resets it, and
  each run adds one ALMA session, so run once per reset. Refuses any URL that is not 127.0.0.1 or localhost.
- File: a FileStore in a temp folder, loaded with the same replay fixtures as the stack.
- est. hosted ms = wall ms + HTTP requests * RTT. Without --rtt-ms, RTT is the median of 5 GETs to the hosted
  project's /auth/v1/health (the one network call this script makes).
"""

import argparse
import json
import os
import statistics
import sys
import tempfile
import time
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import httpx

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

FIXTURES = REPO / "tests" / "replay" / "fixtures"
CLIENT = "ALMA"
HOSTED_HEALTH = "https://ntzqarcksthplcdzivep.supabase.co/auth/v1/health"
LOCAL_HOSTS = {"127.0.0.1", "localhost"}
ORDER_PAGES = REPO / "tests" / "order_sources" / "fixtures"


@dataclass
class Row:
    action: str
    store: str
    wall_ms: float
    requests: int
    est_ms: float


class _Count:
    n = 0


@contextmanager
def count_requests():
    """Count every httpx.Client.send while active (supabase-py's sub-clients all go through it)."""
    counter = _Count()
    real_send = httpx.Client.send

    def counting_send(self, request, *args, **kwargs):
        counter.n += 1
        return real_send(self, request, *args, **kwargs)

    httpx.Client.send = counting_send
    try:
        yield counter
    finally:
        httpx.Client.send = real_send


def require_local(url: str | None) -> str:
    """Refuse to benchmark anything but the local stack: db_config falls back to the hosted URL."""
    if (urlparse(url or "").hostname or "") not in LOCAL_HOSTS:
        raise SystemExit(
            f"refusing: FULFILLMENT_DB_URL must be 127.0.0.1 or localhost, got {url!r}"
        )
    return url


def est_hosted_ms(wall_ms: float, requests: int, rtt_ms: float) -> float:
    return wall_ms + requests * rtt_ms


def measure_rtt(url: str, key: str, n: int = 5) -> float:
    """Median wall time of n GETs to the health endpoint, in ms."""
    times = []
    for _ in range(n):
        t0 = time.perf_counter()
        httpx.get(url, headers={"apikey": key}, timeout=30)
        times.append((time.perf_counter() - t0) * 1000)
    return statistics.median(times)


def timed(fn):
    """Run fn; return (result, wall ms, HTTP requests)."""
    with count_requests() as counter:
        t0 = time.perf_counter()
        out = fn()
        wall = (time.perf_counter() - t0) * 1000
    return out, wall, counter.n


def render(rows: list[Row], rtt_note: str) -> str:
    lines = [
        "| action | store | wall ms | HTTP requests | est. hosted ms |",
        "|---|---|---:|---:|---:|",
    ]
    lines += [
        f"| {r.action} | {r.store} | {r.wall_ms:.1f} | {r.requests} | {r.est_ms:.1f} |"
        for r in rows
    ]
    return "\n".join(lines) + f"\n\n{rtt_note}\n"


def replay_session(name: str | None) -> str:
    sessions = sorted(
        p.name for p in (FIXTURES / CLIENT).iterdir() if (p / "meta.json").exists()
    )
    if not sessions:
        raise SystemExit(f"no replay sessions under {FIXTURES / CLIENT}")
    return name or sessions[0]


def make_supabase_store(cache_dir: Path):
    from shared.store_supabase import SupabaseStore
    from supabase import create_client

    url = require_local(os.environ.get("FULFILLMENT_DB_URL"))
    key = os.environ.get("SUPABASE_SERVICE_KEY")
    if not key:
        raise SystemExit("SUPABASE_SERVICE_KEY is not set")
    return SupabaseStore(create_client(url, key), pc="bench", cache_dir=cache_dir)


def make_file_store(root: Path):
    """Replay fixtures, loaded the way the loader's --fixtures mode loads them into Supabase."""
    from scripts.load_fixtures_to_db import build_fixture_server, load
    from shared.store import FileStore

    server = root / "server"
    build_fixture_server(server, FIXTURES, CLIENT)
    store = FileStore(root / "share")
    load(store, server)
    return store


def _orders_handler(request: httpx.Request) -> httpx.Response:
    pages = {
        None: json.loads(
            (ORDER_PAGES / "orders_page_1.json").read_text(encoding="utf-8")
        ),
        "orders-cursor-1": json.loads(
            (ORDER_PAGES / "orders_page_2.json").read_text(encoding="utf-8")
        ),
    }
    return httpx.Response(
        200, json=pages[json.loads(request.content)["variables"].get("cursor")]
    )


def run_benchmark(
    store, store_label: str, source: str, rtt_ms: float, tmp: Path
) -> list[Row]:
    from PySide6.QtCore import QThreadPool
    from PySide6.QtWidgets import QApplication

    from gui import main_window_pyside as window_module
    from gui import session_browser_widget as browser_module
    from shared.store_mode import Mode
    from shopify_tool import core, report_jobs, session_state
    from shopify_tool.csv_utils import AUTO_DELIMITER
    from shopify_tool.order_sources.shopify import fetch_shopify_orders
    from shopify_tool.profile_manager import ProfileManager
    from shopify_tool.session_manager import SessionManager

    app = QApplication.instance() or QApplication([])  # noqa: F841 - the window needs one
    pm = ProfileManager(str(tmp / "config.ini"), store=store, require_connection=False)
    sm = SessionManager(pm, store)
    config = pm.load_shopify_config(CLIENT)
    src = f"Sessions/CLIENT_{CLIENT}/{source}"
    rows: list[Row] = []
    # Background work (the window's un-awaited ClientDirectory.gather, for one) would hit the store
    # during whichever step is timing. Each step waits for these pools and the queued signals first.
    pools = [QThreadPool.globalInstance()]

    def settle():
        for pool in pools:
            if not pool.waitForDone(30_000):
                raise SystemExit("background work did not finish within 30 s")
        QApplication.processEvents()

    def add(action, fn):
        settle()
        out, wall, requests = timed(fn)
        rows.append(
            Row(
                action,
                store_label,
                wall,
                requests,
                est_hosted_ms(wall, requests, rtt_ms),
            )
        )
        return out

    # (c) analysis: a fresh session with the replay session's inputs, copied through the store first.
    session_path = sm.create_session(CLIENT)
    rel = session_state.session_rel(store, session_path)
    for name in ("orders_export.csv", "inventory.csv"):
        store.write_bytes(
            f"{rel}/input/{name}", store.read_bytes(f"{src}/input/{name}")
        )

    def analyse():
        result = core.run_full_analysis(
            str(store.local_path(f"{rel}/input/inventory.csv")),
            str(store.local_path(f"{rel}/input/orders_export.csv")),
            None,
            AUTO_DELIMITER,
            AUTO_DELIMITER,
            config,
            client_id=CLIENT,
            session_manager=sm,
            profile_manager=pm,
            session_path=session_path,
        )
        if not result[0]:
            raise SystemExit(f"analysis failed: {result[1]}")
        return result[2]

    analysis_df = add("c analysis", analyse)

    # (a) session open: the window's read and apply, on the UI thread, over this store.
    window_module.open_profile_manager = lambda _p: (
        Mode.SUPABASE if store_label == "supabase" else None,
        pm,
    )
    browser_module.SessionBrowserWidget.USE_ASYNC = False
    win = window_module.MainWindow()
    pools.append(win.threadpool)
    try:
        win.show()
        QApplication.processEvents()
        win.load_client_config(CLIENT)
        add("a session open", lambda: win.load_existing_session(session_path))
    finally:
        win.close()

    # (b) pull write: the orders frame a pull produces, written to the session and loaded back.
    frame = fetch_shopify_orders(
        "bench.myshopify.com",
        "bench-token",
        transport=httpx.MockTransport(_orders_handler),
    )
    body = frame.to_csv(index=False).encode("utf-8")

    def pull_write():
        store.write_bytes(f"{rel}/input/orders_export.csv", body)
        return store.local_path(f"{rel}/input/orders_export.csv")

    add("b pull write", pull_write)

    # (d) packing lists: the batch the generate-reports worker builds from the client's packing configs.
    batch = [
        dict(c, report_type="packing_lists")
        for c in config.get("packing_list_configs", [])
    ]

    def packing_lists():
        return report_jobs.generate_reports(
            store,
            batch,
            session_path,
            analysis_df.copy(),
            dict(config.get("tag_categories", {})),
            Path(session_path).name,
            session_manager=sm,
        )

    add("d packing lists", packing_lists)
    return rows


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Benchmark the DB trial's hot paths.")
    parser.add_argument("--store", choices=["supabase", "file"], required=True)
    parser.add_argument(
        "--rtt-ms", type=float, help="RTT for est. hosted ms; skips the hosted probe"
    )
    parser.add_argument(
        "--session", help="replay session to copy (default: first under ALMA)"
    )
    args = parser.parse_args(argv)
    source = replay_session(args.session)

    with tempfile.TemporaryDirectory() as tmp_name:
        tmp = Path(tmp_name)
        if args.store == "supabase":
            store, label = make_supabase_store(tmp / "cache"), "supabase"
            if args.rtt_ms is not None:
                rtt, note = args.rtt_ms, f"RTT used: {args.rtt_ms:.1f} ms (--rtt-ms)"
            else:
                from shared.db_config import SUPABASE_PUBLISHABLE_KEY

                rtt = measure_rtt(HOSTED_HEALTH, SUPABASE_PUBLISHABLE_KEY)
                note = f"RTT used: {rtt:.1f} ms (median of 5 GETs to {HOSTED_HEALTH})"
        else:
            store, label = make_file_store(tmp), "file (local disk)"
            rtt, note = 0.0, "RTT: n/a for file (local disk, no HTTP); est. = wall"
        rows = run_benchmark(store, label, source, rtt, tmp)
    print(f"Session copied: {source}\n")
    print(render(rows, note))


if __name__ == "__main__":
    main()
