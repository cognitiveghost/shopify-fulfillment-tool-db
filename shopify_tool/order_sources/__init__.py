"""Order sources: pull a store's open orders as rows shaped like a Shopify CSV export.

Every source returns the raw Shopify CSV headers, so `_validate_dataframes`, the column
mapping and `analyse` run unchanged on the result.
"""

import pandas as pd

SHOPIFY_CSV_HEADERS = (
    "Name",
    "Lineitem sku",
    "Lineitem quantity",
    "Lineitem name",
    "Shipping Method",
    "Shipping Country",
    "Tags",
    "Notes",
    "Total",
    "Subtotal",
    "Shipping Name",
    "Created at",
)


class OrderSourceError(Exception):
    """A store could not be read. Messages never carry credentials."""


def fetch_orders(order_source: dict | None) -> pd.DataFrame:
    """Fetch orders from the store described by a client's `order_source` settings."""
    if not isinstance(order_source, dict):
        raise OrderSourceError("No order source is configured for this client.")
    platform = order_source.get("platform")
    if platform == "shopify":
        shop = order_source.get("shop")
        token = order_source.get("token")
        if not shop or not token:
            raise OrderSourceError(
                "The Shopify order source needs a shop and an access token."
            )
        # Imported here: shopify.py imports this module for the headers and the error class.
        from shopify_tool.order_sources.shopify import fetch_shopify_orders

        return fetch_shopify_orders(shop, token)
    raise OrderSourceError(f"Unknown order source platform: {platform!r}")
