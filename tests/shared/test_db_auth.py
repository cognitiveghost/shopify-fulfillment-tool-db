import time
from types import SimpleNamespace

import httpx
import keyring
import pytest
from keyring.backend import KeyringBackend
from supabase_auth.errors import AuthApiError

from shared import db_auth


class MemKeyring(KeyringBackend):
    priority = 1

    def __init__(self):
        self.d = {}

    def get_password(self, service, user):
        return self.d.get((service, user))

    def set_password(self, service, user, pw):
        self.d[(service, user)] = pw

    def delete_password(self, service, user):
        self.d.pop((service, user), None)


@pytest.fixture
def kr():
    old = keyring.get_keyring()
    mem = MemKeyring()
    keyring.set_keyring(mem)
    yield mem
    keyring.set_keyring(old)


def _session(token):
    return SimpleNamespace(session=SimpleNamespace(refresh_token=token))


class FakeAuth:
    def __init__(self, *, refresh=None, sign_in=None):
        self.refresh, self.sign_in, self.calls = refresh, sign_in, []
        self.listeners = []

    def on_auth_state_change(self, callback):
        self.listeners.append(callback)

    def refresh_session(self, token):
        self.calls.append(("refresh", token))
        return self.refresh(token)

    def sign_in_with_password(self, creds):
        self.calls.append(("sign_in", creds["email"]))
        return self.sign_in(creds)


def _factory(auth):
    client = SimpleNamespace(auth=auth)
    return lambda: client


def test_stored_token_means_no_prompt(kr):
    kr.d[("App-db", "refresh_token")] = "old"
    auth = FakeAuth(refresh=lambda t: _session("rotated"))
    prompts = []
    client = db_auth.connect("App", lambda: prompts.append(1), _factory(auth))
    assert client is not None and prompts == []
    assert kr.d[("App-db", "refresh_token")] == "rotated"


def test_background_refresh_saves_the_rotated_token(kr):
    auth = FakeAuth(sign_in=lambda c: _session("first"))
    assert db_auth.connect("App", lambda: ("a@b.c", "pw"), _factory(auth)) is not None
    for listener in auth.listeners:
        listener("TOKEN_REFRESHED", SimpleNamespace(refresh_token="second"))
    assert kr.d[("App-db", "refresh_token")] == "second"


def test_no_token_prompts_once_and_saves(kr):
    auth = FakeAuth(sign_in=lambda c: _session("fresh"))
    prompts = []

    def prompt():
        prompts.append(1)
        return "a@b.c", "pw"

    assert db_auth.connect("App", prompt, _factory(auth)) is not None
    assert len(prompts) == 1 and kr.d[("App-db", "refresh_token")] == "fresh"


def test_cancel_returns_none(kr):
    assert db_auth.connect("App", lambda: None, _factory(FakeAuth())) is None
    assert ("App-db", "refresh_token") not in kr.d


def test_refused_saved_token_falls_back_to_prompt(kr):
    kr.d[("App-db", "refresh_token")] = "revoked"

    def refuse(_):
        raise AuthApiError("bad token", 400, "refresh_token_not_found")

    auth = FakeAuth(refresh=refuse, sign_in=lambda c: _session("new"))
    assert db_auth.connect("App", lambda: ("a@b.c", "pw"), _factory(auth)) is not None
    assert kr.d[("App-db", "refresh_token")] == "new"


def test_wrong_password_asks_again(kr):
    answers = iter([("a@b.c", "bad"), ("a@b.c", "good")])

    def sign_in(c):
        if c["password"] == "bad":
            raise AuthApiError("Invalid login", 400, "invalid_credentials")
        return _session("t")

    assert db_auth.connect("App", lambda: next(answers), _factory(FakeAuth(sign_in=sign_in))) is not None


def test_unreachable_returns_none_quickly(kr):
    def down(_):
        raise httpx.ConnectError("no route")

    start = time.monotonic()
    assert db_auth.connect("App", lambda: ("a@b.c", "pw"), _factory(FakeAuth(sign_in=down))) is None
    kr.d[("App-db", "refresh_token")] = "t"
    assert db_auth.connect("App", lambda: None, _factory(FakeAuth(refresh=down))) is None
    assert time.monotonic() - start < 5
    assert ("App-db", "refresh_token") in kr.d  # an outage must not forget the sign-in


def test_http_client_has_five_second_timeout():
    assert db_auth.TIMEOUT_SECONDS == 5
