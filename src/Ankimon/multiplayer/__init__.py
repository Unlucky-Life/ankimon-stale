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

import inspect
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
from .encounter import install_raid_boss_encounter_patch
from .hud import build_hud_fragment
from .outbox import Outbox
from .raid_rewards import claim_raid_reward

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

    caller_frame = inspect.currentframe().f_back
    caller_globals = caller_frame.f_globals if caller_frame is not None else {}
    install_raid_boss_encounter_patch(_controller, caller_globals)
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
        # Rounds this client is already simulating, keyed by
        # (match, round, attempt) so a replay is a new piece of work and a
        # slow poll never starts the same one twice.
        self._resolving = set()

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
        for key in (
            "raid",
            "raid_rooms",
            "friends",
            "friend_requests",
            "raid_reward",
            "pvp",
            "guest",
        ):
            if key in new_state:
                merged[key] = new_state[key]
        self._derive_toasts(old_state, merged)
        self._claim_raid_reward(merged.get("raid_reward"))
        self.state = merged
        self._seconds_since_sync = 0
        self._save_state()
        self._resolve_open_rounds()

    def _claim_raid_reward(self, reward):
        try:
            message = claim_raid_reward(reward)
        except Exception as exc:
            self.logger.log("warning", f"Could not claim raid reward: {exc}")
            return
        if message:
            self._queue_toast(message)

    def _derive_toasts(self, old_state: dict, new_state: dict):
        old_raid = old_state.get("raid") or {}
        new_raid = new_state.get("raid") or {}
        if new_raid.get("locked") and not old_raid.get("locked"):
            boss = new_raid.get("boss_name", "The raid")
            self._queue_toast(f"{boss} raid has started — no one else can join.")
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

        old_friends = {
            f.get("username"): f for f in (old_state.get("friends") or [])
        }
        for friend in new_state.get("friends") or []:
            name = friend.get("username")
            old_friend = old_friends.get(name, {})
            if friend.get("online") and not old_friend.get("online"):
                self._queue_toast(f"{name} is online!")

        old_incoming = {
            r.get("username")
            for r in (old_state.get("friend_requests") or {}).get("incoming", [])
        }
        new_requests = new_state.get("friend_requests") or {}
        for request in new_requests.get("incoming", []):
            name = request.get("username")
            if name and name not in old_incoming:
                self._queue_toast(f"{name} sent you a friend request!")

        old_outgoing = {
            r.get("username")
            for r in (old_state.get("friend_requests") or {}).get("outgoing", [])
        }
        new_outgoing = {r.get("username") for r in new_requests.get("outgoing", [])}
        new_friend_names = {f.get("username") for f in new_state.get("friends") or []}
        for name in old_outgoing - new_outgoing:
            if name in new_friend_names:
                self._queue_toast(f"{name} accepted your friend request!")

        old_matches = {
            m.get("id"): m for m in (old_state.get("pvp") or {}).get("matches", [])
        }
        for match in (new_state.get("pvp") or {}).get("matches", []):
            old_match = old_matches.get(match.get("id"), {})
            opponent = match.get("opponent", "Your rival")
            resolution = match.get("resolution") or {}
            old_resolution = old_match.get("resolution") or {}
            status = match.get("status")
            old_status = old_match.get("status")
            if match.get("incoming_challenge") and not old_match.get(
                "incoming_challenge"
            ):
                self._queue_toast(f"{opponent} challenged you to a battle!")
            elif status == "suspended" and old_status not in (None, "suspended"):
                # A battle that ends with no winner and no explanation reads
                # as the addon losing it.
                reason = match.get("suspended_reason") or "the two games disagreed"
                self._queue_toast(
                    f"Battle against {opponent} suspended - {reason}. "
                    "No winner, and your turn token came back."
                )
            elif status == "stalled" and old_status not in (None, "stalled"):
                self._queue_toast(
                    f"{opponent} has not confirmed the round yet - the battle is on hold."
                )
            elif resolution.get("attempt", 1) > old_resolution.get("attempt", 1):
                self._queue_toast(
                    f"Round {resolution.get('round', '')} against {opponent} "
                    "is being replayed - no damage was applied."
                )
            elif match.get("opponent_move_committed") and not old_match.get(
                "opponent_move_committed"
            ):
                self._queue_toast(f"{opponent} committed their move!")
            elif status == "finished" and old_status not in (
                None,
                "finished",
            ):
                winner = match.get("winner", "")
                credentials = load_credentials() or {}
                if winner and winner == credentials.get("username"):
                    self._queue_toast(f"You won the battle against {opponent}!")
                else:
                    self._queue_toast(f"The battle against {opponent} is over.")

    # --- Peer-verified PvP rounds -------------------------------------------

    def _battle_states(self) -> dict:
        """Per-match engine state carried between rounds, kept on disk.

        The server stores HP and hashes, not Pokemon state, so each client
        keeps its own. Losing it (a fresh profile, a cleared cache) does not
        let the round be faked: the reconstruction below will disagree with
        the opponent, and a disagreement suspends the battle rather than
        deciding it.
        """
        return self.state.setdefault("pvp_battle_states", {})

    def _carried_state(self, match_id: str, round_number: int):
        carried = self._battle_states().get(match_id)
        if not carried or carried.get("after_round") != round_number - 1:
            return None
        return carried.get("state")

    @staticmethod
    def _pvp_modules():
        """Import the resolver lazily.

        It pulls in the whole battle engine and its move data, which is
        several megabytes of JSON: worth loading the first time a PvP round
        actually has to be simulated, not on every Anki start.
        """
        from . import pvp_resolution, pvp_team

        return pvp_resolution, pvp_team

    def _resolve_open_rounds(self):
        """Simulate and report any open round this client has not answered.

        Runs off the review flow entirely: the poll that discovers the round
        is already on a background thread, and the simulation and its submit
        go back to one.
        """
        if not self.enabled:
            return
        username = (load_credentials() or {}).get("username")
        if not username:
            return
        matches = (self.state.get("pvp") or {}).get("matches", [])
        # Forget work claimed for battles that are over, so a long session
        # does not carry every round it ever played.
        live = {match.get("id") for match in matches if match.get("status") == "active"}
        self._resolving = {key for key in self._resolving if key[0] in live}
        for match in matches:
            resolution = match.get("resolution")
            if not resolution or match.get("status") != "active":
                continue
            if resolution.get("you_submitted"):
                continue
            key = (
                match.get("id"),
                resolution.get("round"),
                resolution.get("attempt"),
            )
            if key in self._resolving:
                continue
            self._resolving.add(key)
            self._start_round_resolution(match, resolution, username, key)

    def _start_round_resolution(self, match, resolution, username, key):
        match_id = match.get("id")
        round_number = int(resolution.get("round") or 0)
        moves = resolution.get("moves") or {}
        opponent = match.get("opponent")
        # Roles, not viewpoints: both clients put the challenger on the
        # state's user side, or every honest round would hash differently.
        i_am_challenger = bool(match.get("you_are_challenger"))
        challenger_name = username if i_am_challenger else opponent
        opponent_name = opponent if i_am_challenger else username
        carried = self._carried_state(match_id, round_number)
        challenger_team = match.get("your_team") if i_am_challenger else match.get("opponent_team")
        opponent_team = match.get("opponent_team") if i_am_challenger else match.get("your_team")

        pvp_resolution, _ = self._pvp_modules()

        def task():
            return pvp_resolution.resolve_round(
                moves.get(challenger_name),
                moves.get(opponent_name),
                resolution.get("seed"),
                challenger_team=challenger_team,
                opponent_team=opponent_team,
                carried_state=carried,
            )

        def on_done(future):
            try:
                result = future.result()
            except pvp_resolution.ResolutionError as exc:
                self._resolving.discard(key)
                self.logger.log("warning", f"Could not resolve PvP round: {exc}")
                return
            except Exception as exc:
                self._resolving.discard(key)
                self.logger.log("error", f"PvP round resolution failed: {exc}")
                return
            self._battle_states()[match_id] = {
                "after_round": round_number,
                "state": pvp_resolution.dump_state(result.state),
            }
            self._save_state()
            self._submit_round_result(
                match_id,
                round_number,
                result,
                {
                    challenger_name: result.hp[pvp_resolution.CHALLENGER],
                    opponent_name: result.hp[pvp_resolution.OPPONENT],
                },
                key,
            )

        mw.taskman.run_in_background(task, on_done)

    def _submit_round_result(self, match_id, round_number, result, hp_after, key):
        def task():
            return self.api.submit_round_result(
                match_id,
                round_number,
                result.state_hash,
                hp_after,
                engine_version=self._engine_version(),
                log_digest=result.log_digest,
            )

        def on_done(future):
            # The key stays claimed on success: this client has now reported
            # this attempt, and reporting a *different* result for it would
            # suspend the battle.
            try:
                state = future.result()
            except MultiplayerAuthError:
                self._resolving.discard(key)
                self._handle_auth_failure()
                return
            except Exception as exc:
                # A 409 means the server already moved on (deadline passed,
                # or the match was suspended); anything else is worth one
                # more try on the next poll.
                self._resolving.discard(key)
                self.logger.log("warning", f"Could not report PvP round: {exc}")
                return
            self._apply_state(state)

        mw.taskman.run_in_background(task, on_done)

    def pvp_match_credentials(self):
        """(engine_version, team) to send when starting or accepting a match.

        Both are uploaded once per match: the version so a skew is refused up
        front instead of suspending a battle mid-round, the team so every
        later round has the same inputs on both machines. Either can come
        back None — a missing one costs a check, not the battle.
        """
        return self._engine_version(), self._serialized_team()

    def _serialized_team(self):
        try:
            _, pvp_team = self._pvp_modules()
            return pvp_team.dump_team(self.main_pokemon.to_poke_engine_Pokemon())
        except Exception as exc:
            self.logger.log("warning", f"Could not serialize PvP team: {exc}")
            return None

    def _engine_version(self):
        try:
            from .engine_version import engine_version

            return engine_version()
        except Exception as exc:
            # Without a version the server cannot spot a skew, but a match
            # is still playable — do not block the round on it.
            self.logger.log("warning", f"Could not compute engine version: {exc}")
            return None

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
