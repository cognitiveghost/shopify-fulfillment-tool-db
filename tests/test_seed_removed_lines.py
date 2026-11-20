from scripts.seed_removed_lines import pick


def _order(n: int, quantities: list[int], tags: list[str] | None = None) -> dict:
    lines = [{"id": f"L{n}-{i}", "currentQuantity": q} for i, q in enumerate(quantities)]
    return {"id": f"O{n}", "name": f"#{n}", "tags": tags or [], "lineItems": {"nodes": lines}}


def test_pick_skips_single_line_and_touched_orders_and_fills_each_kind():
    orders = [
        _order(1, [1]),  # one line: removing it would empty the order
        _order(2, [1, 1], ["seed-refunded-line"]),  # an earlier run already did this one
        _order(3, [1, 0]),  # only one live line
        _order(4, [2, 1]),
        _order(5, [1, 1]),
        _order(6, [1, 1]),
        _order(7, [1, 1]),
    ]

    plan = [(tag, order["name"]) for tag, order in pick(orders, edits=1, refunds=1, partial=1)]

    assert plan == [("seed-partial-refund", "#4"), ("seed-refunded-line", "#5"), ("seed-removed-line", "#6")]


def test_pick_partial_needs_quantity_two():
    assert pick([_order(1, [1, 1])], edits=0, refunds=0, partial=1) == []
