"""Ankimon multiplayer: co-op raid bosses and friend battles.

Architecture (see docs/multiplayer-go-api-design.md):

- Multiplayer is an *overlay* on the wild-battle loop. The reviewer hooks
  call exactly one function here (`notify_card_reviewed`); everything else
  happens on background threads or in the multiplayer window.
- All server I/O is short-lived request/response HTTP through
  `MultiplayerApiClient`, dispatched via `mw.taskman` — the review flow
  never waits on the network.
- Server state (raid + matches) is cached on the controller and rendered
  from cache by the reviewer HUD; it refreshes when event batches flush or
  the idle poll fires.
"""

import json
import threading
from typing import Callable, Optional

from aqt import gui_hooks, mw
from PyQt6.QtCore import QTimer

from ..resources import user_path
from .api_client import (
    MultiplayerApiClient,
    MultiplayerAuthError,
    load_credentials,
)
from .hud import build_hud_fragment
from .outbox import Outbox

STATE_PATH = user_path / "multiplayer_state.json"

FLUSH_INTERVAL_MS = 15_000
FLUSH_EVENT_THRESHOLD = 20
ACTIVE_POLL_SECONDS = 30
IDLE_POLL_SECONDS = 300

CARDS_PER_TOKEN = 10

BOSS_TOAST_THRESHOLDS = (75, 50, 25)

_controller = None


def init_multiplayer(settings_obj, logger, main_pokemon):
    """Create the singleton controller. Called once from addon startup."""
    global _controller
    if _controller is None:
        _controller = MultiplayerController(settings_obj, logger, main_pokemon)
    return _controller


def get_controller():
    return _controller


def notify_card_reviewed(grade: str, time_elapsed: int):
    """The single hook-side entry point; must never raise into the reviewer."""
    if _controller is None:
        return
    try:
        _controller.on_card_reviewed(grade, time_elapsed)
        message = _controller.drain_toast()
        if message:
            from ..functions.drawing_utils import tooltipWithColour

            tooltipWithColour(message, "#7FB3D5")
    except Exception as e:
        try:
            _controller.logger.log("error", f"Ankimon multiplayer: {e}")
        except Exception:
            pass


class MultiplayerController:
    def __init__(self, settings_obj, logger, main_pokemon):
        self.settings = settings_obj
        self.logger = logger
        self.main_pokemon = main_pokemon
        self.api = MultiplayerApiClient(settings_obj)
        self.outbox = Outbox()

        self.state = self._load_state()
        self._toasts = []
        self._toast_lock = threading.Lock()
        self._flush_inflight = False
        self._auth_failed = False
        self._seconds_since_sync = 0

        self._timer = QTimer(mw)
        self._timer.timeout.connect(self._on_timer)
        self._timer.start(FLUSH_INTERVAL_MS)

        gui_hooks.profile_will_close.append(self.on_profile_will_close)

    # --- Enablement ---------------------------------------------------------

    @property
    def enabled(self) -> bool:
        return bool(
            self.settings.get("multiplayer.enabled", False)
            and load_credentials() is not None
            and not self._auth_failed
        )

    def reset_auth(self):
        """Called after the player saves new credentials or settings."""
        self._auth_failed = False

    # --- Review hook ----------------------------------------------------------

    def on_card_reviewed(self, grade: str, time_elapsed: int):
        if not self.enabled:
            return
        self.outbox.push(
            "card_reviewed",
            {
                "grade": grade,
                "time_s": int(time_elapsed or 0),
                "level": int(getattr(self.main_pokemon, "level", 1) or 1),
            },
        )
        # Local, display-only token progress; the server value wins on sync.
        pvp = self.state.setdefault("pvp", {})
        progress = pvp.get("token_progress", 0) + 1
        if progress >= CARDS_PER_TOKEN:
            progress = 0
            pvp["tokens"] = min(pvp.get("tokens", 0) + 1, 3)
            if self._has_active_match():
                self._queue_toast("PvP turn token charged!")
        pvp["token_progress"] = progress

        if self.outbox.pending_count() >= FLUSH_EVENT_THRESHOLD:
            self.flush_soon()

    # --- Background sync -----------------------------------------------------

    def _on_timer(self):
        if not self.enabled:
            return
        self._seconds_since_sync += FLUSH_INTERVAL_MS // 1000
        if self.outbox.pending_count() > 0:
            self.flush_soon()
            return
        poll_after = (
            ACTIVE_POLL_SECONDS if self._has_active_session() else IDLE_POLL_SECONDS
        )
        if self._seconds_since_sync >= poll_after:
            self.refresh_state()

    def flush_soon(self):
        """Send the next outbox batch in the background."""
        if self._flush_inflight or not self.enabled:
            return
        batch = self.outbox.peek_batch()
        if not batch:
            return
        self._flush_inflight = True

        def task():
            return self.api.post_events(batch)

        def on_done(future):
            self._flush_inflight = False
            try:
                state = future.result()
            except MultiplayerAuthError:
                self._handle_auth_failure()
                return
            except Exception:
                return  # keep events queued; next timer tick retries
            self.outbox.ack(batch)
            self._apply_state(state)
            if self.outbox.pending_count() > 0:
                self.flush_soon()

        mw.taskman.run_in_background(task, on_done)

    def refresh_state(self, on_finished: Optional[Callable] = None):
        """Fetch state without sending events (idle poll / window refresh)."""
        if not self.enabled:
            if on_finished:
                on_finished(False)
            return

        def task():
            return self.api.get_state()

        def on_done(future):
            try:
                state = future.result()
            except MultiplayerAuthError:
                self._handle_auth_failure()
                if on_finished:
                    on_finished(False)
                return
            except Exception:
                if on_finished:
                    on_finished(False)
                return
            self._apply_state(state)
            if on_finished:
                on_finished(True)

        mw.taskman.run_in_background(task, on_done)

    def run_action(self, task: Callable, on_done: Callable):
        """Run one API action in the background; used by the window.

        `on_done(result, error)` is invoked on the main thread. A returned
        state payload is applied to the cache automatically.
        """

        def wrapper(future):
            try:
                result = future.result()
            except MultiplayerAuthError as e:
                self._handle_auth_failure()
                on_done(None, e)
                return
            except Exception as e:
                on_done(None, e)
                return
            if isinstance(result, dict) and ("raid" in result or "pvp" in result):
                self._apply_state(result)
            on_done(result, None)

        mw.taskman.run_in_background(task, wrapper)

    def _handle_auth_failure(self):
        if not self._auth_failed:
            self._auth_failed = True
            self._queue_toast("Multiplayer sign-in failed — check your credentials.")

    # --- State cache -----------------------------------------------------------

    def _load_state(self) -> dict:
        try:
            with open(STATE_PATH, "r", encoding="utf-8") as f:
                state = json.load(f)
            if isinstance(state, dict):
                return state
        except (OSError, json.JSONDecodeError):
            pass
        return {}

    def _save_state(self):
        try:
            with open(STATE_PATH, "w", encoding="utf-8") as f:
                json.dump(self.state, f)
        except OSError:
            pass

    def _apply_state(self, new_state: dict):
        """Merge fresh server state into the cache and derive toasts."""
        if not isinstance(new_state, dict):
            return
        old_state = self.state
        merged = dict(old_state)
        for key in ("raid", "pvp"):
            if key in new_state:
                merged[key] = new_state[key]
        self._derive_toasts(old_state, merged)
        self.state = merged
        self._seconds_since_sync = 0
        self._save_state()

    def _derive_toasts(self, old_state: dict, new_state: dict):
        old_raid = old_state.get("raid") or {}
        new_raid = new_state.get("raid") or {}
        if new_raid.get("boss_max_hp"):
            new_pct = 100 * new_raid.get("boss_hp", 0) / new_raid["boss_max_hp"]
            old_pct = (
                100 * old_raid.get("boss_hp", 0) / old_raid["boss_max_hp"]
                if old_raid.get("boss_max_hp")
                else 100
            )
            boss = new_raid.get("boss_name", "The raid boss")
            if new_raid.get("boss_hp", 1) <= 0 < old_raid.get("boss_hp", 1):
                self._queue_toast(f"{boss} was defeated! Claim your raid reward.")
            else:
                for threshold in BOSS_TOAST_THRESHOLDS:
                    if new_pct <= threshold < old_pct:
                        self._queue_toast(f"{boss} is down to {int(new_pct)}% HP!")
                        break

        old_matches = {
            m.get("id"): m for m in (old_state.get("pvp") or {}).get("matches", [])
        }
        for match in (new_state.get("pvp") or {}).get("matches", []):
            old_match = old_matches.get(match.get("id"), {})
            opponent = match.get("opponent", "Your rival")
            if match.get("incoming_challenge") and not old_match.get(
                "incoming_challenge"
            ):
                self._queue_toast(f"{opponent} challenged you to a battle!")
            elif match.get("opponent_move_committed") and not old_match.get(
                "opponent_move_committed"
            ):
                self._queue_toast(f"{opponent} committed their move!")
            elif match.get("status") == "finished" and old_match.get("status") not in (
                None,
                "finished",
            ):
                winner = match.get("winner", "")
                credentials = load_credentials() or {}
                if winner and winner == credentials.get("username"):
                    self._queue_toast(f"You won the battle against {opponent}!")
                else:
                    self._queue_toast(f"The battle against {opponent} is over.")

    def _has_active_session(self) -> bool:
        return bool(self.state.get("raid")) or self._has_active_match()

    def _has_active_match(self) -> bool:
        matches = (self.state.get("pvp") or {}).get("matches", [])
        return any(m.get("status") in ("active", "pending") for m in matches)

    # --- Reviewer-facing output --------------------------------------------------

    def get_hud_fragment(self):
        """(html, css) for the reviewer HUD, or None. Cached state only."""
        try:
            if not self.enabled:
                return None
            return build_hud_fragment(self.state)
        except Exception:
            return None

    def _queue_toast(self, message: str):
        with self._toast_lock:
            self._toasts.append(message)
            del self._toasts[:-5]  # never let stale toasts pile up

    def drain_toast(self) -> Optional[str]:
        """Return at most one queued toast; called once per answered card."""
        with self._toast_lock:
            if self._toasts:
                return self._toasts.pop(0)
        return None

    # --- Lifecycle ----------------------------------------------------------------

    def on_profile_will_close(self):
        self._save_state()
        # Best-effort final flush; the outbox persists anything that fails.
        self.flush_soon()
