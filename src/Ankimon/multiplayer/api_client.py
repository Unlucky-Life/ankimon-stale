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
    """Raised when credentials are missing or rejected (401)."""


class MultiplayerConflictError(MultiplayerApiError):
    """Raised on 409 - e.g. joining a raid that has already started."""


def _error_message(response, fallback: str) -> str:
    """Prefer the server's own {"error": ...} text over a status-code string."""
    try:
        body = response.json()
    except ValueError:
        return fallback
    if isinstance(body, dict):
        message = body.get("error") or body.get("message")
        if isinstance(message, str) and message.strip():
            return message.strip()
    return fallback


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


def save_credentials(username: str, api_key: str) -> None:
    """Persist multiplayer credentials in the existing leaderboard file."""
    credentials = {"username": username, "api_key": api_key}
    with open(user_path_credentials, "w", encoding="utf-8") as f:
        json.dump(credentials, f, indent=2)


class MultiplayerApiClient:
    def __init__(self, settings_obj):
        self.settings = settings_obj
        self.session = requests.Session()

    @property
    def base_url(self) -> str:
        url = self.settings.get("multiplayer.api_url", DEFAULT_API_URL) or DEFAULT_API_URL
        return f"{url.rstrip('/')}/{API_VERSION}"

    @property
    def root_url(self) -> str:
        url = self.settings.get("multiplayer.api_url", DEFAULT_API_URL) or DEFAULT_API_URL
        return url.rstrip("/")

    def _request_no_auth(self, method: str, path: str, payload: Optional[dict] = None) -> dict:
        try:
            response = self.session.request(
                method,
                f"{self.root_url}{path}",
                json=payload,
                timeout=(CONNECT_TIMEOUT, READ_TIMEOUT),
            )
        except requests.exceptions.RequestException as e:
            raise MultiplayerApiError(f"Request failed: {e}") from e

        if response.status_code >= 400:
            raise MultiplayerApiError(
                f"{method} {path} failed with status {response.status_code}"
            )
        content_type = response.headers.get("Content-Type", "")
        if "application/json" not in content_type:
            return {"ok": True, "body": response.text.strip()}
        try:
            return response.json() if response.content else {}
        except ValueError as e:
            raise MultiplayerApiError("Server returned invalid JSON.") from e

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

        if response.status_code == 401:
            raise MultiplayerAuthError("Multiplayer credentials were rejected.")
        if response.status_code == 403:
            # Not a credential problem: the server refused this action (e.g.
            # human-vs-human battles are gated off). Treating it as an auth
            # failure would disable multiplayer over a feature flag.
            raise MultiplayerApiError(
                _error_message(response, "The server refused that action.")
            )
        if response.status_code == 409:
            raise MultiplayerConflictError(
                _error_message(
                    response, f"{method} {path} conflicted with server state (409)."
                )
            )
        if response.status_code >= 400:
            raise MultiplayerApiError(
                _error_message(
                    response,
                    f"{method} {path} failed with status {response.status_code}",
                )
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

    def check_health(self) -> dict:
        """Check server reachability without requiring credentials."""
        return self._request_no_auth("GET", "/healthz")

    def create_guest(self) -> dict:
        payload = self._request_no_auth("POST", f"/{API_VERSION}/guests")
        if payload.get("username") and payload.get("api_key"):
            save_credentials(payload["username"], payload["api_key"])
        return payload

    def setup_demo(self) -> dict:
        return self._request("POST", "/demo/setup")

    # --- Raids ------------------------------------------------------------

    def create_raid(
        self,
        target_days: int = 5,
        visibility: str = "public",
        bots: int = 0,
    ) -> dict:
        return self._request(
            "POST",
            "/raids",
            payload={
                "target_days": target_days,
                "visibility": visibility,
                "bots": bots,
            },
        )

    def join_raid(self, raid_code: str) -> dict:
        return self._request("POST", f"/raids/{raid_code}/join")

    def leave_raid(self, raid_code: str) -> dict:
        return self._request("POST", f"/raids/{raid_code}/leave")

    def start_raid(self, raid_code: str) -> dict:
        """Lock the raid (owner only) - no one else can join afterwards."""
        return self._request("POST", f"/raids/{raid_code}/start")

    # --- Friends ------------------------------------------------------------

    def add_friend(self, username: str) -> dict:
        return self._request("POST", "/friends", payload={"username": username})

    def respond_to_friend_request(self, username: str, accept: bool) -> dict:
        return self._request(
            "POST", f"/friends/{username}/respond", payload={"accept": accept}
        )

    def remove_friend(self, username: str) -> dict:
        return self._request("DELETE", f"/friends/{username}")

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
