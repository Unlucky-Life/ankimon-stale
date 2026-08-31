"""Serializing a PvP team so the opponent's client can simulate with it.

Peer-verified PvP (docs/multiplayer-pvp-phase-d.md) has both clients run the
same round on the same inputs. One of those inputs is the other player's
Pokemon, so it has to cross the network — once, at match creation, never per
round: re-uploading a team every round would be an opening for mid-match
edits.

The wire form is a JSON object with sorted keys and no floats that carry more
precision than the engine uses, for the same reason `round_hash` is careful:
two machines that hold the same team must build the same bytes from it.

What this module does *not* do is vouch for the team. A client can submit a
Pokemon it never earned; Phase D verifies resolution, not provenance. That is
why ranked play stays off until teams live server-side.
"""

import json

from ..poke_engine.objects import Pokemon

# Engine fields that describe a Pokemon at the start of a round. Explicit,
# not `__slots__`: a new engine field must be considered here rather than
# silently absorbed or silently dropped.
SCALAR_FIELDS = (
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
)


class InvalidTeamError(ValueError):
    """A team payload this module will not turn into a Pokemon."""


def _move_dict(move):
    if isinstance(move, dict):
        return {
            "id": move.get("id"),
            "disabled": bool(move.get("disabled", False)),
            "current_pp": int(move.get("current_pp", 0) or 0),
        }
    return {
        "id": getattr(move, "id", None),
        "disabled": bool(getattr(move, "disabled", False)),
        "current_pp": int(getattr(move, "current_pp", 0) or 0),
    }


def serialize_pokemon(pokemon) -> dict:
    """The engine Pokemon as a plain, ordered, JSON-safe dict."""
    data = {"id": pokemon.id}
    for field in SCALAR_FIELDS:
        data[field] = getattr(pokemon, field, None)
    data["types"] = [str(t) for t in (pokemon.types or ())]
    # Sets have no order; sorting is what makes the bytes reproducible.
    data["volatile_status"] = sorted(str(v) for v in (pokemon.volatile_status or ()))
    data["evs"] = [int(ev) for ev in (pokemon.evs or ())]
    # Move slot order is meaningful, so it is preserved, not sorted.
    data["moves"] = [_move_dict(move) for move in (pokemon.moves or ())]
    return data


def deserialize_pokemon(data: dict) -> Pokemon:
    """Rebuild the engine Pokemon a `serialize_pokemon` payload describes."""
    if not isinstance(data, dict) or not data.get("id"):
        raise InvalidTeamError("team payload has no Pokemon id")
    try:
        pokemon = Pokemon(
            identifier=str(data["id"]),
            level=int(data.get("level") or 1),
            types=[str(t) for t in data.get("types") or ()],
            hp=int(data.get("hp") or 0),
            maxhp=int(data.get("maxhp") or 0),
            ability=data.get("ability"),
            item=data.get("item"),
            attack=int(data.get("attack") or 0),
            defense=int(data.get("defense") or 0),
            special_attack=int(data.get("special_attack") or 0),
            special_defense=int(data.get("special_defense") or 0),
            speed=int(data.get("speed") or 0),
            nature=data.get("nature") or "serious",
            evs=tuple(int(ev) for ev in data.get("evs") or (85,) * 6),
            attack_boost=int(data.get("attack_boost") or 0),
            defense_boost=int(data.get("defense_boost") or 0),
            special_attack_boost=int(data.get("special_attack_boost") or 0),
            special_defense_boost=int(data.get("special_defense_boost") or 0),
            speed_boost=int(data.get("speed_boost") or 0),
            accuracy_boost=int(data.get("accuracy_boost") or 0),
            evasion_boost=int(data.get("evasion_boost") or 0),
            status=data.get("status"),
            terastallized=bool(data.get("terastallized", False)),
            volatile_status=set(data.get("volatile_status") or ()),
            moves=[dict(move) for move in data.get("moves") or ()],
        )
    except (TypeError, ValueError) as exc:
        raise InvalidTeamError(str(exc)) from exc
    return pokemon


def dump_team(pokemon) -> str:
    """Wire string for `POST /v1/matches`'s `team` field."""
    return json.dumps(
        serialize_pokemon(pokemon),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        allow_nan=False,
    )


def load_team(payload) -> Pokemon:
    """Inverse of `dump_team`, tolerating an already-decoded payload."""
    if isinstance(payload, Pokemon):
        return payload
    if isinstance(payload, str):
        try:
            payload = json.loads(payload)
        except (ValueError, TypeError) as exc:
            raise InvalidTeamError("team payload is not JSON") from exc
    return deserialize_pokemon(payload)
