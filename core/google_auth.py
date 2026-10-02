"""OAuth credentials for the user's own Google Workspace APIs."""

from __future__ import annotations

import logging
import os
from pathlib import Path

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow

logger = logging.getLogger("vibe-daemon")

SCOPES = [
    "https://www.googleapis.com/auth/calendar",
    "https://www.googleapis.com/auth/gmail.modify",
    "https://www.googleapis.com/auth/drive",
    "https://www.googleapis.com/auth/documents",
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/youtube",
]

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
CREDENTIALS_PATH = _PROJECT_ROOT / "credentials.json"
TOKEN_PATH = Path(os.path.expanduser("~/.vibe_daemon_token.json"))


def get_google_credentials() -> Credentials:
    """Return a valid user credential, refreshing or running OAuth as needed.

    Client secrets are read from ``credentials.json`` in the project root.
    The authorized-user token is stored at ``~/.vibe_daemon_token.json``.
    """
    creds = _load_token()
    if creds and not _has_required_scopes(creds):
        logger.info("Stored Google token is missing required scopes; re-authenticating")
        creds = None

    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            logger.info("Refreshing expired Google token")
            try:
                creds.refresh(Request())
            except Exception:
                logger.exception("Google token refresh failed; starting OAuth")
                creds = _run_installed_app_flow()
        else:
            creds = _run_installed_app_flow()
        _save_token(creds)

    return creds


def _load_token() -> Credentials | None:
    if not TOKEN_PATH.is_file():
        return None
    try:
        return Credentials.from_authorized_user_file(str(TOKEN_PATH), SCOPES)
    except Exception:
        logger.exception("Could not load Google token from %s", TOKEN_PATH)
        return None


def _has_required_scopes(creds: Credentials) -> bool:
    granted = set(creds.scopes or [])
    if not granted:
        return True
    return set(SCOPES).issubset(granted)


def _run_installed_app_flow() -> Credentials:
    if not CREDENTIALS_PATH.is_file():
        raise FileNotFoundError(
            f"Google OAuth client secrets not found at {CREDENTIALS_PATH}. "
            "Download credentials.json from Google Cloud Console into the project root."
        )
    flow = InstalledAppFlow.from_client_secrets_file(str(CREDENTIALS_PATH), SCOPES)
    return flow.run_local_server(port=0)


def _save_token(creds: Credentials) -> None:
    TOKEN_PATH.write_text(creds.to_json(), encoding="utf-8")
    try:
        TOKEN_PATH.chmod(0o600)
    except OSError:
        logger.warning("Could not restrict permissions on %s", TOKEN_PATH)
    logger.info("Saved Google token to %s", TOKEN_PATH)
