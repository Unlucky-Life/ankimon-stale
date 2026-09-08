"""Canonical serialization of a battle state, and its hash.

Phase D resolves a PvP round by having both clients simulate it and report a
hash of the result; the server accepts the round only if the two hashes
agree (see docs/multiplayer-pvp-phase-d.md, item D4).

That only works if two machines that computed the *same* outcome always
produce the same bytes. So this module does not hash `repr(state)`. It walks
the engine's state explicitly and emits a canonical form:

- only fields that affect a turn's outcome — no timestamps, no usernames, no
  sprite paths, nothing display-only;
- sets sorted into lists, mapping keys sorted, so iteration order cannot
  leak in;
- integers kept as integers, and floats rounded to a fixed precision, so two
  builds cannot disagree in the last bit of a value that is equal in every
  way that matters;
- zero-valued side conditions dropped, since a `defaultdict(int)` that has
  been *read* holds keys one that has not been read does not, and that
  difference is invisible in play.

Anything this module cannot interpret is a bug, not something to paper over:
an unknown field silently excluded from the hash is a field a cheating
client can change freely.
"""

import hashlib
import json

# Rounding applied to any float before it reaches the hash. The engine's
# damage math is integer by the time it lands in the state; this exists for
# percentages and multipliers that ride along.
FLOAT_PRECISION = 6

# The engine's Pokemon fields that affect a turn. Deliberately explicit
# rather than `__slots__`: a new engine field must be considered and added
# here, not silently absorbed.
POKEMON_FIELDS = (
    "id",
    "level",
    "hp",
    "maxhp",
    "ability",
    "item",
    "attack",
    "defense",
    "special_attack",
    "special_defense",
    "speed",
    "nature",
    "attack_boost",
    "defense_boost",
    "special_attack_boost",
    "special_defense_boost",
    "speed_boost",
    "accuracy_boost",
    "evasion_boost",
    "status",
    "terastallized",
    "burn_multiplier",
)


class UnhashableStateError(ValueError):
    """A state held something this module will not silently drop."""


def _scalar(value):
    if isinstance(value, bool) or value is None:
        return value
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return round(value, FLOAT_PRECISION)
    if isinstance(value, str):
        return value
    raise UnhashableStateError(
        "cannot canonicalize value of type {}: {!r}".format(type(value).__name__, value)
    )


def _sorted_strings(values):
    return sorted(str(value) for value in values or ())


def _move(move):
    """One move slot. Slot *order* is meaningful, so it is not sorted."""
    if isinstance(move, dict):
        return {str(key): _scalar(value) for key, value in sorted(move.items())}
    # Engine Move objects expose the same three fields under attributes.
    return {
        "id": _scalar(getattr(move, "id", None)),
        "disabled": _scalar(getattr(move, "disabled", False)),
        "current_pp": _scalar(getattr(move, "current_pp", 0)),
    }


def canonical_pokemon(pokemon):
    out = {}
    for field in POKEMON_FIELDS:
        if hasattr(pokemon, field):
            out[field] = _scalar(getattr(pokemon, field))
    out["types"] = _sorted_strings(getattr(pokemon, "types", ()))
    out["volatile_status"] = _sorted_strings(getattr(pokemon, "volatile_status", ()))
    out["evs"] = [_scalar(ev) for ev in getattr(pokemon, "evs", ())]
    out["moves"] = [_move(move) for move in getattr(pokemon, "moves", ())]
    return out


def canonical_side(side):
    conditions = getattr(side, "side_conditions", None) or {}
    return {
        "active": canonical_pokemon(side.active),
        # Reserve is keyed by name, so sorting the keys is the canonical order.
        "reserve": {
            str(name): canonical_pokemon(pokemon)
            for name, pokemon in sorted((getattr(side, "reserve", None) or {}).items())
        },
        "wish": [_scalar(value) for value in getattr(side, "wish", ()) or ()],
        "future_sight": [_scalar(value) for value in getattr(side, "future_sight", ()) or ()],
        # A defaultdict(int) grows keys just by being read, so a zero is
        # indistinguishable from an absent condition. Drop both.
        "side_conditions": {
            str(key): _scalar(value)
            for key, value in sorted(conditions.items())
            if value
        },
    }


def canonical_state(state):
    """The outcome-relevant content of a battle state, in canonical form."""
    return {
        "user": canonical_side(state.user),
        "opponent": canonical_side(state.opponent),
        "weather": _scalar(getattr(state, "weather", None)),
        "field": _scalar(getattr(state, "field", None)),
        "trick_room": bool(getattr(state, "trick_room", False)),
    }


def canonical_json(state):
    return json.dumps(
        canonical_state(state),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        allow_nan=False,
    )


def state_hash(state):
    """Hex digest both clients report for a resolved round."""
    return hashlib.sha256(canonical_json(state).encode("utf-8")).hexdigest()
