import json

import pytest

from shared.store import APPLIED_OPS, APPLIED_OPS_KEEP, Delta, StoreError, delta


@delta("test.add")
def _add(current, n):
    return {"n": (current or {}).get("n", 0) + n}


def test_delta_without_id_just_applies():
    result = Delta("test.add", {"n": 2})({"n": 1})

    assert result == {"n": 3}
    assert APPLIED_OPS not in result


def test_delta_with_id_applies_once():
    d = Delta("test.add", {"n": 2}, op_id="a1")

    once = d({"n": 1})

    assert once["n"] == 3
    assert once[APPLIED_OPS] == ["a1"]
    assert d(once)["n"] == 3


def test_applied_ops_ring_keeps_last_200():
    doc = {"n": 0, APPLIED_OPS: [str(i) for i in range(APPLIED_OPS_KEEP)]}

    result = Delta("test.add", {"n": 1}, op_id="new")(doc)

    ids = result[APPLIED_OPS]
    assert len(ids) == APPLIED_OPS_KEEP
    assert ids[-1] == "new"
    assert "0" not in ids


def test_round_trip_through_json():
    d = Delta("test.add", {"n": 2}, op_id="a1")

    restored = Delta.from_op(json.loads(json.dumps(d.to_op())))

    assert restored(None) == d(None)


def test_unknown_kind_raises_store_error():
    with pytest.raises(StoreError):
        Delta.from_op({"kind": "nope", "args": {}, "op_id": "x"})(None)
