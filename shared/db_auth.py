"""Sign in to Supabase, remembering only the refresh token (in the OS keyring)."""
import logging
from collections.abc import Callable

import httpx
import keyring
from keyring.errors import KeyringError
from supabase_auth.errors import AuthApiError, AuthError, AuthRetryableError

from shared import db_config
from shared.i18n import _
from supabase import ClientOptions, create_client

logger = logging.getLogger(__name__)

TIMEOUT_SECONDS = 5  # to connect: an unreachable project fails fast
TRANSFER_SECONDS = 60  # a large upload or history replace needs longer than that
_TOKEN_USER = "refresh_token"
_UNREACHABLE = (httpx.HTTPError, AuthRetryableError)


class SignInCancelled(Exception):
    """The user closed the sign-in dialog. Not "unreachable": the caller decides."""


def _make_client():
    http = httpx.Client(timeout=httpx.Timeout(TRANSFER_SECONDS, connect=TIMEOUT_SECONDS))
    return create_client(
        db_config.SUPABASE_URL, db_config.SUPABASE_PUBLISHABLE_KEY, ClientOptions(httpx_client=http)
    )


def _remember(service: str, session) -> None:
    try:
        keyring.set_password(service, _TOKEN_USER, session.refresh_token)
    except KeyringError:
        logger.warning("No keyring available: the refresh token is not saved")


def connect(app_name: str, prompt: Callable[[str | None], tuple[str, str] | None], factory=_make_client):
    """A signed-in client; None only when the project is unreachable.

    Raises SignInCancelled when the user cancels. prompt(error) keeps being
    asked until success, cancel, or unreachable; error says why it asks again.
    """
    service = f"{app_name}-db"
    client = factory()
    # The client refreshes the access token in the background, and each refresh
    # rotates the refresh token: keep the keyring's copy current or the next start fails.
    client.auth.on_auth_state_change(
        lambda event, session: _remember(service, session) if event == "TOKEN_REFRESHED" and session else None
    )
    try:
        try:
            token = keyring.get_password(service, _TOKEN_USER)
        except KeyringError:
            token = None
        error = None
        if token:
            try:
                res = client.auth.refresh_session(token)
                _remember(service, res.session)  # refresh tokens rotate
                return client
            except _UNREACHABLE:
                return None
            except AuthError:
                logger.info("Saved sign-in was refused; asking again")
                error = _("Your saved sign-in has expired. Sign in again.")
        while True:
            creds = prompt(error)
            if creds is None:
                raise SignInCancelled()
            try:
                res = client.auth.sign_in_with_password({"email": creds[0], "password": creds[1]})
            except _UNREACHABLE:
                return None
            except AuthApiError:
                error = _("Wrong email or password. Try again.")
                continue  # ask again
            _remember(service, res.session)
            return client
    except _UNREACHABLE:
        return None
