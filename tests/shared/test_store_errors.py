import json

import httpx
import pytest
from postgrest.exceptions import APIError, generate_default_error_message
from storage3.exceptions import StorageApiError, StorageException

from shared.store import StoreError, StoreUnreachable
from shared.store_supabase import _guard, _is_unreachable


def _gateway_api_error(status: int) -> APIError:
    """The APIError postgrest raises for a non-JSON error body, such as a proxy's HTML page."""
    return APIError(generate_default_error_message(httpx.Response(status, text="<html>gateway</html>")))


def _api_error(code: str) -> APIError:
    return APIError({"code": code, "message": "permission denied"})


class _Dummy:
    def __init__(self, exc: BaseException):
        self.exc = exc

    @_guard
    def read(self, rel):
        raise self.exc


def test_store_unreachable_is_a_store_error():
    assert issubclass(StoreUnreachable, StoreError)


def test_transport_errors_are_unreachable():
    for exc in (httpx.ConnectError("x"), httpx.ReadTimeout("x"), httpx.RemoteProtocolError("x"),
                OSError(), json.JSONDecodeError("x", "", 0)):
        assert _is_unreachable(exc), exc


def test_server_5xx_is_unreachable():
    assert _is_unreachable(_gateway_api_error(503))
    assert _is_unreachable(StorageApiError("bad gateway", "InternalError", 502))
    assert _is_unreachable(StorageException({"statusCode": 502}))


def test_permanent_errors_are_not_unreachable():
    assert not _is_unreachable(_api_error("42501"))
    assert not _is_unreachable(_gateway_api_error(400))
    assert not _is_unreachable(StorageException({"statusCode": 404}))
    assert not _is_unreachable(ValueError())
    assert not _is_unreachable(StoreError("x"))


def test_guard_raises_unreachable_for_network_and_plain_error_otherwise():
    network = httpx.ConnectError("x")
    with pytest.raises(StoreUnreachable) as e:
        _Dummy(network).read("a.json")
    assert e.value.__cause__ is network

    with pytest.raises(StoreError) as e:
        _Dummy(_api_error("42501")).read("a.json")
    assert not isinstance(e.value, StoreUnreachable)
