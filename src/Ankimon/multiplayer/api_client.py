"""Blocking HTTP client for the Ankimon multiplayer Go API.

Every method here performs a plain, short-lived request/response call and
raises on failure. Nothing in this module touches Qt or the Anki main
thread — callers (the MultiplayerController) are responsible for running
these methods in the background via mw.taskman.
"""

import json
import uuid
from typing import Optional

import requests

from ..resources import user_path_credentials

DEFAULT_API_URL = "https://multiplayer-api.ankimon.com"
API_VERSION = "v1"

CONNECT_TIMEOUT = 2
READ_TIMEOUT = 5


class MultiplayerApiError(Exception):
    """Raised for any transport or server-side error."""


class MultiplayerAuthError(MultiplayerApiError):
    """Raised when credentials are missing or rejected (401/403)."""


def load_credentials() -> Optional[dict]:
    """Return {"username": ..., "api_key": ...} or None if not configured.

    Reuses the leaderboard credentials file so players sign in once.
    """
    try:
        with open(user_path_credentials, "r", encoding="utf-8") as f:
            credentials = json.load(f)
    except (OSError, json.JSONDecodeError):
        return None
    if credentials.get("username") and credentials.get("api_key"):
        return credentials
    return None


class MultiplayerApiClient:
    def __init__(self, settings_obj):
        self.settings = settings_obj
        self.session = requests.Session()

    @property
    def base_url(self) -> str:
        url = self.settings.get("multiplayer.api_url", DEFAULT_API_URL) or DEFAULT_API_URL
        return f"{url.rstrip('/')}/{API_VERSION}"

    def _request(self, method: str, path: str, payload: Optional[dict] = None,
                 idempotency_key: Optional[str] = None) -> dict:
        credentials = load_credentials()
        if credentials is None:
            raise MultiplayerAuthError("No multiplayer credentials configured.")

        headers = {
            "Authorization": f"Bearer {credentials['api_key']}",
            "X-Ankimon-Username": credentials["username"],
        }
        if idempotency_key:
            headers["Idempotency-Key"] = idempotency_key

        try:
            response = self.session.request(
                method,
                f"{self.base_url}{path}",
                json=payload,
                headers=headers,
                timeout=(CONNECT_TIMEOUT, READ_TIMEOUT),
            )
        except requests.exceptions.RequestException as e:
            raise MultiplayerApiError(f"Request failed: {e}") from e

        if response.status_code in (401, 403):
            raise MultiplayerAuthError("Multiplayer credentials were rejected.")
        if response.status_code >= 400:
            raise MultiplayerApiError(
                f"{method} {path} failed with status {response.status_code}"
            )
        try:
            return response.json() if response.content else {}
        except ValueError as e:
            raise MultiplayerApiError("Server returned invalid JSON.") from e

    # --- Event ingest -----------------------------------------------------

    def post_events(self, events: list) -> dict:
        """Send a batch of review events; the response embeds fresh state.

        Events carry stable UUIDs so the server can deduplicate retries.
        """
        return self._request(
            "POST",
            "/events:batch",
            payload={"events": events},
            idempotency_key=str(uuid.uuid4()),
        )

    def get_state(self) -> dict:
        """Fetch the caller's multiplayer state (raid + matches) directly."""
        return self._request("GET", "/state")

    # --- Raids ------------------------------------------------------------

    def create_raid(self, target_days: int = 5) -> dict:
        return self._request("POST", "/raids", payload={"target_days": target_days})

    def join_raid(self, raid_code: str) -> dict:
        return self._request("POST", f"/raids/{raid_code}/join")

    def leave_raid(self, raid_code: str) -> dict:
        return self._request("POST", f"/raids/{raid_code}/leave")

    # --- Friend battles ---------------------------------------------------

    def challenge_friend(self, opponent_username: str) -> dict:
        return self._request(
            "POST", "/matches", payload={"opponent": opponent_username}
        )

    def respond_to_challenge(self, match_id: str, accept: bool) -> dict:
        return self._request(
            "POST", f"/matches/{match_id}/respond", payload={"accept": accept}
        )

    def submit_turn(self, match_id: str, move: str) -> dict:
        return self._request(
            "POST",
            f"/matches/{match_id}/turns",
            payload={"move": move},
            idempotency_key=str(uuid.uuid4()),
        )
