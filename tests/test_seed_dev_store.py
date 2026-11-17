import json

import httpx
import pandas as pd
import pytest

from scripts import seed_dev_store
from scripts.load_fixtures_to_db import FIXTURES, _latest_config
from scripts.seed_dev_store import (
    FAKE_NAMES,
    SeedPlan,
    build_seed,
    main,
    push_seed,
    write_stock_csv,
)
from shopify_tool.order_sources.shopify import API_VERSION

SHOP = "x.myshopify.com"


def _fixture_frames() -> list[pd.DataFrame]:
    return [pd.read_csv(f, dtype=str, keep_default_na=False) for f in (FIXTURES / "ALMA").glob("*/orders.csv")]


def test_fake_names_count_and_unique():
    assert len(FAKE_NAMES) >= 200
    assert len(set(FAKE_NAMES)) == len(FAKE_NAMES)


def test_build_seed_is_deterministic():
    assert build_seed("ALMA", FIXTURES, 50, 7) == build_seed("ALMA", FIXTURES, 50, 7)
    assert build_seed("ALMA", FIXTURES, 50, 7) != build_seed("ALMA", FIXTURES, 50, 8)


def test_build_seed_uses_fixture_skus_and_fake_names():
    plan = build_seed("ALMA", FIXTURES, 50, 7)
    frames = _fixture_frames()
    skus = set().union(*(set(df["Lineitem sku"]) for df in frames)) - {""}
    fixture_values = set().union(*(set(df["Shipping Method"]) | set(df["Shipping Country"]) for df in frames))

    assert all(p["sku"] in skus for p in plan.products)
    assert all(line["sku"] in skus for o in plan.orders for line in o["lines"] if line["sku"] is not None)
    assert all(o["name"] in FAKE_NAMES for o in plan.orders)
    assert all(o["country"] in fixture_values and o["shipping_method"] in fixture_values for o in plan.orders)


def test_build_seed_no_sku_share():
    plan = build_seed("ALMA", FIXTURES, 300, 1)
    lines = [line for o in plan.orders for line in o["lines"]]
    share = sum(line["sku"] is None for line in lines) / len(lines)
    assert 0.01 <= share <= 0.02


def _plan(n_products: int, n_orders: int) -> SeedPlan:
    products = [{"sku": f"SKU-{i}", "title": f"Product {i}"} for i in range(n_products)]
    line = {"sku": "SKU-0", "title": "Product 0", "quantity": 1, "price": "9.99"}
    orders = [
        {"name": "Testa Probova", "country": "BG", "shipping_method": "Test", "lines": [line]}
        for _ in range(n_orders)
    ]
    return SeedPlan("ALMA", products, orders, {p["sku"]: 5 for p in products})


def _responder(seen: list, product_errors: list | None = None):
    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        body = json.loads(request.content)
        if "productCreate" in body["query"]:
            if product_errors and body["variables"]["product"]["title"] in product_errors:
                payload = {"productCreate": {"product": None, "userErrors": [{"field": ["title"], "message": "bad"}]}}
            else:
                payload = {"productCreate": {"product": {"id": "gid://shopify/Product/1"}, "userErrors": []}}
        else:
            payload = {"orderCreate": {"order": {"id": "gid://shopify/Order/1"}, "userErrors": []}}
        return httpx.Response(200, json={"data": payload})

    return handler


def test_push_seed_sends_products_then_orders():
    seen = []
    push_seed(_plan(3, 2), SHOP, "tok", transport=httpx.MockTransport(_responder(seen)))

    queries = [json.loads(r.content)["query"] for r in seen]
    assert ["productCreate" in q for q in queries] == [True, True, True, False, False]
    assert ["orderCreate" in q for q in queries] == [False, False, False, True, True]
    assert all(r.headers["X-Shopify-Access-Token"] == "tok" for r in seen)
    assert all(str(r.url) == f"https://{SHOP}/admin/api/{API_VERSION}/graphql.json" for r in seen)


def test_push_seed_raises_on_user_errors():
    seen = []
    transport = httpx.MockTransport(_responder(seen, product_errors=["Product 0"]))
    with pytest.raises(RuntimeError, match="SKU-0"):
        push_seed(_plan(3, 2), SHOP, "tok", transport=transport)
    assert len(seen) == 1


def test_write_stock_csv_headers(tmp_path):
    plan = build_seed("ALMA", FIXTURES, 5, 1)
    out = tmp_path / "stock.csv"
    write_stock_csv(plan, out, _latest_config(FIXTURES / "ALMA"))

    assert out.read_bytes().decode("utf-8").splitlines()[0] == "Артикул,Име,Наличност"
    df = pd.read_csv(out, dtype=str, keep_default_na=False)
    assert list(df["Артикул"]) == [p["sku"] for p in plan.products]


@pytest.mark.parametrize("shop", ["example.com", "evil.example/.myshopify.com", "x.myshopify.com.evil.io"])
def test_main_refuses_non_myshopify_shop(monkeypatch, shop):
    def no_request(*args, **kwargs):
        raise AssertionError("no request expected")

    monkeypatch.setattr(seed_dev_store, "push_seed", no_request)
    monkeypatch.setenv("SHOPIFY_SEED_TOKEN", "t")
    with pytest.raises(SystemExit) as exc:
        main(["--client", "ALMA", "--shop", shop])
    assert exc.value.code == 2


def test_main_refuses_without_token(monkeypatch, capsys):
    def no_request(*args, **kwargs):
        raise AssertionError("no request expected")

    monkeypatch.setattr(seed_dev_store, "push_seed", no_request)
    monkeypatch.delenv("SHOPIFY_SEED_TOKEN", raising=False)
    with pytest.raises(SystemExit) as exc:
        main(["--client", "ALMA", "--shop", SHOP])
    assert exc.value.code == 2
    assert "SHOPIFY_SEED_TOKEN" in capsys.readouterr().err
