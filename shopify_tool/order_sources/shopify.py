"""Shopify Admin GraphQL order fetcher: open, unfulfilled orders as one row per line item."""

import time

import httpx
import pandas as pd

from shopify_tool.order_sources import SHOPIFY_CSV_HEADERS, OrderSourceError

API_VERSION = "2026-10"
MAX_THROTTLE_RETRIES = 5
_TIMEOUT_SECONDS = 30.0
_sleep = time.sleep  # tests patch this

# Page sizes keep the requested cost under Shopify's 1,000-point cap per query:
# each order costs ~10 points plus one per line item asked for, so 25 x (10 + 20).
_ORDERS_QUERY = """
query Orders($cursor: String) {
  orders(first: 25, after: $cursor, query: "fulfillment_status:unfulfilled status:open") {
    pageInfo { hasNextPage endCursor }
    nodes {
      id
      name
      createdAt
      note
      tags
      subtotalPriceSet { shopMoney { amount } }
      totalPriceSet { shopMoney { amount } }
      shippingLine { title }
      shippingAddress { name countryCodeV2 }
      lineItems(first: 20) {
        pageInfo { hasNextPage endCursor }
        nodes { sku name quantity }
      }
    }
  }
}
"""

_LINE_ITEMS_QUERY = """
query LineItems($id: ID!, $cursor: String) {
  node(id: $id) {
    ... on Order {
      lineItems(first: 50, after: $cursor) {
        pageInfo { hasNextPage endCursor }
        nodes { sku name quantity }
      }
    }
  }
}
"""


def fetch_shopify_orders(
    shop: str, token: str, *, transport: httpx.BaseTransport | None = None
) -> pd.DataFrame:
    """Return the store's open unfulfilled orders with the Shopify CSV export headers."""
    url = f"https://{shop}/admin/api/{API_VERSION}/graphql.json"
    rows = []
    with httpx.Client(
        transport=transport,
        timeout=_TIMEOUT_SECONDS,
        headers={"X-Shopify-Access-Token": token},
    ) as client:
        cursor = None
        while True:
            page = _post(client, url, _ORDERS_QUERY, {"cursor": cursor})["orders"]
            for order in page["nodes"]:
                rows.extend(_order_rows(order, _all_line_items(client, url, order)))
            if not page["pageInfo"]["hasNextPage"]:
                break
            cursor = page["pageInfo"]["endCursor"]
    return pd.DataFrame(rows, columns=list(SHOPIFY_CSV_HEADERS))


def _all_line_items(client, url, order):
    connection = order["lineItems"]
    lines = list(connection["nodes"])
    info = connection["pageInfo"]
    while info["hasNextPage"]:
        connection = _post(
            client,
            url,
            _LINE_ITEMS_QUERY,
            {"id": order["id"], "cursor": info["endCursor"]},
        )["node"]["lineItems"]
        lines.extend(connection["nodes"])
        info = connection["pageInfo"]
    return lines


def _post(client, url, query, variables):
    """POST one GraphQL request, retrying on THROTTLED. Errors never include the token."""
    for attempt in range(MAX_THROTTLE_RETRIES + 1):
        try:
            response = client.post(url, json={"query": query, "variables": variables})
        except httpx.HTTPError as exc:
            raise OrderSourceError(
                f"Could not reach Shopify ({type(exc).__name__})."
            ) from None
        if response.status_code in (401, 403):
            raise OrderSourceError(
                f"Shopify rejected the access token (HTTP {response.status_code})."
            )
        if response.status_code != 200:
            raise OrderSourceError(f"Shopify returned HTTP {response.status_code}.")
        try:
            body = response.json()
        except ValueError:
            raise OrderSourceError(
                "Shopify returned a response that is not JSON."
            ) from None

        errors = body.get("errors") or []
        if not errors and body.get("data") is not None:
            return body["data"]
        if any(
            (error.get("extensions") or {}).get("code") == "THROTTLED"
            for error in errors
        ):
            if attempt < MAX_THROTTLE_RETRIES:
                _sleep(_seconds_to_restore(body.get("extensions")))
            continue
        messages = (
            "; ".join(str(error.get("message", "unknown error")) for error in errors)
            or "no data"
        )
        raise OrderSourceError(f"Shopify GraphQL error: {messages}")
    raise OrderSourceError("Shopify kept throttling the request; try again later.")


def _seconds_to_restore(extensions):
    """Time until the query cost fits the bucket again, at the reported restore rate."""
    cost = (extensions or {}).get("cost") or {}
    status = cost.get("throttleStatus") or {}
    deficit = cost.get("requestedQueryCost", 0) - status.get("currentlyAvailable", 0)
    # At least a second, so a throttle reply without cost data still backs off.
    return max(deficit / (status.get("restoreRate") or 1.0), 1.0)


def _order_rows(order, lines):
    address = order.get("shippingAddress") or {}
    shipping = order.get("shippingLine") or {}
    shared = {
        "Name": order["name"],
        "Shipping Method": shipping.get("title") or "",
        "Shipping Country": address.get("countryCodeV2") or "",
        "Tags": ", ".join(order.get("tags") or []),
        "Notes": order.get("note") or "",
        "Total": order["totalPriceSet"]["shopMoney"]["amount"],
        "Subtotal": order["subtotalPriceSet"]["shopMoney"]["amount"],
        "Shipping Name": address.get("name") or "",
        "Created at": order["createdAt"],
    }
    return [
        {
            **shared,
            "Lineitem sku": line.get("sku") or "",
            "Lineitem quantity": line["quantity"],
            "Lineitem name": line["name"],
        }
        for line in lines
    ]
