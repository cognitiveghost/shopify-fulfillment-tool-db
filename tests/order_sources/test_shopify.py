"""Shopify order fetcher, driven by httpx.MockTransport serving anonymised GraphQL pages."""

import json
from pathlib import Path

import httpx
import pandas as pd
import pytest

from shopify_tool import core
from shopify_tool.order_sources import (
    SHOPIFY_CSV_HEADERS,
    OrderSourceError,
    fetch_orders,
    shopify,
)
from shopify_tool.order_sources.shopify import fetch_shopify_orders

FIXTURES = Path(__file__).parent / "fixtures"
SHOP = "testa-probova.myshopify.com"
TOKEN = "shpat_FAKE_TOKEN_0123456789"
ORDER_2001 = "gid://shopify/Order/2001"


def _fixture(name):
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def _serve(order_pages, line_pages=None, seen=None):
    """Fake Admin GraphQL: order pages keyed by cursor (None = first page), line pages by (order id, cursor)."""
    line_pages = line_pages or {}

    def handler(request):
        variables = json.loads(request.content)["variables"]
        if seen is not None:
            seen.append(variables)
        if "id" in variables:
            return httpx.Response(
                200, json=line_pages[(variables["id"], variables["cursor"])]
            )
        return httpx.Response(200, json=order_pages[variables["cursor"]])

    return httpx.MockTransport(handler)


def _two_order_pages(seen=None):
    return _serve(
        {
            None: _fixture("orders_page_1.json"),
            "orders-cursor-1": _fixture("orders_page_2.json"),
        },
        seen=seen,
    )


def _fetch(transport):
    return fetch_shopify_orders(SHOP, TOKEN, transport=transport)


class TestFetchShopifyOrders:
    def test_headers_exact(self):
        df = _fetch(_two_order_pages())

        assert SHOPIFY_CSV_HEADERS == (
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
        assert list(df.columns) == list(SHOPIFY_CSV_HEADERS)

    def test_one_row_per_line_item(self):
        df = _fetch(_two_order_pages())

        assert df["Name"].tolist() == ["#1001", "#1001", "#1002", "#1003"]
        assert df["Lineitem name"].tolist() == [
            "Alpha Cream 50ml",
            "Beta Serum 30ml",
            "Gamma Gift Card",
            "Delta Oil 100ml",
        ]
        assert df["Lineitem quantity"].tolist() == [1, 2, 1, 3]

    def test_order_fields_repeat_on_every_line(self):
        df = _fetch(_two_order_pages())

        first, second = df.iloc[0], df.iloc[1]
        for row in (first, second):
            assert row["Shipping Method"] == "Standard 5-10 business days"
            assert row["Shipping Country"] == "DK"
            assert row["Tags"] == "express, gift"
            assert row["Notes"] == "Testa note one"
            assert row["Total"] == "82.00"
            assert row["Subtotal"] == "72.00"
            assert row["Shipping Name"] == "Testa Probova"
            assert row["Created at"] == "2026-11-01T09:00:00Z"

    def test_missing_order_fields_become_empty_strings(self):
        df = _fetch(_two_order_pages())

        gift_card = df.iloc[2]
        assert gift_card["Shipping Method"] == ""
        assert gift_card["Shipping Country"] == ""
        assert gift_card["Shipping Name"] == ""
        assert gift_card["Notes"] == ""
        assert gift_card["Tags"] == ""

    def test_order_pagination(self):
        seen = []
        df = _fetch(_two_order_pages(seen))

        assert [variables["cursor"] for variables in seen] == [None, "orders-cursor-1"]
        assert set(df["Name"]) == {"#1001", "#1002", "#1003"}

    def test_line_item_pagination(self):
        lines = [
            {"sku": f"SKU-{i:03d}", "name": f"Line {i:03d}", "quantity": 1}
            for i in range(1, 121)
        ]
        order = {
            "id": ORDER_2001,
            "name": "#2001",
            "createdAt": "2026-11-03T07:00:00Z",
            "note": None,
            "tags": [],
            "subtotalPriceSet": {"shopMoney": {"amount": "600.00"}},
            "totalPriceSet": {"shopMoney": {"amount": "600.00"}},
            "shippingLine": None,
            "shippingAddress": None,
            "lineItems": {
                "pageInfo": {"hasNextPage": True, "endCursor": "li-50"},
                "nodes": lines[:50],
            },
        }
        orders_page = {
            "data": {
                "orders": {
                    "pageInfo": {"hasNextPage": False, "endCursor": None},
                    "nodes": [order],
                }
            }
        }

        def line_page(nodes, has_next, end_cursor):
            return {
                "data": {
                    "node": {
                        "lineItems": {
                            "pageInfo": {
                                "hasNextPage": has_next,
                                "endCursor": end_cursor,
                            },
                            "nodes": nodes,
                        }
                    }
                }
            }

        line_pages = {
            (ORDER_2001, "li-50"): line_page(lines[50:100], True, "li-100"),
            (ORDER_2001, "li-100"): line_page(lines[100:], False, None),
        }
        df = _fetch(_serve({None: orders_page}, line_pages))

        assert len(df) == 120
        assert df["Lineitem sku"].tolist() == [line["sku"] for line in lines]

    def test_missing_sku_kept(self):
        df = _fetch(_two_order_pages())

        assert df["Lineitem sku"].tolist() == [
            "SKU-ALPHA-1",
            "SKU-BETA-2",
            "",
            "SKU-DELTA-4",
        ]
        assert isinstance(df.iloc[2]["Lineitem sku"], str)

    def test_throttled_retries(self, monkeypatch):
        sleeps = []
        monkeypatch.setattr(shopify, "_sleep", sleeps.append)
        responses = iter(
            [
                _fixture("throttled.json"),
                _fixture("orders_page_1.json"),
                _fixture("orders_page_2.json"),
            ]
        )
        transport = httpx.MockTransport(
            lambda request: httpx.Response(200, json=next(responses))
        )

        df = _fetch(transport)

        # (requestedQueryCost 1000 - currentlyAvailable 100) / restoreRate 50
        assert sleeps == [18.0]
        assert len(df) == 4

    def test_gives_up_after_five_throttled_retries(self, monkeypatch):
        sleeps = []
        monkeypatch.setattr(shopify, "_sleep", sleeps.append)
        transport = httpx.MockTransport(
            lambda request: httpx.Response(200, json=_fixture("throttled.json"))
        )

        with pytest.raises(OrderSourceError):
            _fetch(transport)
        assert len(sleeps) == 5

    def test_dataframe_passes_validation(self):
        df = _fetch(_two_order_pages())
        stock_df = pd.DataFrame({"SKU": ["SKU-ALPHA-1"], "Stock": [5]})
        config = {
            "column_mappings": {
                "orders": {
                    "Name": "Order_Number",
                    "Lineitem sku": "SKU",
                    "Lineitem quantity": "Quantity",
                    "Lineitem name": "Product_Name",
                    "Shipping Method": "Shipping_Method",
                    "Shipping Country": "Shipping_Country",
                    "Tags": "Tags",
                    "Notes": "Notes",
                    "Total": "Total_Price",
                    "Subtotal": "Subtotal",
                    "Shipping Name": "Customer",
                    "Created at": "Created_At",
                },
                "stock": {"SKU": "SKU", "Stock": "Stock"},
            }
        }

        assert core._validate_dataframes(df, stock_df, config) == []


def _unauthorised(request):
    return httpx.Response(
        401, json={"errors": "[API] Invalid API key or access token."}
    )


def _graphql_denied(request):
    return httpx.Response(
        200, json={"errors": [{"message": "Access denied for orders field."}]}
    )


def _unreachable(request):
    raise httpx.ConnectError(f"connection refused while sending {TOKEN}")


@pytest.mark.parametrize("handler", [_unauthorised, _graphql_denied, _unreachable])
def test_errors_never_carry_the_token(handler):
    with pytest.raises(OrderSourceError) as excinfo:
        _fetch(httpx.MockTransport(handler))

    assert TOKEN not in str(excinfo.value)


def test_fetch_orders_none_errors():
    with pytest.raises(OrderSourceError):
        fetch_orders(None)


def test_fetch_orders_unknown_platform_errors():
    with pytest.raises(OrderSourceError):
        fetch_orders({"platform": "woocommerce", "shop": SHOP, "token": TOKEN})


def test_fetch_orders_shopify_without_token_errors():
    with pytest.raises(OrderSourceError):
        fetch_orders({"platform": "shopify", "shop": SHOP})


def test_fetch_orders_shopify_dispatches_with_shop_and_token(monkeypatch):
    calls = []
    sentinel = pd.DataFrame(columns=list(SHOPIFY_CSV_HEADERS))
    monkeypatch.setattr(
        shopify,
        "fetch_shopify_orders",
        lambda shop, token: calls.append((shop, token)) or sentinel,
    )

    result = fetch_orders({"platform": "shopify", "shop": SHOP, "token": TOKEN})

    assert calls == [(SHOP, TOKEN)]
    assert result is sentinel


def test_a_throttle_without_cost_data_still_waits():
    from shopify_tool.order_sources.shopify import _seconds_to_restore

    assert _seconds_to_restore(None) == 1.0
