"""Regression tests for the seedable engine RNG (Phase D, item D1).

``simulate_battle_with_poke_engine`` used to draw its outcome from the
``random`` module's shared generator, so a PvP round could not be reproduced
on the opponent's machine and seeding it would have reseeded the wild-battle
loop's encounters, IVs and reward rolls too. It now takes an optional ``rng``.

These tests pin both halves of that contract: a passed-in generator makes a
turn reproducible *and* leaves the global generator alone, while omitting it
keeps the wild-battle path on the shared generator exactly as before.

See docs/multiplayer-pvp-phase-d.md.
"""

import importlib.util
import os
import random
import sys
import types

import pytest

SRC = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "src"))
ANKIMON = os.path.join(SRC, "Ankimon")


def _stub_package(name, path=None):
    module = types.ModuleType(name)
    module.__path__ = [path] if path else []
    sys.modules[name] = module
    return module


@pytest.fixture
def engine():
    """Load the real engine hook with only its Anki-facing deps stubbed."""
    saved = dict(sys.modules)
    try:
        _stub_package("Ankimon", ANKIMON)
        _stub_package("Ankimon.poke_engine", os.path.join(ANKIMON, "poke_engine"))
        _stub_package("Ankimon.pyobj")

        error_handler = types.ModuleType("Ankimon.pyobj.error_handler")
        error_handler.show_warning_with_traceback = lambda *a, **k: None
        sys.modules["Ankimon.pyobj.error_handler"] = error_handler

        singletons = types.ModuleType("Ankimon.singletons")
        singletons.settings_obj = types.SimpleNamespace(get=lambda key, default=None: default)
        singletons.ankimon_tracker_obj = types.SimpleNamespace(multiplier=1)
        sys.modules["Ankimon.singletons"] = singletons

        name = "Ankimon.poke_engine.ankimon_hooks_to_poke_engine"
        spec = importlib.util.spec_from_file_location(
            name, os.path.join(ANKIMON, "poke_engine", "ankimon_hooks_to_poke_engine.py")
        )
        module = importlib.util.module_from_spec(spec)
        sys.modules[name] = module
        spec.loader.exec_module(module)
        yield module
    finally:
        sys.modules.clear()
        sys.modules.update(saved)


class FakePokemon:
    """The slice of PokemonObject the engine hook actually touches."""

    def __init__(self, engine_pokemon_factory, name, level, attacks):
        self._factory = engine_pokemon_factory
        self.name = name
        self.level = level
        self.attacks = attacks

    def to_poke_engine_Pokemon(self):
        return self._factory(self.name.lower(), self.level, self.attacks)


@pytest.fixture
def make_battle(engine):
    """Build a fresh, identical pair of combatants for each simulation."""
    from Ankimon.poke_engine.objects import Pokemon

    def engine_pokemon(identifier, level, attacks):
        return Pokemon(
            identifier=identifier,
            level=level,
            types=["water"],
            hp=100,
            maxhp=100,
            ability="torrent",
            item=None,
            attack=80,
            defense=80,
            special_attack=80,
            special_defense=80,
            speed=80,
            moves=[{"id": move, "disabled": False, "current_pp": 16} for move in attacks],
        )

    def build():
        attacks = ["watergun", "tackle", "bubble", "growl"]
        return (
            FakePokemon(engine_pokemon, "Squirtle", 20, attacks),
            FakePokemon(engine_pokemon, "Charmander", 20, list(attacks)),
        )

    return build


def _outcome(engine, make_battle, *, rng=None, main_move="watergun", enemy_move="tackle"):
    """Run one turn and reduce it to a comparable summary."""
    main_pokemon, enemy_pokemon = make_battle()
    results = engine.simulate_battle_with_poke_engine(
        main_pokemon, enemy_pokemon, main_move, enemy_move, 1, None, rng
    )
    # (damage taken, damage dealt, resulting state repr) is enough to detect a
    # different outcome having been drawn.
    return results[2], results[3], repr(results[1])


def test_seeded_rng_reproduces_the_same_turn(engine, make_battle):
    first = _outcome(engine, make_battle, rng=random.Random(20260827))
    second = _outcome(engine, make_battle, rng=random.Random(20260827))
    assert first == second


def test_different_seeds_draw_different_outcomes(engine, make_battle):
    # Guards the test above from going vacuous: if the turn ever stopped
    # depending on the rng at all, "same seed reproduces" would pass for the
    # wrong reason.
    outcomes = {_outcome(engine, make_battle, rng=random.Random(seed))[:2] for seed in range(12)}
    assert len(outcomes) > 1


def test_seeded_rng_does_not_disturb_the_global_generator(engine, make_battle):
    # This is the bug the rng parameter exists to prevent: a PvP round must not
    # reseed the generator the wild-battle loop, encounters and IVs draw from.
    random.seed(4242)
    before = random.getstate()
    _outcome(engine, make_battle, rng=random.Random(1))
    assert random.getstate() == before


def test_omitting_rng_keeps_using_the_shared_generator(engine, make_battle):
    # The wild-battle path passes no rng, so the shared generator must still
    # drive the draw - two runs from the same global seed stay identical.
    random.seed(99)
    first = _outcome(engine, make_battle)
    random.seed(99)
    second = _outcome(engine, make_battle)
    assert first == second

    # ...and the call must actually consume from it, not from somewhere else.
    random.seed(99)
    state_before = random.getstate()
    _outcome(engine, make_battle)
    assert random.getstate() != state_before


def test_outcome_draw_is_independent_of_generation_order(engine, make_battle):
    """Item D2: the same seed must pick the same outcome even if the engine
    hands back the possibilities in a different order.

    Shuffling the engine's return value is the only way to simulate the
    reordering this guards against - the engine's own order is stable today,
    which is exactly why a regression in it would otherwise go unnoticed until
    two clients disagreed mid-match.
    """
    real = engine.get_all_state_instructions

    def reversed_outcomes(mutator, user_move, opponent_move):
        return list(reversed(real(mutator, user_move, opponent_move)))

    baseline = _outcome(engine, make_battle, rng=random.Random(31337))

    engine.get_all_state_instructions = reversed_outcomes
    try:
        reordered = _outcome(engine, make_battle, rng=random.Random(31337))
    finally:
        engine.get_all_state_instructions = real

    assert reordered == baseline


def test_canonical_outcome_key_is_a_total_order(engine):
    outcomes = [
        types.SimpleNamespace(instructions=[["damage", "opponent", 7]], percentage=0.5),
        types.SimpleNamespace(instructions=[["damage", "opponent", 6]], percentage=0.5),
        types.SimpleNamespace(instructions=[["damage", "opponent", 6]], percentage=0.2),
    ]
    ordered = sorted(outcomes, key=engine.canonical_outcome_key)
    assert [(o.instructions[0][2], o.percentage) for o in ordered] == [(6, 0.2), (6, 0.5), (7, 0.5)]
    # Sorting an already-sorted list must not move anything.
    assert sorted(ordered, key=engine.canonical_outcome_key) == ordered


def test_missing_moves_are_drawn_from_the_passed_rng(engine, make_battle):
    """The move fallback is a draw too, so it must honour the same generator."""
    calls = []

    class RecordingRandom(random.Random):
        def choice(self, seq):
            calls.append(tuple(seq))
            return super().choice(seq)

    _outcome(engine, make_battle, rng=RecordingRandom(7), main_move=None, enemy_move=None)
    assert len(calls) == 2, "both sides' move fallbacks should use the passed rng"
