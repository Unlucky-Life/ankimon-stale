"""Regression tests for the battle message / status / stat handling.

These load the modules under test directly from their file path with stub
parent packages, so they run without a live Anki install (importing the real
``Ankimon`` package executes the whole addon and needs ``aqt``).
"""

import importlib.util
import os
import sys
import types

import pytest

SRC = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "src"))
ANKIMON = os.path.join(SRC, "Ankimon")


class RecordingErrorHandler:
    """Stand-in for pyobj.error_handler.show_warning_with_traceback."""

    def __init__(self):
        self.calls = []

    def __call__(self, parent=None, exception=None, message="An error occurred."):
        if not exception:
            # Same contract as the real handler - a positional misuse must fail here.
            raise ValueError("An exception must be provided.")
        self.calls.append((exception, message))


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


@pytest.fixture
def battle_functions():
    """Load Ankimon.functions.battle_functions with its Anki-dependent deps stubbed."""
    saved = dict(sys.modules)
    try:
        _stub_package("Ankimon")
        _stub_package("Ankimon.functions")
        _stub_package("Ankimon.pyobj")
        _stub_package("Ankimon.poke_engine")

        _load_from_path(
            "Ankimon.poke_engine.constants",
            os.path.join(ANKIMON, "poke_engine", "constants.py"),
        )

        error_handler = types.ModuleType("Ankimon.pyobj.error_handler")
        error_handler.show_warning_with_traceback = RecordingErrorHandler()
        sys.modules["Ankimon.pyobj.error_handler"] = error_handler

        move_names = types.ModuleType("Ankimon.move_names")
        move_names.format_move_name = lambda move: str(move).replace("-", " ").title()
        sys.modules["Ankimon.move_names"] = move_names

        module = _load_from_path(
            "Ankimon.functions.battle_functions",
            os.path.join(ANKIMON, "functions", "battle_functions.py"),
        )
        module.TEST_ERROR_HANDLER = error_handler.show_warning_with_traceback
        yield module
    finally:
        sys.modules.clear()
        sys.modules.update(saved)


@pytest.fixture
def pokemon_obj_module():
    """Load Ankimon.pyobj.pokemon_obj with its heavy deps stubbed out."""
    saved = dict(sys.modules)
    try:
        _stub_package("Ankimon")
        _stub_package("Ankimon.functions")
        _stub_package("Ankimon.pyobj")
        _stub_package("Ankimon.poke_engine")

        sprite_functions = types.ModuleType("Ankimon.functions.sprite_functions")
        sprite_functions.get_sprite_path = lambda *a, **k: ""
        sys.modules["Ankimon.functions.sprite_functions"] = sprite_functions

        objects = types.ModuleType("Ankimon.poke_engine.objects")
        objects.Pokemon = object
        sys.modules["Ankimon.poke_engine.objects"] = objects

        resources = types.ModuleType("Ankimon.resources")
        resources.pkmnimgfolder = ""
        resources.mainpokemon_path = ""
        resources.mypokemon_path = ""
        sys.modules["Ankimon.resources"] = resources

        utils = types.ModuleType("Ankimon.utils")
        utils.substract_item_from_itembag = lambda *a, **k: None
        utils.give_item = lambda *a, **k: None
        sys.modules["Ankimon.utils"] = utils

        yield _load_from_path(
            "Ankimon.pyobj.pokemon_obj",
            os.path.join(ANKIMON, "pyobj", "pokemon_obj.py"),
        )
    finally:
        sys.modules.clear()
        sys.modules.update(saved)


class FakeTranslator:
    """Renders a translation key plus its arguments, so tests can assert on both."""

    def translate(self, key, **kwargs):
        if not kwargs:
            return key
        args = " ".join(f"{k}={v}" for k, v in sorted(kwargs.items()))
        return f"{key}[{args}]"


class FakePokemon:
    def __init__(self, name="pikachu", hp=100, battle_status="fighting"):
        self.name = name
        self.hp = hp
        self.battle_status = battle_status
        self.volatile_status = set()


def _message(battle_functions, **overrides):
    kwargs = dict(
        battle_info={"instructions": []},
        multiplier=1.0,
        main_pokemon=FakePokemon("pikachu"),
        enemy_pokemon=FakePokemon("bulbasaur"),
        user_attack="thunderbolt",
        enemy_attack="tackle",
        dmg_from_user_move=10,
        dmg_from_enemy_move=5,
        user_hp_after=95,
        opponent_hp_after=90,
        battle_status="fighting",
        pokemon_encounter=1,
        translator=FakeTranslator(),
        changes=None,
    )
    kwargs.update(overrides)
    return battle_functions.process_battle_data(**kwargs)


# --- process_battle_data: "splash" means "did not attack" ---------------------


def test_both_attacks_are_announced(battle_functions):
    message = _message(battle_functions)
    assert "enemy_attack_announcement" in message
    assert "player_attack_announcement" in message


def test_enemy_splash_is_not_announced_as_an_attack(battle_functions):
    message = _message(battle_functions, enemy_attack="splash")
    assert "enemy_attack_announcement" not in message
    assert "player_attack_announcement" in message


def test_user_splash_is_not_announced_as_an_attack(battle_functions):
    message = _message(battle_functions, user_attack="splash")
    assert "player_attack_announcement" not in message
    assert "enemy_attack_announcement" in message


def test_status_replaces_the_user_attack_announcement(battle_functions):
    message = _message(battle_functions, battle_status="slp")
    assert "pokemon_is_sleeping" in message
    assert "player_attack_announcement" not in message


def test_status_is_reported_even_without_a_usable_move(battle_functions):
    message = _message(battle_functions, battle_status="slp", user_attack="splash")
    assert "pokemon_is_sleeping" in message


# --- split_damage_and_heals ---------------------------------------------------


def test_positive_damage_is_left_alone(battle_functions):
    assert battle_functions.split_damage_and_heals(30, 0) == (30, 0)


def test_negative_damage_becomes_a_heal(battle_functions):
    assert battle_functions.split_damage_and_heals(-12, 0) == (0, 12)


def test_negative_damage_adds_to_existing_heals(battle_functions):
    assert battle_functions.split_damage_and_heals(-12, 5) == (0, 17)


def test_existing_negative_heals_survive(battle_functions):
    # Life Orb style recoil is reported as a negative heal and must stay negative.
    assert battle_functions.split_damage_and_heals(10, -8) == (10, -8)


# --- update_pokemon_battle_status --------------------------------------------


def test_status_application_is_written_to_both_sides(battle_functions):
    enemy, main = FakePokemon("bulbasaur"), FakePokemon("pikachu")
    battle_info = {
        "instructions": [
            ["apply_status", "opponent", "brn"],
            ["apply_status", "user", "par"],
        ]
    }
    enemy_changed, main_changed = battle_functions.update_pokemon_battle_status(
        battle_info, enemy, main
    )
    assert (enemy_changed, main_changed) == (True, True)
    assert enemy.battle_status == "brn"
    assert main.battle_status == "par"


def test_fainted_status_clears_volatiles(battle_functions):
    enemy = FakePokemon("bulbasaur", hp=0)
    enemy.volatile_status = {"confusion"}
    main = FakePokemon("pikachu")
    enemy_changed, _ = battle_functions.update_pokemon_battle_status(
        {"instructions": [["apply_status", "opponent", "brn"]]}, enemy, main
    )
    assert enemy_changed is True
    assert enemy.battle_status == "fainted"
    assert enemy.volatile_status == set()


def test_internal_error_is_reported_not_re_raised(battle_functions):
    class ExplodingPokemon(FakePokemon):
        @property
        def battle_status(self):
            raise RuntimeError("boom")

        @battle_status.setter
        def battle_status(self, value):
            raise RuntimeError("boom")

    enemy = ExplodingPokemon.__new__(ExplodingPokemon)
    enemy.name = "bulbasaur"
    enemy.hp = 100
    enemy.volatile_status = set()
    main = FakePokemon("pikachu")

    result = battle_functions.update_pokemon_battle_status(
        {"instructions": [["apply_status", "opponent", "brn"]]}, enemy, main
    )

    assert result == (False, False)
    # The handler must have received the real exception - passing it positionally
    # made it land on `parent`, which raised ValueError out of the except block.
    calls = battle_functions.TEST_ERROR_HANDLER.calls
    assert len(calls) == 1
    assert isinstance(calls[0][0], RuntimeError)


# --- PokemonObject._update_battle_stats --------------------------------------


def _bare_pokemon(pokemon_obj_module):
    """A PokemonObject with just enough state for the derived `stats` property."""
    pokemon = pokemon_obj_module.PokemonObject.__new__(
        pokemon_obj_module.PokemonObject
    )
    pokemon.base_stats = {"hp": 45, "atk": 49, "def": 49, "spa": 65, "spd": 65, "spe": 45}
    pokemon.level = 50
    pokemon.nature = "hardy"
    pokemon.iv = {"hp": 31, "atk": 31, "def": 31, "spa": 31, "spd": 31, "spe": 31}
    pokemon.ev = {"hp": 4, "atk": 0, "def": 0, "spa": 0, "spd": 0, "spe": 2}
    return pokemon


def test_battle_stats_are_the_real_stats_not_the_evs(pokemon_obj_module):
    pokemon = _bare_pokemon(pokemon_obj_module)

    pokemon._update_battle_stats()

    assert pokemon._battle_stats == pokemon.stats
    # The old merge loop wrote stats, then ivs, then evs onto the same keys,
    # so the result was always just a copy of the EVs.
    assert pokemon._battle_stats != pokemon.ev
    assert pokemon._battle_stats != pokemon.iv


def test_battle_stats_are_isolated_from_the_source_stats(pokemon_obj_module):
    pokemon = _bare_pokemon(pokemon_obj_module)

    pokemon._update_battle_stats()
    original_speed = pokemon.stats["spe"]
    pokemon._battle_stats["spe"] = original_speed // 2  # e.g. paralysis speed drop

    assert pokemon.stats["spe"] == original_speed
