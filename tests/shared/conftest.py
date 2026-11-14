"""Fixtures for tests that run against the local Supabase stack (SUPABASE_TEST_URL set; CI and `supabase start`)."""
import os
import uuid

import pytest

_TABLES = [("clients", "client_id"), ("list_locks", "client_id"), ("groups", "id"), ("workers", "id"), ("stats", "id")]


def _need(var: str) -> str:
    val = os.environ.get(var)
    if not val:
        pytest.skip(f"{var} not set: no local Supabase stack")
    return val


@pytest.fixture(scope="session")
def supabase_admin():
    """Service-role client: creates users and wipes tables. Test-only; the app never has this key."""
    from supabase import create_client

    return create_client(_need("SUPABASE_TEST_URL"), _need("SUPABASE_TEST_SERVICE_KEY"))


@pytest.fixture(scope="session")
def supabase_user(supabase_admin):
    """(email, password) of a throwaway signed-up user."""
    email, password = f"t{uuid.uuid4().hex[:10]}@example.com", uuid.uuid4().hex
    supabase_admin.auth.admin.create_user({"email": email, "password": password, "email_confirm": True})
    return email, password


def make_user_client(email: str, password: str):
    from supabase import create_client

    c = create_client(_need("SUPABASE_TEST_URL"), _need("SUPABASE_TEST_ANON_KEY"))
    c.auth.sign_in_with_password({"email": email, "password": password})
    return c


def _wipe(admin):
    for table, col in _TABLES:
        admin.table(table).delete().neq(col, "" if col != "id" else 0).execute()
    bucket = admin.storage.from_("files")
    stack = [""]
    while stack:
        prefix = stack.pop()
        for item in bucket.list(prefix, {"limit": 1000}):
            path = f"{prefix}/{item['name']}".lstrip("/")
            if item.get("id") is None:
                stack.append(path)
            else:
                bucket.remove([path])


@pytest.fixture
def supabase_client(supabase_admin, supabase_user):
    """An authenticated user client over empty tables and an empty `files` bucket."""
    _wipe(supabase_admin)
    return make_user_client(*supabase_user)
