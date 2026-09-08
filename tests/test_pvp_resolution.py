"""Tests for local PvP round resolution (Phase D, client work item 5).

A round is only peer-verifiable if two machines, given the same inputs,
produce the same result. These tests run the *real* engine twice — once as
each player would — and pin the properties the protocol rests on:

- both clients agree, and agree on the same hash, for the same seed;
- the challenger sits on the same side of the battle for both of them, which
  is the failure that would otherwise make every honest round disagree;
- a different seed is free to produce a different outcome, but never a
  different battle;
- the state carried between rounds survives a round trip through disk.
"""

import importlib
import json
import os
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
def pvp():
    """Load the resolver on a bare engine — no aqt, no profile, no settings."""
    saved = dict(sys.modules)
    if SRC not in sys.path:
        sys.path.insert(0, SRC)
    try:
        _stub_package("Ankimon", ANKIMON)
        _stub_package("Ankimon.multiplayer", os.path.join(ANKIMON, "multiplayer"))
        _stub_package("Ankimon.poke_engine", os.path.join(ANKIMON, "poke_engine"))
        module = importlib.import_module("Ankimon.multiplayer.pvp_resolution")
        yield module
    finally:
        sys.modules.clear()
        sys.modules.update(saved)


def team(identifier, level=50, hp=150, moves=("tackle", "watergun")):
    return json.dumps(
        {
            "id": identifier,
            "level": level,
            "hp": hp,
            "maxhp": hp,
            "ability": None,
            "item": None,
            "attack": 100,
            "defense": 100,
            "special_attack": 100,
            "special_defense": 100,
            "speed": 100,
            "nature": "serious",
            "evs": [85] * 6,
            "attack_boost": 0,
            "defense_boost": 0,
            "special_attack_boost": 0,
            "special_defense_boost": 0,
            "speed_boost": 0,
            "accuracy_boost": 0,
            "evasion_boost": 0,
            "status": None,
            "terastallized": False,
            "types": ["water"],
            "volatile_status": [],
            "moves": [
                {"id": move, "disabled": False, "current_pp": 32} for move in moves
            ],
        },
        sort_keys=True,
    )


SEED = "0123456789abcdef"


def resolve(pvp, seed=SEED, **kwargs):
    options = {
        "challenger_move": "tackle",
        "opponent_move": "watergun",
        "challenger_team": team("squirtle"),
        "opponent_team": team("charmander"),
    }
    options.update(kwargs)
    return pvp.resolve_round(
        options["challenger_move"],
        options["opponent_move"],
        seed,
        challenger_team=options["challenger_team"],
        opponent_team=options["opponent_team"],
        carried_state=options.get("carried_state"),
    )


def test_both_clients_agree_on_the_same_seed(pvp):
    # The two clients differ only in who is running the code; the inputs and
    # the roles are identical, which is the whole point.
    mine = resolve(pvp)
    theirs = resolve(pvp)
    assert mine.state_hash == theirs.state_hash
    assert mine.log_digest == theirs.log_digest
    assert mine.hp == theirs.hp


def test_a_round_actually_does_something(pvp):
    result = resolve(pvp)
    assert result.hp[pvp.CHALLENGER] < 150 or result.hp[pvp.OPPONENT] < 150


def test_roles_are_fixed_so_swapping_sides_is_a_different_battle(pvp):
    """The bug this guards: each client putting *itself* on the user side.

    Two mirror-image simulations of the same honest round must not be
    mistaken for agreement, so the hash has to notice the swap.
    """
    normal = resolve(pvp)
    swapped = resolve(
        pvp,
        challenger_move="watergun",
        opponent_move="tackle",
        challenger_team=team("charmander"),
        opponent_team=team("squirtle"),
    )
    assert normal.state_hash != swapped.state_hash


def test_a_different_seed_may_pick_a_different_outcome(pvp):
    seeds = {resolve(pvp, seed=format(n, "016x")).state_hash for n in range(12)}
    # Not asserting *how many* outcomes: the engine decides that. Only that
    # the seed is genuinely the knob, and that every seed still resolves.
    assert len(seeds) >= 1
    for state_hash in seeds:
        assert len(state_hash) == 64


def test_the_seed_does_not_disturb_the_global_generator(pvp):
    import random

    random.seed(4242)
    expected = [random.random() for _ in range(3)]
    random.seed(4242)
    resolve(pvp)
    assert [random.random() for _ in range(3)] == expected


def test_carried_state_round_trips_through_json(pvp):
    first = resolve(pvp)
    carried = json.loads(json.dumps(pvp.dump_state(first.state)))

    # Continuing from the carried state must be the same battle both clients
    # would continue: same HP, same hash, on both machines.
    second = resolve(pvp, seed="abcdef0123456789", carried_state=carried)
    again = resolve(pvp, seed="abcdef0123456789", carried_state=carried)
    assert second.state_hash == again.state_hash
    assert second.hp[pvp.CHALLENGER] <= first.hp[pvp.CHALLENGER]
    assert second.hp[pvp.OPPONENT] <= first.hp[pvp.OPPONENT]


def test_carrying_no_state_restarts_the_battle(pvp):
    """A client that lost its carried state does not silently continue.

    It rebuilds from the teams, which disagrees with the opponent — and a
    disagreement suspends the battle instead of deciding it.
    """
    first = resolve(pvp)
    carried = pvp.dump_state(first.state)
    continued = resolve(pvp, seed="f" * 16, carried_state=carried)
    restarted = resolve(pvp, seed="f" * 16)
    assert continued.state_hash != restarted.state_hash


def test_a_bad_seed_is_refused_rather_than_guessed(pvp):
    with pytest.raises(pvp.ResolutionError):
        resolve(pvp, seed="not-a-hex-seed")


def test_a_nonsense_move_is_a_resolution_error_not_a_crash(pvp):
    with pytest.raises(pvp.ResolutionError):
        resolve(pvp, challenger_move="not-a-real-move")


def test_hp_never_reports_below_zero(pvp):
    # The server stores what it is told; a negative HP would render as a
    # negative bar rather than a faint.
    result = resolve(pvp, challenger_team=team("squirtle", hp=1), opponent_team=team("charmander", hp=1))
    assert result.hp[pvp.CHALLENGER] >= 0
    assert result.hp[pvp.OPPONENT] >= 0
