"""Sign in to Supabase, remembering only the refresh token (in the OS keyring)."""
import logging
from collections.abc import Callable

import httpx
import keyring
from keyring.errors import KeyringError
from supabase_auth.errors import AuthApiError, AuthError, AuthRetryableError

from shared import db_config
from supabase import ClientOptions, create_client

logger = logging.getLogger(__name__)

TIMEOUT_SECONDS = 5
PROMPT_ATTEMPTS = 5
_TOKEN_USER = "refresh_token"
_UNREACHABLE = (httpx.HTTPError, AuthRetryableError)


def _make_client():
    http = httpx.Client(timeout=TIMEOUT_SECONDS)
    return create_client(
        db_config.SUPABASE_URL, db_config.SUPABASE_PUBLISHABLE_KEY, ClientOptions(httpx_client=http)
    )


def _remember(service: str, session) -> None:
    try:
        keyring.set_password(service, _TOKEN_USER, session.refresh_token)
    except KeyringError:
        logger.warning("No keyring available: the refresh token is not saved")


def connect(app_name: str, prompt: Callable[[], tuple[str, str] | None], factory=_make_client):
    """A signed-in client, or None (user cancelled, or the project is unreachable)."""
    service = f"{app_name}-db"
    client = factory()
    try:
        try:
            token = keyring.get_password(service, _TOKEN_USER)
        except KeyringError:
            token = None
        if token:
            try:
                res = client.auth.refresh_session(token)
                _remember(service, res.session)  # refresh tokens rotate
                return client
            except _UNREACHABLE:
                return None
            except AuthError:
                logger.info("Saved sign-in was refused; asking again")
        for _ in range(PROMPT_ATTEMPTS):
            creds = prompt()
            if creds is None:
                return None
            try:
                res = client.auth.sign_in_with_password({"email": creds[0], "password": creds[1]})
            except _UNREACHABLE:
                return None
            except AuthApiError:
                continue  # wrong email or password: ask again
            _remember(service, res.session)
            return client
    except _UNREACHABLE:
        return None
    return None
