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
