"""Seed a Shopify dev store with fake test orders and stock, shaped like a replay client's fixtures.

    SHOPIFY_SEED_TOKEN=... .venv/bin/python scripts/seed_dev_store.py --client ALMA --shop <x>.myshopify.com \
        --orders 300 [--seed 1] [--stock-out PATH]

Products, SKUs, shipping methods and countries come from tests/replay/fixtures; customer names are fake
(FAKE_NAMES). Orders are test orders (no receipts sent). Lines are custom line items (title, sku and price, no
variant), so the store needs no product variants. The token is read from SHOPIFY_SEED_TOKEN only: never argv,
never printed. The shop must be <x>.myshopify.com. A run that fails midway leaves a partial seed, and a rerun
adds duplicates: start again on a fresh dev store.
"""

import argparse
import os
import random
import re
import sys
import time
from dataclasses import dataclass
from pathlib import Path

import httpx
import pandas as pd

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from scripts.load_fixtures_to_db import FIXTURES, _latest_config, stock_csv_bytes
from shopify_tool.order_sources.shopify import API_VERSION

_FIRST = (
    "Testa", "Proba", "Fiktiva", "Demo", "Probna", "Testova", "Fiktivna", "Pilotna", "Vzorna", "Opitna",
    "Mokka", "Fakina", "Shablona", "Primerna", "Kukla", "Nulev", "Probnik", "Testik", "Fiktik", "Mokkina",
)
_LAST = (
    "Probova", "Fiktiva", "Testova", "Demova", "Mokkova", "Vzorova", "Opitova", "Nulova", "Pilotova", "Shablonova",
)
FAKE_NAMES: tuple[str, ...] = tuple(f"{first} {last}" for first in _FIRST for last in _LAST)

CURRENCY = "EUR"
_SKU_SHARE = 0.015  # share of order lines sent without a SKU (exercises the no-SKU rule)

_PRODUCT_CREATE = """mutation($product: ProductCreateInput!) {
  productCreate(product: $product) { product { id } userErrors { field message } }
}"""
_ORDER_CREATE = """mutation($order: OrderCreateOrderInput!, $options: OrderCreateOptionsInput) {
  orderCreate(order: $order, options: $options) { order { id } userErrors { field message } }
}"""


@dataclass
class SeedPlan:
    client: str
    products: list[dict]  # {"sku", "title"}
    orders: list[dict]  # {"name", "country", "shipping_method", "lines": [{"sku", "title", "quantity", "price"}]}
    stock: dict[str, int]  # sku -> quantity


def build_seed(client: str, fixtures: Path, n_orders: int, rng_seed: int) -> SeedPlan:
    frames = [pd.read_csv(f, dtype=str, keep_default_na=False) for f in sorted((fixtures / client).glob("*/orders.csv"))]
    rows = pd.concat(frames, ignore_index=True)
    titles = {}
    for sku, title in zip(rows["Lineitem sku"], rows["Lineitem name"]):
        if sku:
            titles.setdefault(sku, title)
    products = [{"sku": sku, "title": titles[sku]} for sku in sorted(titles)]
    methods = sorted(set(rows["Shipping Method"]) - {""})
    countries = sorted(set(rows["Shipping Country"]) - {""})

    rng = random.Random(rng_seed)
    stock = {p["sku"]: rng.randint(0, 200) for p in products}
    orders = []
    for _ in range(n_orders):
        lines = []
        for _ in range(rng.randint(1, 3)):
            product = rng.choice(products)
            lines.append(
                {
                    "sku": None if rng.random() < _SKU_SHARE else product["sku"],
                    "title": product["title"],
                    "quantity": rng.randint(1, 3),
                    "price": f"{rng.randint(500, 15000) / 100:.2f}",
                }
            )
        orders.append(
            {
                "name": rng.choice(FAKE_NAMES),
                "country": rng.choice(countries),
                "shipping_method": rng.choice(methods),
                "lines": lines,
            }
        )
    return SeedPlan(client, products, orders, stock)


_THROTTLED = "Too many attempts"  # dev stores allow about 5 orderCreate calls a minute


def _graphql(http: httpx.Client, url: str, query: str, variables: dict, field: str, label: str, sleep=time.sleep) -> None:
    while True:
        resp = http.post(url, json={"query": query, "variables": variables})
        resp.raise_for_status()
        body = resp.json()
        if body.get("errors"):
            raise RuntimeError(f"{field} failed for {label}: {body['errors']}")
        user_errors = ((body.get("data") or {}).get(field) or {}).get("userErrors") or []
        if user_errors and all(_THROTTLED in e["message"] for e in user_errors):
            sleep(60)
            continue
        if user_errors:
            raise RuntimeError(f"{field} failed for {label}: {user_errors}")
        return


def _money(amount: str) -> dict:
    return {"shopMoney": {"amount": amount, "currencyCode": CURRENCY}}


def _order_variables(order: dict) -> dict:
    first, _, last = order["name"].partition(" ")
    return {
        "order": {
            "test": True,
            "currency": CURRENCY,
            "shippingAddress": {"firstName": first, "lastName": last, "countryCode": order["country"]},
            "shippingLines": [{"title": order["shipping_method"], "priceSet": _money("0.00")}],
            "lineItems": [
                {
                    "title": line["title"],
                    **({"sku": line["sku"]} if line["sku"] else {}),
                    "quantity": line["quantity"],
                    "priceSet": _money(line["price"]),
                    "requiresShipping": True,
                }
                for line in order["lines"]
            ],
        },
        "options": {"sendReceipt": False, "sendFulfillmentReceipt": False, "inventoryBehaviour": "BYPASS"},
    }


def push_seed(
    plan: SeedPlan, shop: str, token: str, transport: httpx.BaseTransport | None = None, resume_from: int = 0, sleep=time.sleep
) -> None:
    """resume_from > 0 skips the products and the first resume_from orders (already in the store)."""
    url = f"https://{shop}/admin/api/{API_VERSION}/graphql.json"
    with httpx.Client(transport=transport, headers={"X-Shopify-Access-Token": token}) as http:
        for product in plan.products if not resume_from else []:
            variables = {"product": {"title": product["title"]}}
            _graphql(http, url, _PRODUCT_CREATE, variables, "productCreate", f"product {product['sku']}")
        for i, order in enumerate(plan.orders[resume_from:], start=resume_from):
            variables = _order_variables(order)
            _graphql(http, url, _ORDER_CREATE, variables, "orderCreate", f"order {i} {order['name']}", sleep)


def write_stock_csv(plan: SeedPlan, path: Path, config: dict) -> None:
    df = pd.DataFrame(
        {
            "SKU": [p["sku"] for p in plan.products],
            "Product_Name": [p["title"] for p in plan.products],
            "Stock": [plan.stock[p["sku"]] for p in plan.products],
        }
    )
    Path(path).write_bytes(stock_csv_bytes(df, config))


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Seed a Shopify dev store with fake orders and stock.")
    parser.add_argument("--client", required=True, choices=("ALMA", "HERBAR", "LIDA"))
    parser.add_argument("--shop", required=True, help="the dev store, <x>.myshopify.com")
    parser.add_argument("--orders", type=int, default=300)
    parser.add_argument("--seed", type=int, default=1)
    parser.add_argument("--resume-from", type=int, default=0, metavar="N", help="skip products and the first N orders")
    parser.add_argument("--stock-out", type=Path, metavar="PATH", help="also write the stock CSV here")
    args = parser.parse_args(argv)

    token = os.environ.get("SHOPIFY_SEED_TOKEN")
    if not token:
        print("refusing: set SHOPIFY_SEED_TOKEN in the environment (never on the command line)", file=sys.stderr)
        sys.exit(2)
    if not re.fullmatch(r"[a-z0-9-]+\.myshopify\.com", args.shop):
        print(f"refusing: {args.shop} does not end in .myshopify.com", file=sys.stderr)
        sys.exit(2)

    plan = build_seed(args.client, FIXTURES, args.orders, args.seed)
    push_seed(plan, args.shop, token, resume_from=args.resume_from)
    print(f"pushed {len(plan.products)} products and {len(plan.orders)} orders to {args.shop}")
    if args.stock_out:
        write_stock_csv(plan, args.stock_out, _latest_config(FIXTURES / args.client))


if __name__ == "__main__":
    main()
