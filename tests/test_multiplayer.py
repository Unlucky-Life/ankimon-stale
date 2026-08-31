"""Regression tests for the multiplayer controller, HUD, and raid rewards.

These load modules directly from their file path with stub parent packages,
so they run without a live Anki install (importing the real ``Ankimon``
package executes the whole addon and needs ``aqt``). Same style as
``tests/test_battle_hardening.py``.
"""

import importlib.util
import json
import os
import shutil
import sys
import tempfile
import types
from pathlib import Path

import pytest

SRC = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "src"))
ANKIMON = os.path.join(SRC, "Ankimon")


@pytest.fixture
def tmp_dir():
    """A scratch directory outside pytest's own basetemp.

    Avoids relying on pytest's ``tmp_path``, whose basetemp
    (``%LOCALAPPDATA%\\Temp\\pytest-of-<user>``) can be left in a
    permission-denied state by other tools on this machine.
    """
    path = tempfile.mkdtemp(prefix="ankimon-mp-test-")
    try:
        yield path
    finally:
        shutil.rmtree(path, ignore_errors=True)


def _stub_package(name):
    module = types.ModuleType(name)
    module.__path__ = []
    sys.modules[name] = module
    return module


def _load_from_path(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def _load_package_from_path(name, path):
    """Load a package's __init__.py so its own relative imports resolve."""
    spec = importlib.util.spec_from_file_location(
        name, path, submodule_search_locations=[]
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


# --- MultiplayerController -----------------------------------------------


class FakeSignal:
    def connect(self, fn):
        self._fn = fn


class FakeQTimer:
    def __init__(self, parent=None):
        self.timeout = FakeSignal()
        self.started_ms = None

    def start(self, ms):
        self.started_ms = ms


class FakeGuiHooks:
    def __init__(self):
        self.profile_will_close = []


class FakeSettings:
    def __init__(self, **values):
        self._values = dict(values)

    def get(self, key, default=None):
        return self._values.get(key, default)

    def set(self, key, value):
        self._values[key] = value


class FakeLogger:
    def __init__(self):
        self.entries = []

    def log(self, level, message):
        self.entries.append((level, message))


@pytest.fixture
def multiplayer_module(tmp_dir):
    saved = dict(sys.modules)
    try:
        _stub_package("Ankimon")

        resources = types.ModuleType("Ankimon.resources")
        resources.user_path = Path(tmp_dir)
        sys.modules["Ankimon.resources"] = resources

        aqt = types.ModuleType("aqt")
        aqt.gui_hooks = FakeGuiHooks()
        aqt.mw = types.SimpleNamespace(
            taskman=types.SimpleNamespace(run_in_background=lambda *a, **k: None)
        )
        sys.modules["aqt"] = aqt

        pyqt6 = types.ModuleType("PyQt6")
        pyqt6.__path__ = []
        sys.modules["PyQt6"] = pyqt6
        qtcore = types.ModuleType("PyQt6.QtCore")
        qtcore.QTimer = FakeQTimer
        sys.modules["PyQt6.QtCore"] = qtcore

        api_client_stub = types.ModuleType("Ankimon.multiplayer.api_client")

        class MultiplayerApiClient:
            def __init__(self, settings_obj):
                self.settings = settings_obj

        class MultiplayerAuthError(Exception):
            pass

        api_client_stub.MultiplayerApiClient = MultiplayerApiClient
        api_client_stub.MultiplayerAuthError = MultiplayerAuthError
        api_client_stub.load_credentials = lambda: {
            "username": "ash",
            "api_key": "secret",
        }

        encounter_stub = types.ModuleType("Ankimon.multiplayer.encounter")
        encounter_stub.install_raid_boss_encounter_patch = (
            lambda controller, caller_globals: None
        )

        hud_stub = types.ModuleType("Ankimon.multiplayer.hud")
        hud_stub.build_hud_fragment = lambda state: None

        outbox_stub = types.ModuleType("Ankimon.multiplayer.outbox")

        class Outbox:
            def __init__(self):
                self._events = []

            def push(self, event_type, payload):
                self._events.append((event_type, payload))

            def pending_count(self):
                return len(self._events)

            def peek_batch(self):
                return list(self._events)

            def ack(self, events):
                acked = list(events)
                self._events = [e for e in self._events if e not in acked]

        outbox_stub.Outbox = Outbox

        raid_rewards_stub = types.ModuleType("Ankimon.multiplayer.raid_rewards")
        raid_rewards_stub.claimed_calls = []

        def _claim_raid_reward(reward):
            raid_rewards_stub.claimed_calls.append(reward)
            return None

        raid_rewards_stub.claim_raid_reward = _claim_raid_reward

        sys.modules["Ankimon.multiplayer.api_client"] = api_client_stub
        sys.modules["Ankimon.multiplayer.encounter"] = encounter_stub
        sys.modules["Ankimon.multiplayer.hud"] = hud_stub
        sys.modules["Ankimon.multiplayer.outbox"] = outbox_stub
        sys.modules["Ankimon.multiplayer.raid_rewards"] = raid_rewards_stub

        module = _load_package_from_path(
            "Ankimon.multiplayer",
            os.path.join(ANKIMON, "multiplayer", "__init__.py"),
        )
        yield module
    finally:
        sys.modules.clear()
        sys.modules.update(saved)


def _make_controller(module):
    return module.MultiplayerController(
        FakeSettings(**{"multiplayer.enabled": True}),
        FakeLogger(),
        types.SimpleNamespace(level=10),
    )


def _active_match(**overrides):
    match = {
        "id": "match-1",
        "opponent": "gary",
        "status": "active",
        "your_move_committed": False,
        "opponent_pokemon": {
            "id": 25,
            "name": "pikachu",
            "hp": 100,
            "max_hp": 100,
        },
    }
    match.update(overrides)
    return match


class ImmediateFuture:
    def __init__(self, value):
        self.value = value

    def result(self):
        return self.value


def test_new_active_match_replaces_the_encounter_immediately(multiplayer_module):
    starts = []
    controller = multiplayer_module.MultiplayerController(
        FakeSettings(**{"multiplayer.enabled": True}),
        FakeLogger(),
        types.SimpleNamespace(level=10),
        start_reviewer_battle=lambda: starts.append(True) or True,
    )
    controller.state = {"pvp": {"matches": []}}

    controller._apply_state({"pvp": {"matches": [_active_match()]}})

    # No wild Pokemon stays on screen once a friend battle starts.
    assert starts == [True]
    controller.on_card_reviewed("good", 4)
    assert starts == [True]


def test_encounter_swap_retries_when_the_reviewer_is_closed(multiplayer_module):
    starts = []

    def start():
        starts.append(True)
        return len(starts) > 1  # closed reviewer first, open on the retry

    controller = multiplayer_module.MultiplayerController(
        FakeSettings(**{"multiplayer.enabled": True}),
        FakeLogger(),
        types.SimpleNamespace(level=10),
        start_reviewer_battle=start,
    )
    controller.state = {"pvp": {"matches": []}}

    controller._apply_state({"pvp": {"matches": [_active_match()]}})
    assert len(starts) == 1

    controller.on_card_reviewed("good", 4)
    assert len(starts) == 2
    controller.on_card_reviewed("good", 4)
    assert len(starts) == 2


def test_opponent_switching_pokemon_rebuilds_the_encounter(multiplayer_module):
    starts = []
    controller = multiplayer_module.MultiplayerController(
        FakeSettings(**{"multiplayer.enabled": True}),
        FakeLogger(),
        types.SimpleNamespace(level=10),
        start_reviewer_battle=lambda: starts.append(True) or True,
    )
    controller.state = {"pvp": {"matches": [_active_match()]}}

    switched = _active_match()
    switched["opponent_pokemon"] = {
        "id": 143, "name": "snorlax", "hp": 100, "max_hp": 100,
    }
    controller._apply_state({"pvp": {"matches": [switched]}})

    assert starts == [True]


def test_active_pokemon_payload_reports_the_selected_pokemon(multiplayer_module):
    controller = multiplayer_module.MultiplayerController(
        FakeSettings(**{"multiplayer.enabled": True}),
        FakeLogger(),
        types.SimpleNamespace(id=143, name="snorlax", level=62),
    )

    assert controller.active_pokemon_payload() == {
        "name": "Snorlax",
        "id": 143,
        "level": 62,
    }


def test_active_pokemon_payload_skips_an_unusable_pokemon(multiplayer_module):
    controller = multiplayer_module.MultiplayerController(
        FakeSettings(**{"multiplayer.enabled": True}),
        FakeLogger(),
        types.SimpleNamespace(id=0, name="", level=5),
    )

    assert controller.active_pokemon_payload() is None


def test_opponent_damage_refreshes_the_reviewer_battle(multiplayer_module):
    refreshes = []
    controller = multiplayer_module.MultiplayerController(
        FakeSettings(**{"multiplayer.enabled": True}),
        FakeLogger(),
        types.SimpleNamespace(level=10),
        refresh_reviewer_battle=lambda: refreshes.append(True) or True,
    )
    controller.state = {"pvp": {"matches": [_active_match()]}}

    hurt = _active_match(your_hp=60)
    hurt["opponent_pokemon"] = dict(hurt["opponent_pokemon"], hp=40)
    controller._apply_state({"pvp": {"matches": [hurt]}})

    assert refreshes == [True]


def test_unchanged_match_does_not_refresh_the_reviewer_battle(multiplayer_module):
    refreshes = []
    controller = multiplayer_module.MultiplayerController(
        FakeSettings(**{"multiplayer.enabled": True}),
        FakeLogger(),
        types.SimpleNamespace(level=10),
        refresh_reviewer_battle=lambda: refreshes.append(True) or True,
    )
    controller.state = {"pvp": {"matches": [_active_match()]}}

    controller._apply_state({"pvp": {"matches": [_active_match()]}})

    assert refreshes == []


def test_sync_reviewer_enemy_copies_server_hp(multiplayer_module):
    controller = _make_controller(multiplayer_module)
    match = _active_match()
    match["opponent_pokemon"] = dict(match["opponent_pokemon"], hp=30)
    controller.state = {"pvp": {"matches": [match]}}
    enemy = types.SimpleNamespace(
        id=25, tier="PvP: gary", hp=100, current_hp=100, max_hp=100,
        battle_status="fighting",
    )

    assert controller.is_reviewer_pvp_enemy(enemy) is True
    assert enemy.hp == 30
    assert enemy.current_hp == 30
    assert enemy.max_hp == 100

    wild = types.SimpleNamespace(
        id=25, tier="Normal", hp=100, current_hp=100, max_hp=100,
        battle_status="fighting",
    )
    assert controller.is_reviewer_pvp_enemy(wild) is False
    assert wild.hp == 100


def test_reviewed_card_is_queued_for_the_server(multiplayer_module):
    controller = _make_controller(multiplayer_module)
    controller.state = {"pvp": {"matches": []}}

    controller.on_card_reviewed("good", 3)

    # The card itself is what pays for an attack, so it has to reach the
    # server; nothing is tallied locally any more.
    assert controller.outbox.pending_count() == 1


def test_reviewer_attack_submits_active_opponent_move(multiplayer_module):
    controller = _make_controller(multiplayer_module)
    controller.state = {"pvp": {"matches": [_active_match()]}}
    submitted = []
    completed_match = _active_match(
        status="finished",
        your_move_committed=True,
        opponent_pokemon={
            "id": 25,
            "name": "pikachu",
            "hp": 0,
            "max_hp": 100,
        },
    )
    completed_state = {"pvp": {"matches": [completed_match]}}
    controller.api.submit_turn = (
        lambda match_id, move: submitted.append((match_id, move)) or completed_state
    )
    refreshes = []
    controller._refresh_reviewer_battle = lambda: refreshes.append(True) or True

    def run_immediately(task, on_done):
        on_done(ImmediateFuture(task()))

    multiplayer_module.mw.taskman.run_in_background = run_immediately
    enemy = types.SimpleNamespace(
        id=25, tier="PvP: gary", hp=73, current_hp=73, max_hp=100,
        battle_status="fighting",
    )

    controller.main_pokemon.attacks = ["thunderbolt"]
    controller.on_reviewer_attack(enemy)

    assert submitted == [("match-1", "thunderbolt")]
    assert enemy.hp == 0
    assert enemy.current_hp == 0
    assert enemy.battle_status == "fainted"
    assert refreshes == [True]
    assert controller._pending_reviewer_match_id == "match-1"


def test_missing_opponent_hp_does_not_faint_the_enemy(multiplayer_module):
    controller = _make_controller(multiplayer_module)
    match = _active_match()
    match["opponent_hp"] = 55
    match["opponent_pokemon"] = {"id": 25, "name": "pikachu", "max_hp": 100}
    controller.state = {"pvp": {"matches": [match]}}
    enemy = types.SimpleNamespace(
        id=25, tier="PvP: gary", hp=70, current_hp=70, max_hp=100,
        battle_status="fighting",
    )

    assert controller.is_reviewer_pvp_enemy(enemy) is True
    # The match-level HP stands in; an absent field is unknown, not zero.
    assert enemy.hp == 55
    assert enemy.battle_status == "fighting"


def test_unknown_opponent_hp_leaves_the_enemy_untouched(multiplayer_module):
    controller = _make_controller(multiplayer_module)
    match = _active_match()
    match["opponent_pokemon"] = {"id": 25, "name": "pikachu", "max_hp": 100}
    controller.state = {"pvp": {"matches": [match]}}
    enemy = types.SimpleNamespace(
        id=25, tier="PvP: gary", hp=70, current_hp=70, max_hp=100,
        battle_status="fighting",
    )

    assert controller.is_reviewer_pvp_enemy(enemy) is True
    assert enemy.hp == 70
    assert enemy.battle_status == "fighting"


def test_poll_is_skipped_while_a_move_is_in_flight(multiplayer_module):
    controller = _make_controller(multiplayer_module)
    controller.state = {"pvp": {"matches": [_active_match()]}}
    polls = []
    controller.api.get_state = lambda: polls.append(True) or {}

    def run_immediately(task, on_done):
        on_done(ImmediateFuture(task()))

    multiplayer_module.mw.taskman.run_in_background = run_immediately

    controller._turn_submissions_inflight.add("match-1")
    finished = []
    controller.refresh_state(on_finished=finished.append)

    # A poll answered from the pre-move snapshot would roll the committed
    # move and the HP bar back on screen.
    assert polls == []
    assert finished == [False]

    controller._turn_submissions_inflight.discard("match-1")
    controller.refresh_state()
    assert polls == [True]


def test_reviewer_attack_picks_a_random_move(multiplayer_module):
    controller = _make_controller(multiplayer_module)
    controller.main_pokemon.attacks = ["thunderbolt", "quick attack", "iron tail"]

    picked = {controller.random_attack() for _ in range(60)}

    assert picked == {"thunderbolt", "quick attack", "iron tail"}


def test_random_move_falls_back_to_the_server_pick(multiplayer_module):
    controller = _make_controller(multiplayer_module)
    controller.main_pokemon.attacks = []

    # An empty move asks the server to pick one rather than skipping the
    # attack the answered card already paid for.
    assert controller.random_attack() == ""


def test_random_move_reads_dict_shaped_attacks(multiplayer_module):
    controller = _make_controller(multiplayer_module)
    controller.main_pokemon.attacks = [{"name": "hydro pump"}, "", None]

    assert controller.random_attack() == "hydro pump"


def test_reviewer_attack_ignores_wild_encounter(multiplayer_module):
    controller = _make_controller(multiplayer_module)
    controller.state = {"pvp": {"matches": [_active_match()]}}
    submitted = []
    controller.api.submit_turn = lambda match_id, move: submitted.append((match_id, move))
    enemy = types.SimpleNamespace(id=25, tier="Normal")

    controller.on_reviewer_attack(enemy)

    assert submitted == []


def test_reviewer_attack_sends_the_answered_card_first(multiplayer_module):
    """The server refuses a move from a player who has not answered a card."""
    controller = _make_controller(multiplayer_module)
    controller.state = {"pvp": {"matches": [_active_match()]}}
    calls = []
    controller.api.post_events = (
        lambda events, active_pokemon=None: calls.append(("events", len(events)))
    )
    controller.api.submit_turn = (
        lambda match_id, move: calls.append(("turn", move))
        or {"pvp": {"matches": [_active_match(your_move_committed=True)]}}
    )

    def run_immediately(task, on_done):
        on_done(ImmediateFuture(task()))

    multiplayer_module.mw.taskman.run_in_background = run_immediately
    enemy = types.SimpleNamespace(
        id=25, tier="PvP: gary", hp=100, current_hp=100, max_hp=100,
        battle_status="fighting",
    )

    controller.main_pokemon.attacks = ["thunderbolt"]
    controller.on_card_reviewed("good", 3)
    controller.on_reviewer_attack(enemy)

    assert calls == [("events", 1), ("turn", "thunderbolt")]
    # The batch is acknowledged only after the whole chain succeeded.
    assert controller.outbox.pending_count() == 0


def test_reviewer_attack_keeps_the_card_queued_when_it_fails(multiplayer_module):
    controller = _make_controller(multiplayer_module)
    controller.state = {"pvp": {"matches": [_active_match()]}}

    def boom(match_id, move):
        raise RuntimeError("offline")

    controller.api.post_events = lambda events, active_pokemon=None: {}
    controller.api.submit_turn = boom

    def run_immediately(task, on_done):
        class FailedFuture:
            def result(self):
                return task()

        on_done(FailedFuture())

    multiplayer_module.mw.taskman.run_in_background = run_immediately
    enemy = types.SimpleNamespace(
        id=25, tier="PvP: gary", hp=100, current_hp=100, max_hp=100,
        battle_status="fighting",
    )

    controller.on_card_reviewed("good", 3)
    controller.on_reviewer_attack(enemy)

    assert controller.outbox.pending_count() == 1
    assert controller._turn_submissions_inflight == set()


def test_apply_state_merges_friends_and_raid_rooms(multiplayer_module):
    controller = _make_controller(multiplayer_module)
    controller.state = {}

    new_state = {
        "guest": False,
        "raid": {
            "code": "ABC234",
            "boss_name": "Articuno",
            "boss_hp": 8200,
            "boss_max_hp": 10000,
        },
        "raid_rooms": [
            {"code": "ABC234", "boss_name": "Articuno", "party_size": 2, "locked": False}
        ],
        "friends": [
            {"username": "misty", "raw_username": "misty", "bot": False, "online": True}
        ],
        "friend_requests": {"incoming": [{"username": "gary"}], "outgoing": []},
        "pvp": {"human_enabled": False, "attack_ready": True, "matches": []},
    }

    controller._apply_state(new_state)

    # Regression: the old merge only copied "raid", "raid_reward", "pvp" and
    # silently dropped everything else, leaving the friends list always empty.
    assert controller.state["friends"] == new_state["friends"]
    assert controller.state["raid_rooms"] == new_state["raid_rooms"]
    assert controller.state["friend_requests"] == new_state["friend_requests"]
    assert controller.state["guest"] is False
    assert controller.state["raid"]["code"] == "ABC234"
    assert controller.state["pvp"]["human_enabled"] is False


def test_apply_state_preserves_unrelated_cached_keys(multiplayer_module):
    controller = _make_controller(multiplayer_module)
    controller.state = {"some_local_only_key": "keep-me"}

    controller._apply_state({"friends": []})

    assert controller.state["some_local_only_key"] == "keep-me"
    assert controller.state["friends"] == []


def test_toast_derived_for_friend_coming_online(multiplayer_module):
    controller = _make_controller(multiplayer_module)
    controller.state = {"friends": [{"username": "misty", "online": False}]}

    controller._apply_state({"friends": [{"username": "misty", "online": True}]})

    assert controller.drain_toast() == "misty is online!"


def test_toast_derived_for_boss_hp_threshold(multiplayer_module):
    controller = _make_controller(multiplayer_module)
    controller.state = {
        "raid": {"boss_name": "Articuno", "boss_hp": 8000, "boss_max_hp": 10000}
    }

    controller._apply_state(
        {"raid": {"boss_name": "Articuno", "boss_hp": 4000, "boss_max_hp": 10000}}
    )

    toast = controller.drain_toast()
    assert toast is not None
    assert "Articuno" in toast
    assert "40%" in toast


def test_toast_derived_for_raid_locked(multiplayer_module):
    controller = _make_controller(multiplayer_module)
    controller.state = {
        "raid": {"boss_name": "Articuno", "boss_hp": 8000, "boss_max_hp": 10000, "locked": False}
    }

    controller._apply_state(
        {"raid": {"boss_name": "Articuno", "boss_hp": 8000, "boss_max_hp": 10000, "locked": True}}
    )

    toast = controller.drain_toast()
    assert toast is not None
    assert "Articuno" in toast
    assert "started" in toast


def test_toast_derived_for_incoming_friend_request(multiplayer_module):
    controller = _make_controller(multiplayer_module)
    controller.state = {"friend_requests": {"incoming": [], "outgoing": []}}

    controller._apply_state(
        {"friend_requests": {"incoming": [{"username": "gary"}], "outgoing": []}}
    )

    toast = controller.drain_toast()
    assert toast is not None
    assert "gary" in toast


# --- HUD fragment ----------------------------------------------------------


@pytest.fixture
def hud_module():
    saved = dict(sys.modules)
    try:
        _stub_package("Ankimon")
        _stub_package("Ankimon.functions")
        _stub_package("Ankimon.multiplayer")

        business = types.ModuleType("Ankimon.business")
        business.get_image_as_base64 = lambda path: ""
        sys.modules["Ankimon.business"] = business

        sprite_functions = types.ModuleType("Ankimon.functions.sprite_functions")
        sprite_functions.get_sprite_path = lambda *a, **k: ""
        sys.modules["Ankimon.functions.sprite_functions"] = sprite_functions

        yield _load_from_path(
            "Ankimon.multiplayer.hud",
            os.path.join(ANKIMON, "multiplayer", "hud.py"),
        )
    finally:
        sys.modules.clear()
        sys.modules.update(saved)


def test_hud_fragment_none_without_active_raid_or_match(hud_module):
    assert hud_module.build_hud_fragment({}) is None
    assert hud_module.build_hud_fragment({"raid": {}, "pvp": {}}) is None


def test_hud_fragment_none_when_raid_defeated(hud_module):
    state = {
        "raid": {
            "boss_name": "Articuno",
            "boss_id": 144,
            "boss_hp": 0,
            "boss_max_hp": 10000,
            "defeated": True,
        }
    }
    assert hud_module.build_hud_fragment(state) is None


def test_hud_fragment_with_active_raid(hud_module):
    state = {
        "raid": {
            "boss_name": "Articuno",
            "boss_id": 144,
            "boss_hp": 5000,
            "boss_max_hp": 10000,
        }
    }
    result = hud_module.build_hud_fragment(state)
    assert result is not None
    html, css = result
    assert "Articuno" in html
    assert "50%" in html
    assert 'id="ankimon-mp-raid"' in html
    assert "ankimon-mp-panel" in css


def test_hud_tells_player_when_reviewer_attack_is_ready(hud_module):
    state = {
        "pvp": {
            "attack_ready": True,
            "matches": [
                {
                    "status": "active",
                    "your_move_committed": False,
                }
            ],
        }
    }

    html, _css = hud_module.build_hud_fragment(state)

    assert "ATTACK READY" in html


def _battle_state(**match_overrides):
    match = {
        "status": "active",
        "opponent": "gary",
        "your_move_committed": False,
        "opponent_move_committed": False,
        "opponent_pokemon": {
            "name": "gengar",
            "id": 94,
            "level": 50,
            "hp": 30,
            "max_hp": 120,
        },
    }
    match.update(match_overrides)
    return {"pvp": {"attack_ready": True, "matches": [match]}}


def test_hud_shows_friend_battle_like_a_raid_boss(hud_module):
    html, css = hud_module.build_hud_fragment(_battle_state())

    assert 'id="ankimon-mp-battle"' in html
    assert "GARY" in html
    assert "Gengar" in html
    assert "Lv50" in html
    # Same panel shape and live HP bar as the raid boss.
    assert "ankimon-mp-panel" in html
    assert "ankimon-mp-track" in html
    assert "width:25.0%" in html
    assert "25%" in html
    assert "ankimon-mp-fill-battle" in css


def test_hud_battle_panel_waits_for_the_opponent(hud_module):
    html, _css = hud_module.build_hud_fragment(
        _battle_state(your_move_committed=True)
    )

    assert "WAITING FOR OPPONENT" in html
    assert "ATTACK READY" not in html


def test_hud_battle_panel_points_at_the_next_card(hud_module):
    state = _battle_state()
    state["pvp"]["attack_ready"] = False

    html, _css = hud_module.build_hud_fragment(state)

    # No turn currency: the next answered card is the next attack.
    assert "ANSWER A CARD TO ATTACK" in html


def test_hud_shows_raid_and_battle_together(hud_module):
    state = _battle_state()
    state["raid"] = {
        "boss_name": "Articuno",
        "boss_id": 144,
        "boss_hp": 5000,
        "boss_max_hp": 10000,
    }

    html, _css = hud_module.build_hud_fragment(state)

    assert 'id="ankimon-mp-raid"' in html
    assert 'id="ankimon-mp-battle"' in html


def test_hud_battle_panel_falls_back_to_match_hp(hud_module):
    state = _battle_state(opponent_hp=60)
    state["pvp"]["matches"][0]["opponent_pokemon"].pop("hp")

    html, _css = hud_module.build_hud_fragment(state)

    assert "50%" in html


def test_hud_battle_panel_escapes_opponent_names(hud_module):
    html, _css = hud_module.build_hud_fragment(
        _battle_state(opponent="<script>x</script>")
    )

    assert "<script>" not in html
    assert "&lt;SCRIPT&gt;" in html


# --- Encounter selection ----------------------------------------------------


@pytest.fixture
def encounter_module():
    saved = dict(sys.modules)
    try:
        _stub_package("Ankimon")
        _stub_package("Ankimon.functions")
        _stub_package("Ankimon.multiplayer")

        pokedex = types.ModuleType("Ankimon.functions.pokedex_functions")
        pokedex.get_all_pokemon_moves = lambda name, level: ["Tackle", "Growl"]
        pokedex.get_base_experience = lambda actual_id: 100
        pokedex.get_effort_values = lambda actual_id: {"hp": 1}
        pokedex.get_growth_rate = lambda pokemon_id: "medium"
        pokedex.search_pokedex_by_id = lambda pokemon_id: {
            143: "snorlax",
            144: "articuno",
            25: "pikachu",
        }.get(int(pokemon_id), "")
        pokedex.search_pokedex = lambda name, key: {
            "types": ["normal"],
            "baseStats": {
                "hp": 100, "atk": 100, "def": 100, "spa": 100, "spd": 100, "spe": 100
            },
            "abilities": {"0": "immunity"},
            "actual_id": 1,
        }.get(key)
        sys.modules["Ankimon.functions.pokedex_functions"] = pokedex

        pokemon_functions = types.ModuleType("Ankimon.functions.pokemon_functions")
        pokemon_functions.pick_random_gender = lambda name: "M"
        sys.modules["Ankimon.functions.pokemon_functions"] = pokemon_functions

        utils = types.ModuleType("Ankimon.utils")
        utils.get_ev_spread = lambda mode: {"hp": 4}
        sys.modules["Ankimon.utils"] = utils

        yield _load_from_path(
            "Ankimon.multiplayer.encounter",
            os.path.join(ANKIMON, "multiplayer", "encounter.py"),
        )
    finally:
        sys.modules.clear()
        sys.modules.update(saved)


def _encounter_controller(**state):
    return types.SimpleNamespace(state=state, logger=FakeLogger())


def _tracker():
    return types.SimpleNamespace(pokemon_encounter=1, cards_battle_round=3)


TIER_INDEX = 14
NAME_INDEX = 0
LEVEL_INDEX = 2


def test_friend_battle_replaces_the_wild_encounter(encounter_module):
    match = _active_match()
    match["opponent_pokemon"] = {"id": 143, "name": "Snorlax", "level": 62, "hp": 80}
    controller = _encounter_controller(pvp={"matches": [match]})

    result = encounter_module._build_pvp_opponent_tuple(match, 10, _tracker())

    assert result[NAME_INDEX] == "snorlax"
    assert result[LEVEL_INDEX] == 62
    assert result[TIER_INDEX] == "PvP: gary"
    assert encounter_module._active_pvp_match_from_controller(controller) is match


def test_friend_battle_outranks_a_raid_boss(encounter_module):
    match = _active_match()
    match["opponent_pokemon"] = {"id": 143, "name": "Snorlax", "level": 62, "hp": 80}
    controller = _encounter_controller(
        pvp={"matches": [match]},
        raid={"boss_id": 144, "boss_name": "Articuno", "boss_level": 70, "boss_hp": 5000},
    )
    calls = []

    def original(main_pokemon_level, tracker):
        calls.append(True)
        return ("wild", 0)

    module_globals = {}
    encounter_functions = types.ModuleType("Ankimon.functions.encounter_functions")
    encounter_functions.generate_random_pokemon = original
    sys.modules["Ankimon.functions.encounter_functions"] = encounter_functions

    encounter_module.install_raid_boss_encounter_patch(controller, module_globals)
    patched = module_globals["generate_random_pokemon"]

    result = patched(10, _tracker())

    assert calls == []
    assert result[NAME_INDEX] == "snorlax"
    assert result[TIER_INDEX] == "PvP: gary"

    # With the battle over, the raid boss takes the slot back.
    controller.state["pvp"] = {"matches": []}
    result = patched(10, _tracker())
    assert calls == []
    assert result[TIER_INDEX] == "Raid Boss"

    # And with neither, the normal wild roll runs.
    controller.state["raid"] = {}
    assert patched(10, _tracker()) == ("wild", 0)
    assert calls == [True]


def test_finished_battle_does_not_hold_the_encounter(encounter_module):
    finished = _active_match(status="finished")
    fainted = _active_match(id="match-2")
    fainted["opponent_pokemon"] = dict(fainted["opponent_pokemon"], hp=0)

    controller = _encounter_controller(pvp={"matches": [finished, fainted]})

    assert encounter_module._active_pvp_match_from_controller(controller) is None


# --- Raid reward claiming ---------------------------------------------------


@pytest.fixture
def raid_rewards_module(tmp_dir):
    saved = dict(sys.modules)
    try:
        _stub_package("Ankimon")
        _stub_package("Ankimon.functions")
        _stub_package("Ankimon.pyobj")
        _stub_package("Ankimon.multiplayer")

        resources = types.ModuleType("Ankimon.resources")
        resources.user_path = Path(tmp_dir)
        resources.mypokemon_path = Path(tmp_dir) / "mypokemon.json"
        sys.modules["Ankimon.resources"] = resources

        utils = types.ModuleType("Ankimon.utils")
        utils.get_ev_spread = lambda mode: {
            "hp": 0, "atk": 0, "def": 0, "spa": 0, "spd": 0, "spe": 0
        }
        sys.modules["Ankimon.utils"] = utils

        def _search_pokedex(name, field):
            data = {
                "types": ["ice", "flying"],
                "baseStats": {
                    "hp": 90, "atk": 85, "def": 100, "spa": 95, "spd": 125, "spe": 85
                },
                "abilities": {"0": "Pressure"},
                "actual_id": 144,
            }
            return data.get(field)

        pokedex_functions = types.ModuleType("Ankimon.functions.pokedex_functions")
        pokedex_functions.get_all_pokemon_moves = lambda name, level: ["Peck", "Ice Beam"]
        pokedex_functions.get_base_experience = lambda actual_id: 100
        pokedex_functions.get_growth_rate = lambda boss_id: "medium"
        pokedex_functions.search_pokedex = _search_pokedex
        pokedex_functions.search_pokedex_by_id = lambda boss_id: "articuno"
        sys.modules["Ankimon.functions.pokedex_functions"] = pokedex_functions

        pokemon_functions = types.ModuleType("Ankimon.functions.pokemon_functions")
        pokemon_functions.pick_random_gender = lambda name: "Genderless"
        sys.modules["Ankimon.functions.pokemon_functions"] = pokemon_functions

        pokemon_obj = types.ModuleType("Ankimon.pyobj.pokemon_obj")

        class FakePokemonObject:
            def __init__(self, **kwargs):
                for key, value in kwargs.items():
                    setattr(self, key, value)

            def calculate_max_hp(self):
                return 100

        pokemon_obj.PokemonObject = FakePokemonObject
        sys.modules["Ankimon.pyobj.pokemon_obj"] = pokemon_obj

        yield _load_from_path(
            "Ankimon.multiplayer.raid_rewards",
            os.path.join(ANKIMON, "multiplayer", "raid_rewards.py"),
        )
    finally:
        sys.modules.clear()
        sys.modules.update(saved)


def test_claim_raid_reward_keeps_the_server_reward_level(raid_rewards_module):
    import json

    from Ankimon.resources import mypokemon_path

    # A partial-tier reward sits below the full-tier band; the client
    # must not round it up into full-tier territory.
    message = raid_rewards_module.claim_raid_reward(
        {
            "id": "reward-partial",
            "boss_id": 144,
            "boss_name": "Articuno",
            "reason": "expired",
            "tier": "partial",
            "level": 23,
        }
    )
    assert message is not None

    with open(mypokemon_path, "r", encoding="utf-8") as f:
        caught = json.load(f)
    assert caught[-1]["level"] == 23


def test_claim_raid_reward_grants_pokemon_and_mentions_tier_and_reason(
    raid_rewards_module,
):
    reward = {
        "id": "reward-1",
        "boss_id": 144,
        "boss_name": "Articuno",
        "level": 34,
        "tier": "full",
        "reason": "defeated",
    }

    message = raid_rewards_module.claim_raid_reward(reward)

    assert message is not None
    assert "Articuno" in message
    assert "full" in message


def test_claim_raid_reward_is_idempotent(raid_rewards_module):
    reward = {
        "id": "reward-1",
        "boss_id": 144,
        "boss_name": "Articuno",
        "level": 34,
        "tier": "full",
        "reason": "defeated",
    }

    first = raid_rewards_module.claim_raid_reward(reward)
    second = raid_rewards_module.claim_raid_reward(reward)

    assert first is not None
    assert second is None


# --- api_client: HTTP status handling ----------------------------------------


class FakeResponse:
    def __init__(self, status_code, body=None, content=b"{}"):
        self.status_code = status_code
        self._body = body
        self.content = content
        self.headers = {"Content-Type": "application/json"}

    def json(self):
        if self._body is None:
            raise ValueError("no json")
        return self._body


@pytest.fixture
def api_client_module(tmp_dir):
    """Load the real api_client with `requests` and the credentials path stubbed."""
    saved = dict(sys.modules)
    try:
        _stub_package("Ankimon")
        _stub_package("Ankimon.multiplayer")

        requests_stub = types.ModuleType("requests")

        class RequestException(Exception):
            pass

        requests_stub.exceptions = types.SimpleNamespace(
            RequestException=RequestException
        )
        requests_stub.Session = lambda: types.SimpleNamespace(
            request=lambda *a, **k: None
        )
        sys.modules["requests"] = requests_stub

        credentials_path = os.path.join(tmp_dir, "credentials.json")
        with open(credentials_path, "w", encoding="utf-8") as f:
            json.dump({"username": "ash", "api_key": "key"}, f)
        resources = types.ModuleType("Ankimon.resources")
        resources.user_path_credentials = credentials_path
        sys.modules["Ankimon.resources"] = resources

        yield _load_from_path(
            "Ankimon.multiplayer.api_client",
            os.path.join(ANKIMON, "multiplayer", "api_client.py"),
        )
    finally:
        sys.modules.clear()
        sys.modules.update(saved)


def _client_returning(api_client_module, response):
    client = api_client_module.MultiplayerApiClient(FakeSettings())
    client.session = types.SimpleNamespace(request=lambda *a, **k: response)
    return client


def test_401_is_an_auth_failure(api_client_module):
    client = _client_returning(api_client_module, FakeResponse(401))
    with pytest.raises(api_client_module.MultiplayerAuthError):
        client.get_state()


def test_403_is_not_an_auth_failure(api_client_module):
    # A feature gate ("player battles are coming soon") must not disable
    # multiplayer as if the credentials were wrong.
    response = FakeResponse(403, {"error": "player-vs-player battles are coming soon"})
    client = _client_returning(api_client_module, response)
    with pytest.raises(api_client_module.MultiplayerApiError) as excinfo:
        client.challenge_friend("gary")
    assert not isinstance(excinfo.value, api_client_module.MultiplayerAuthError)
    assert "coming soon" in str(excinfo.value)


def test_409_carries_the_server_message(api_client_module):
    response = FakeResponse(409, {"error": "this raid has already started"})
    client = _client_returning(api_client_module, response)
    with pytest.raises(api_client_module.MultiplayerConflictError) as excinfo:
        client.join_raid("ABC234")
    assert "already started" in str(excinfo.value)
