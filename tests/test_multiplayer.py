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
                pass

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
        "pvp": {"human_enabled": False, "tokens": 2, "token_progress": 4, "matches": []},
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
    assert "ankimon-mp-raid" in css


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


# --- Peer-verified round poller ------------------------------------------


class SyncTaskman:
    """Runs background work inline, so a test can see the whole chain."""

    def __init__(self):
        self.errors = []

    def run_in_background(self, task, on_done):
        class Future:
            def __init__(self, fn):
                self._fn = fn

            def result(self):
                return self._fn()

        on_done(Future(task))


class FakeResolution:
    """Stands in for the engine: records inputs, returns a fixed result."""

    def __init__(self):
        self.calls = []
        self.CHALLENGER = "challenger"
        self.OPPONENT = "opponent"

        class ResolutionError(ValueError):
            pass

        self.ResolutionError = ResolutionError

    def resolve_round(self, challenger_move, opponent_move, seed, **kwargs):
        self.calls.append(
            {
                "challenger_move": challenger_move,
                "opponent_move": opponent_move,
                "seed": seed,
                **kwargs,
            }
        )
        return types.SimpleNamespace(
            state="engine-state",
            hp={"challenger": 71, "opponent": 62},
            state_hash="deadbeef",
            log_digest="cafe",
        )

    def dump_state(self, state):
        return {"carried": state}


def _resolving_controller(module, monkeypatch, match, submit_result=None):
    controller = _make_controller(module)
    resolution = FakeResolution()
    module.mw.taskman = SyncTaskman()
    submissions = []

    def submit_round_result(match_id, round_number, state_hash, hp_after, **kwargs):
        submissions.append(
            {
                "match_id": match_id,
                "round": round_number,
                "state_hash": state_hash,
                "hp_after": hp_after,
                **kwargs,
            }
        )
        return submit_result or {}

    controller.api.submit_round_result = submit_round_result
    controller._pvp_modules = lambda: (resolution, None)
    controller._engine_version = lambda: "1.0+abc"
    controller.state = {"pvp": {"matches": [match]}}
    return controller, resolution, submissions


def _open_match(**overrides):
    match = {
        "id": "m1",
        "status": "active",
        "opponent": "gary",
        "you_are_challenger": True,
        "your_team": "ash-team",
        "opponent_team": "gary-team",
        "resolution": {
            "round": 1,
            "attempt": 1,
            "seed": "0f0f0f0f0f0f0f0f",
            "moves": {"ash": "ember", "gary": "tackle"},
            "you_submitted": False,
        },
    }
    match.update(overrides)
    return match


def test_open_round_is_simulated_and_reported(multiplayer_module, monkeypatch):
    controller, resolution, submissions = _resolving_controller(
        multiplayer_module, monkeypatch, _open_match()
    )
    controller._resolve_open_rounds()

    assert len(resolution.calls) == 1
    call = resolution.calls[0]
    assert call["challenger_move"] == "ember" and call["opponent_move"] == "tackle"
    assert call["challenger_team"] == "ash-team"
    assert call["opponent_team"] == "gary-team"

    assert len(submissions) == 1
    submitted = submissions[0]
    assert submitted["round"] == 1
    assert submitted["state_hash"] == "deadbeef"
    # HP goes up keyed by username, so the server can check both sides.
    assert submitted["hp_after"] == {"ash": 71, "gary": 62}
    assert submitted["engine_version"] == "1.0+abc"


def test_the_challenger_stays_on_the_challenger_side(multiplayer_module, monkeypatch):
    # Ash answered the challenge this time, so gary's team and move are the
    # challenger's — the same battle both clients must build.
    controller, resolution, submissions = _resolving_controller(
        multiplayer_module, monkeypatch, _open_match(you_are_challenger=False)
    )
    controller._resolve_open_rounds()

    call = resolution.calls[0]
    assert call["challenger_move"] == "tackle" and call["opponent_move"] == "ember"
    assert call["challenger_team"] == "gary-team"
    assert call["opponent_team"] == "ash-team"
    assert submissions[0]["hp_after"] == {"gary": 71, "ash": 62}


def test_a_round_this_client_already_reported_is_left_alone(multiplayer_module, monkeypatch):
    match = _open_match()
    match["resolution"]["you_submitted"] = True
    controller, resolution, submissions = _resolving_controller(
        multiplayer_module, monkeypatch, match
    )
    controller._resolve_open_rounds()
    assert resolution.calls == [] and submissions == []


def test_a_round_is_only_simulated_once_across_polls(multiplayer_module, monkeypatch):
    controller, resolution, submissions = _resolving_controller(
        multiplayer_module, monkeypatch, _open_match()
    )
    controller._resolve_open_rounds()
    controller._resolve_open_rounds()
    assert len(resolution.calls) == 1


def test_a_replay_is_new_work(multiplayer_module, monkeypatch):
    match = _open_match()
    controller, resolution, submissions = _resolving_controller(
        multiplayer_module, monkeypatch, match
    )
    controller._resolve_open_rounds()
    # The server replayed the round with a fresh seed: same round, new
    # attempt, and this client has not reported *that* attempt.
    match["resolution"]["attempt"] = 2
    match["resolution"]["seed"] = "1111111111111111"
    controller._resolve_open_rounds()
    assert [call["seed"] for call in resolution.calls] == [
        "0f0f0f0f0f0f0f0f",
        "1111111111111111",
    ]


def test_the_battle_state_is_carried_to_the_next_round(multiplayer_module, monkeypatch):
    controller, resolution, submissions = _resolving_controller(
        multiplayer_module, monkeypatch, _open_match()
    )
    controller._resolve_open_rounds()
    carried = controller.state["pvp_battle_states"]["m1"]
    assert carried == {"after_round": 1, "state": {"carried": "engine-state"}}

    # Round 2 continues from it rather than restarting the battle.
    match = _open_match()
    match["resolution"]["round"] = 2
    controller.state["pvp"]["matches"] = [match]
    controller._resolve_open_rounds()
    assert resolution.calls[-1]["carried_state"] == {"carried": "engine-state"}


def test_a_settled_match_is_not_resolved(multiplayer_module, monkeypatch):
    controller, resolution, _ = _resolving_controller(
        multiplayer_module, monkeypatch, _open_match(status="suspended")
    )
    controller._resolve_open_rounds()
    assert resolution.calls == []


def test_an_engine_failure_is_logged_and_not_reported(multiplayer_module, monkeypatch):
    controller, resolution, submissions = _resolving_controller(
        multiplayer_module, monkeypatch, _open_match()
    )

    def boom(*args, **kwargs):
        raise resolution.ResolutionError("no outcome")

    resolution.resolve_round = boom
    controller._resolve_open_rounds()
    # Reporting a guess would be worse than reporting nothing: an
    # unconfirmed round stalls, it does not decide the battle.
    assert submissions == []
    assert any(level == "warning" for level, _ in controller.logger.entries)


def _toast_for_match_change(module, old_match, new_match):
    controller = _make_controller(module)
    controller.state = {"pvp": {"matches": [old_match]}}
    controller._derive_toasts(
        {"pvp": {"matches": [old_match]}}, {"pvp": {"matches": [new_match]}}
    )
    return controller.drain_toast() or ""


def test_toast_explains_a_suspended_battle(multiplayer_module):
    # No winner and no explanation reads as the addon losing the match.
    message = _toast_for_match_change(
        multiplayer_module,
        _open_match(),
        _open_match(
            status="suspended",
            resolution=None,
            suspended_reason="the two players' battle results kept disagreeing",
        ),
    )
    assert "suspended" in message
    assert "kept disagreeing" in message
    assert "No winner" in message


def test_toast_says_a_replayed_round_cost_nothing(multiplayer_module):
    replayed = _open_match()
    replayed["resolution"] = dict(replayed["resolution"], attempt=2)
    message = _toast_for_match_change(multiplayer_module, _open_match(), replayed)
    assert "replayed" in message and "no damage" in message


def test_toast_names_the_player_a_stalled_battle_is_waiting_on(multiplayer_module):
    message = _toast_for_match_change(
        multiplayer_module, _open_match(), _open_match(status="stalled")
    )
    assert "gary" in message and "on hold" in message


def test_the_round_result_carries_the_ending_state(multiplayer_module, monkeypatch):
    controller, resolution, submissions = _resolving_controller(
        multiplayer_module, monkeypatch, _open_match()
    )
    controller._resolve_open_rounds()
    # Reported so the server can hand it back next round; it is kept there
    # only if the opponent reported the same one.
    assert submissions[0]["state"] == {"carried": "engine-state"}


def test_the_servers_carried_state_wins_over_the_local_cache(multiplayer_module, monkeypatch):
    match = _open_match()
    match["resolution"]["round"] = 2
    match["resolution"]["carried_state"] = {"carried": "from-server"}
    controller, resolution, _ = _resolving_controller(
        multiplayer_module, monkeypatch, match
    )
    # A stale local copy must not be preferred: the server's is the one both
    # clients agreed on, so it is the one the opponent is simulating from.
    controller.state["pvp_battle_states"] = {
        "m1": {"after_round": 1, "state": {"carried": "stale-local"}}
    }
    controller._resolve_open_rounds()
    assert resolution.calls[0]["carried_state"] == {"carried": "from-server"}


def test_the_local_cache_is_used_when_the_server_carries_nothing(multiplayer_module, monkeypatch):
    match = _open_match()
    match["resolution"]["round"] = 2
    controller, resolution, _ = _resolving_controller(
        multiplayer_module, monkeypatch, match
    )
    controller.state["pvp_battle_states"] = {
        "m1": {"after_round": 1, "state": {"carried": "local"}}
    }
    controller._resolve_open_rounds()
    assert resolution.calls[0]["carried_state"] == {"carried": "local"}


def test_finished_battles_stop_taking_up_cache(multiplayer_module, monkeypatch):
    controller, _, _ = _resolving_controller(
        multiplayer_module, monkeypatch, _open_match(status="finished", resolution=None)
    )
    controller.state["pvp_battle_states"] = {"m1": {"after_round": 4, "state": {}}}
    controller._resolve_open_rounds()
    assert controller.state["pvp_battle_states"] == {}
