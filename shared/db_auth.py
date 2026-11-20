"""Sign in to Supabase, remembering only the refresh token (in the OS keyring)."""
import logging
from collections.abc import Callable

import httpx
import keyring
from keyring.errors import KeyringError, PasswordDeleteError
from supabase_auth.errors import AuthApiError, AuthError, AuthRetryableError

from shared import db_config
from shared.i18n import _
from supabase import ClientOptions, create_client

logger = logging.getLogger(__name__)

TIMEOUT_SECONDS = 5  # to connect: an unreachable project fails fast
TRANSFER_SECONDS = 60  # a large upload or history replace needs longer than that
_TOKEN_USER = "refresh_token"  # noqa: S105 - keyring entry name, not a secret
_UNREACHABLE = (httpx.HTTPError, AuthRetryableError)
_run_tokens: dict[str, str] = {}  # latest refresh token per service, for this run only


class SignInCancelled(Exception):
    """The user closed the sign-in dialog. Not "unreachable": the caller decides."""


def _make_client():
    http = httpx.Client(timeout=httpx.Timeout(TRANSFER_SECONDS, connect=TIMEOUT_SECONDS))
    return create_client(
        db_config.SUPABASE_URL, db_config.SUPABASE_PUBLISHABLE_KEY, ClientOptions(httpx_client=http)
    )


def _keyring_call(action: str, *args):
    """One keyring operation. A missing or failing keyring is logged, never raised."""
    try:
        return getattr(keyring, action)(*args)
    except PasswordDeleteError:  # nothing was saved: not a problem
        return None
    except KeyringError:  # includes NoKeyringError
        logger.warning("Keyring %s failed; the refresh token is not kept on this PC", action)
        return None


def _adopt(service: str, session, remembering: bool) -> None:
    """Every new session is the run's token; only a remembering run also writes it to the keyring."""
    _run_tokens[service] = session.refresh_token
    if remembering:
        _keyring_call("set_password", service, _TOKEN_USER, session.refresh_token)


def connect(app_name: str, prompt: Callable[[str | None], tuple | None], factory=_make_client):
    """A signed-in client; None only when the project is unreachable.

    prompt returns (email, password, remember) or a 2-tuple, which means remember=False.
    Raises SignInCancelled when the user cancels. prompt(error) keeps being
    asked until success, cancel, or unreachable; error says why it asks again.
    """
    service = f"{app_name}-db"
    client = factory()
    remembering = False
    # The client refreshes the access token in the background, and each refresh
    # rotates the refresh token: keep the run's copy current, and the keyring's when remembering.
    def on_state(event, session):
        if event == "TOKEN_REFRESHED" and session:
            _adopt(service, session, remembering)

    client.auth.on_auth_state_change(on_state)
    try:
        # A keyring token exists only for a remembering run, so it tells us which kind this run is.
        saved = _keyring_call("get_password", service, _TOKEN_USER)
        token = _run_tokens.get(service) or saved
        remembering = bool(saved)
        error = None
        if token:
            try:
                res = client.auth.refresh_session(token)
                _adopt(service, res.session, remembering)  # refresh tokens rotate
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
            email, password, *rest = creds
            remember = bool(rest and rest[0])
            try:
                res = client.auth.sign_in_with_password({"email": email, "password": password})
            except _UNREACHABLE:
                return None
            except AuthApiError:
                error = _("Wrong email or password. Try again.")
                continue  # ask again
            remembering = remember
            _adopt(service, res.session, remember)
            if not remember:
                _keyring_call("delete_password", service, _TOKEN_USER)
            return client
    except _UNREACHABLE:
        return None


def forget(app_name: str) -> None:
    """Sign this PC out: drop the keyring token and the run's token. Never raises."""
    service = f"{app_name}-db"
    _run_tokens.pop(service, None)
    _keyring_call("delete_password", service, _TOKEN_USER)
