"""Tests for the canonical state hash (Phase D, item D4).

The hash is what two clients compare to agree on a round, so the properties
worth pinning are: equal-in-play states hash equal regardless of how the
dicts and sets were built, and anything that changes the outcome changes the
hash. A hash that quietly ignores a field is a field a cheating client can
edit for free.
"""

import copy
import importlib.util
import os
import sys
import types
from collections import defaultdict

import pytest

SRC = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "src"))
ANKIMON = os.path.join(SRC, "Ankimon")


def _stub_package(name, path=None):
    module = types.ModuleType(name)
    module.__path__ = [path] if path else []
    sys.modules[name] = module
    return module


@pytest.fixture
def round_hash():
    """Load the module directly - Ankimon.multiplayer.__init__ needs aqt."""
    saved = dict(sys.modules)
    try:
        _stub_package("Ankimon", ANKIMON)
        _stub_package("Ankimon.multiplayer", os.path.join(ANKIMON, "multiplayer"))
        name = "Ankimon.multiplayer.round_hash"
        spec = importlib.util.spec_from_file_location(
            name, os.path.join(ANKIMON, "multiplayer", "round_hash.py")
        )
        module = importlib.util.module_from_spec(spec)
        sys.modules[name] = module
        spec.loader.exec_module(module)
        yield module
    finally:
        sys.modules.clear()
        sys.modules.update(saved)


class FakePokemon:
    def __init__(self, identifier="squirtle", **overrides):
        self.id = identifier
        self.level = 20
        self.types = ["water"]
        self.hp = 84
        self.maxhp = 100
        self.ability = "torrent"
        self.item = None
        self.attack = 80
        self.defense = 81
        self.special_attack = 82
        self.special_defense = 83
        self.speed = 84
        self.nature = "serious"
        self.evs = (85,) * 6
        self.attack_boost = 0
        self.defense_boost = 0
        self.special_attack_boost = 0
        self.special_defense_boost = 0
        self.speed_boost = 0
        self.accuracy_boost = 0
        self.evasion_boost = 0
        self.status = None
        self.terastallized = False
        self.volatile_status = {"substitute", "confusion"}
        self.moves = [
            {"id": "watergun", "disabled": False, "current_pp": 16},
            {"id": "tackle", "disabled": False, "current_pp": 32},
        ]
        for key, value in overrides.items():
            setattr(self, key, value)


class FakeSide:
    def __init__(self, active=None, side_conditions=None, reserve=None):
        self.active = active or FakePokemon()
        self.reserve = reserve or {}
        self.wish = (0, 0)
        self.future_sight = (0, 0)
        self.side_conditions = side_conditions if side_conditions is not None else defaultdict(int)


class FakeState:
    def __init__(self, user=None, opponent=None, weather=None, field=None, trick_room=False):
        self.user = user or FakeSide()
        self.opponent = opponent or FakeSide(FakePokemon("charmander", types=["fire"]))
        self.weather = weather
        self.field = field
        self.trick_room = trick_room


def test_identical_states_hash_equal(round_hash):
    assert round_hash.state_hash(FakeState()) == round_hash.state_hash(FakeState())


def test_set_and_dict_insertion_order_do_not_matter(round_hash):
    a = FakeState()
    b = FakeState()
    # Same content, built in the opposite order - a raw repr would differ.
    b.user.active.volatile_status = set()
    for value in ["confusion", "substitute"]:
        b.user.active.volatile_status.add(value)
    b.user.side_conditions = {"spikes": 2, "stealthrock": 1}
    a.user.side_conditions = {"stealthrock": 1, "spikes": 2}
    assert round_hash.state_hash(a) == round_hash.state_hash(b)


def test_read_but_unset_side_conditions_are_ignored(round_hash):
    # A defaultdict(int) grows keys just by being read, so one client can end a
    # turn holding 'tailwind': 0 where the other holds nothing. Same battle.
    touched = FakeState()
    conditions = defaultdict(int)
    _ = conditions["tailwind"]
    _ = conditions["reflect"]
    touched.user.side_conditions = conditions
    assert round_hash.state_hash(touched) == round_hash.state_hash(FakeState())


def test_a_real_side_condition_still_changes_the_hash(round_hash):
    with_rock = FakeState()
    with_rock.user.side_conditions = defaultdict(int, {"stealthrock": 1})
    assert round_hash.state_hash(with_rock) != round_hash.state_hash(FakeState())


@pytest.mark.parametrize(
    "mutate",
    [
        pytest.param(lambda s: setattr(s.user.active, "hp", 83), id="hp"),
        pytest.param(lambda s: setattr(s.user.active, "status", "brn"), id="status"),
        pytest.param(lambda s: setattr(s.user.active, "attack_boost", 1), id="boost"),
        pytest.param(lambda s: setattr(s.user.active, "item", "leftovers"), id="item"),
        pytest.param(lambda s: setattr(s.user.active, "ability", "raindish"), id="ability"),
        pytest.param(lambda s: s.user.active.volatile_status.add("taunt"), id="volatile"),
        pytest.param(lambda s: setattr(s, "weather", "raindance"), id="weather"),
        pytest.param(lambda s: setattr(s, "trick_room", True), id="trick_room"),
        pytest.param(lambda s: setattr(s.user, "wish", (2, 50)), id="wish"),
        pytest.param(
            lambda s: s.user.active.moves.__setitem__(
                0, {"id": "watergun", "disabled": True, "current_pp": 16}
            ),
            id="move_disabled",
        ),
        pytest.param(
            lambda s: setattr(s.user.active, "moves", list(reversed(s.user.active.moves))),
            id="move_slot_order",
        ),
        pytest.param(
            lambda s: setattr(s.opponent.active, "hp", 12), id="opponent_hp"
        ),
    ],
)
def test_outcome_relevant_changes_change_the_hash(round_hash, mutate):
    baseline = FakeState()
    changed = FakeState()
    mutate(changed)
    assert round_hash.state_hash(changed) != round_hash.state_hash(baseline)


def test_sides_are_not_interchangeable(round_hash):
    # Swapping the two sides must not hash the same, or a client could report
    # its opponent's win as its own.
    swapped = FakeState()
    swapped.user, swapped.opponent = swapped.opponent, swapped.user
    assert round_hash.state_hash(swapped) != round_hash.state_hash(FakeState())


def test_unknown_field_types_raise_rather_than_being_dropped(round_hash):
    state = FakeState()
    state.user.active.item = object()
    with pytest.raises(round_hash.UnhashableStateError):
        round_hash.state_hash(state)


def test_floats_are_rounded_to_a_fixed_precision(round_hash):
    a = FakeState()
    b = FakeState()
    a.user.active.burn_multiplier = 0.5
    b.user.active.burn_multiplier = 0.5 + 1e-12
    assert round_hash.state_hash(a) == round_hash.state_hash(b)

    c = FakeState()
    c.user.active.burn_multiplier = 0.5001
    assert round_hash.state_hash(c) != round_hash.state_hash(a)


def test_canonical_json_is_stable_and_compact(round_hash):
    text = round_hash.canonical_json(FakeState())
    assert text == round_hash.canonical_json(copy.deepcopy(FakeState()))
    assert ", " not in text and '": ' not in text, "separators must stay compact"
