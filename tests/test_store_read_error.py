"""A failed Supabase read is a StoreError, never an empty listing.

Runs without the local Supabase stack: the client is a stub.
"""

import json

import httpx
import pytest
from storage3.exceptions import StorageApiError

from gui.session_browser_widget import SessionLoaderWorker
from shared.store import StoreError, StoreNotFound
from shared.store_supabase import SupabaseStore
from shopify_tool.profile_manager import ProfileManager
from shopify_tool.session_manager import SessionManager


class _Bucket:
    def download(self, rel):
        # Directory paths (Clients, Sessions) are asked of the bucket too; nothing is stored there.
        raise StorageApiError("Object not found", "not_found", 404)


class _UnreachableClient:
    """The database is down: every table call fails at the transport."""

    class storage:
        @staticmethod
        def from_(name):
            return _Bucket()

    def table(self, name):
        raise httpx.ConnectError("network down")


@pytest.fixture
def store(tmp_path):
    return SupabaseStore(_UnreachableClient(), pc="pc1", cache_dir=tmp_path)


@pytest.fixture
def profile_manager(store):
    return ProfileManager(store=store, require_connection=False)


def test_read_error_raises_not_empty(store, profile_manager):
    sessions = SessionManager(profile_manager, store)

    with pytest.raises(StoreError) as sessions_error:
        sessions.list_client_sessions("X")
    assert not isinstance(sessions_error.value, StoreNotFound)

    with pytest.raises(StoreError) as clients_error:
        profile_manager.list_clients()
    assert not isinstance(clients_error.value, StoreNotFound)


def test_a_cut_off_response_raises_not_empty(store, profile_manager, monkeypatch):
    def cut_off(name):
        raise json.JSONDecodeError("Unterminated string", '[{"client_id": "A', 15)

    monkeypatch.setattr(store.client, "table", cut_off)
    with pytest.raises(StoreError):
        profile_manager.list_clients()


def test_a_failed_session_load_reaches_the_page_as_an_error(store, profile_manager):
    worker = SessionLoaderWorker(SessionManager(profile_manager, store), "X")
    failures, loaded = [], []
    worker.error_occurred.connect(failures.append)
    worker.finished_with_data.connect(loaded.append)

    worker.run()

    assert loaded == []
    assert len(failures) == 1
