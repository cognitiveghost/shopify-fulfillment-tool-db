"""Give some seeded dev-store orders lines that the pull must not pack: removed by an order edit, or refunded.

    SHOPIFY_SEED_TOKEN=... .venv/bin/python scripts/seed_removed_lines.py --shop <x>.myshopify.com \
        [--edits 10] [--refunds 10] [--partial 5]

Takes open multi-line orders, oldest first, that no earlier run touched, and tags each one with what was done:
- seed-removed-line: one line set to 0 by an order edit (needs the write_order_edits scope),
- seed-refunded-line: one line fully refunded,
- seed-partial-refund: one unit refunded from a line of quantity 2 or more.
The CSV export still lists those lines at their ordered quantity; the API pull reads currentQuantity and must
pack only what is left. Nothing is restocked or emailed. The token is read from SHOPIFY_SEED_TOKEN only.
"""

import argparse
import os
import re
import sys
from pathlib import Path

import httpx

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from scripts.seed_dev_store import _graphql
from shopify_tool.order_sources.shopify import API_VERSION

TAGS = ("seed-removed-line", "seed-refunded-line", "seed-partial-refund")

_ORDERS = """query($cursor: String) {
  orders(first: 100, after: $cursor, sortKey: CREATED_AT, query: "status:open fulfillment_status:unfulfilled") {
    pageInfo { hasNextPage endCursor }
    nodes { id name tags lineItems(first: 10) { nodes { id currentQuantity } } }
  }
}"""
_REFUND = """mutation($input: RefundInput!, $key: String!) {
  refundCreate(input: $input) @idempotent(key: $key) { refund { id } userErrors { field message } }
}"""
_TAGS_ADD = """mutation($id: ID!, $tags: [String!]!) {
  tagsAdd(id: $id, tags: $tags) { node { id } userErrors { field message } }
}"""
_EDIT_BEGIN = """mutation($id: ID!) {
  orderEditBegin(id: $id) {
    calculatedOrder { id lineItems(first: 10) { nodes { id quantity } } }
    userErrors { field message }
  }
}"""
_EDIT_SET = """mutation($id: ID!, $lineItemId: ID!) {
  orderEditSetQuantity(id: $id, lineItemId: $lineItemId, quantity: 0) {
    calculatedOrder { id } userErrors { field message }
  }
}"""
_EDIT_COMMIT = """mutation($id: ID!) {
  orderEditCommit(id: $id, notifyCustomer: false, staffNote: "seed: line removed") {
    order { id } userErrors { field message }
  }
}"""


def pick(orders: list[dict], edits: int, refunds: int, partial: int) -> list[tuple[str, dict]]:
    """Pure: (tag, order) pairs. Untouched orders with 2+ live lines; partial needs a line of quantity >= 2."""
    plan = []
    want = {"seed-removed-line": edits, "seed-refunded-line": refunds, "seed-partial-refund": partial}
    for order in orders:
        if set(order["tags"]) & set(TAGS):
            continue
        live = [line for line in order["lineItems"]["nodes"] if line["currentQuantity"] > 0]
        if len(live) < 2:
            continue
        if want["seed-partial-refund"] and any(line["currentQuantity"] >= 2 for line in live):
            tag = "seed-partial-refund"
        elif want["seed-refunded-line"]:
            tag = "seed-refunded-line"
        elif want["seed-removed-line"]:
            tag = "seed-removed-line"
        else:
            continue
        want[tag] -= 1
        plan.append((tag, order))
        if not any(want.values()):
            break
    return plan


def _post(http: httpx.Client, url: str, query: str, variables: dict) -> dict:
    resp = http.post(url, json={"query": query, "variables": variables})
    resp.raise_for_status()
    body = resp.json()
    if body.get("errors"):
        raise RuntimeError(body["errors"])
    return body["data"]


def _open_orders(http: httpx.Client, url: str) -> list[dict]:
    orders, cursor = [], None
    while True:
        page = _post(http, url, _ORDERS, {"cursor": cursor})["orders"]
        orders.extend(page["nodes"])
        if not page["pageInfo"]["hasNextPage"]:
            return orders
        cursor = page["pageInfo"]["endCursor"]


def apply(http: httpx.Client, url: str, tag: str, order: dict) -> None:
    live = [line for line in order["lineItems"]["nodes"] if line["currentQuantity"] > 0]
    label = f"{tag} {order['name']}"
    if tag == "seed-removed-line":
        data = _post(http, url, _EDIT_BEGIN, {"id": order["id"]})["orderEditBegin"]
        if data["userErrors"]:
            raise RuntimeError(f"orderEditBegin failed for {label}: {data['userErrors']}")
        calculated = data["calculatedOrder"]
        line_id = next(line["id"] for line in calculated["lineItems"]["nodes"] if line["quantity"] > 0)
        _graphql(http, url, _EDIT_SET, {"id": calculated["id"], "lineItemId": line_id}, "orderEditSetQuantity", label)
        _graphql(http, url, _EDIT_COMMIT, {"id": calculated["id"]}, "orderEditCommit", label)
    else:
        if tag == "seed-partial-refund":
            line, quantity = next(line for line in live if line["currentQuantity"] >= 2), 1
        else:
            line = live[0]
            quantity = line["currentQuantity"]
        refund = {
            "orderId": order["id"],
            "notify": False,
            "note": f"seed: {tag}",
            "refundLineItems": [{"lineItemId": line["id"], "quantity": quantity, "restockType": "NO_RESTOCK"}],
        }
        _graphql(http, url, _REFUND, {"input": refund, "key": f"{order['id']}/{tag}"}, "refundCreate", label)
    _graphql(http, url, _TAGS_ADD, {"id": order["id"], "tags": [tag]}, "tagsAdd", label)


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Remove or refund lines on some seeded dev-store orders.")
    parser.add_argument("--shop", required=True, help="the dev store, <x>.myshopify.com")
    parser.add_argument("--edits", type=int, default=10)
    parser.add_argument("--refunds", type=int, default=10)
    parser.add_argument("--partial", type=int, default=5)
    args = parser.parse_args(argv)

    token = os.environ.get("SHOPIFY_SEED_TOKEN")
    if not token:
        print("refusing: set SHOPIFY_SEED_TOKEN in the environment (never on the command line)", file=sys.stderr)
        sys.exit(2)
    if not re.fullmatch(r"[a-z0-9-]+\.myshopify\.com", args.shop):
        print(f"refusing: {args.shop} does not end in .myshopify.com", file=sys.stderr)
        sys.exit(2)

    url = f"https://{args.shop}/admin/api/{API_VERSION}/graphql.json"
    with httpx.Client(headers={"X-Shopify-Access-Token": token}, timeout=30.0) as http:
        plan = pick(_open_orders(http, url), args.edits, args.refunds, args.partial)
        for tag, order in plan:
            apply(http, url, tag, order)
            print(f"{order['name']}: {tag}")
    print(f"changed {len(plan)} orders on {args.shop}")


if __name__ == "__main__":
    main()
