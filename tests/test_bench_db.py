"""The DB benchmark's helpers: the local-only guard, the request counter and the hosted-time estimate."""

import importlib.util
from pathlib import Path

import httpx
import pytest

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "bench_db.py"


def _load_bench():
    spec = importlib.util.spec_from_file_location("bench_db", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


bench = _load_bench()


def test_require_local_refuses_hosted():
    with pytest.raises(SystemExit):
        bench.require_local("https://ntzqarcksthplcdzivep.supabase.co")
    with pytest.raises(SystemExit):
        bench.require_local(None)
    assert bench.require_local("http://127.0.0.1:54321") == "http://127.0.0.1:54321"


def test_count_requests_counts_client_sends():
    t = httpx.MockTransport(lambda r: httpx.Response(200))
    with bench.count_requests() as c, httpx.Client(transport=t) as cl:
        cl.get("http://x/a")
        cl.get("http://x/b")
    assert c.n == 2


def test_est_hosted_ms():
    assert bench.est_hosted_ms(10.0, 4, 50.0) == 210.0
