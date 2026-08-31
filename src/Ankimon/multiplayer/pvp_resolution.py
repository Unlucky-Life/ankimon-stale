"""Simulating one PvP round locally, the way the opponent's client will.

The server does not compute Pokemon damage in Phase D. When both moves are
in it opens a *resolution*: a seed, the two revealed moves, and a deadline.
Both clients run this module, report a hash of the result, and the server
applies the round only if the two reports agree
(docs/multiplayer-pvp-phase-d.md).

Two clients only agree if they build the same battle from the same inputs, so
three things here are deliberate:

- **Roles, not viewpoints.** The challenger is always the state's `user` side
  and the challenged player always the `opponent` side, on both machines. If
  each client put *itself* on the user side, the two states would be mirror
  images and every honest round would hash differently.
- **The seed is the only randomness.** `random.Random(seed)` is passed
  explicitly; the module-global generator is never touched, since it also
  drives wild encounters, IVs and raid-reward rolls (item D1).
- **Outcomes are drawn in canonical order** (item D2), so the draw cannot
  depend on the order the engine happened to generate them in.

The engine core this imports is pure — no settings, no tracker, no profile —
which is what lets a round be reproduced, and tested, headlessly.
"""

import hashlib
import json
import random
from collections import defaultdict

from ..poke_engine.canonical import canonical_outcome_key
from ..poke_engine.find_state_instructions import get_all_state_instructions
from ..poke_engine.helpers import normalize_name
from ..poke_engine.objects import Side, State, StateMutator
from .pvp_team import load_team, serialize_pokemon
from .round_hash import state_hash

# The side conditions the engine expects to find present. It keeps them in a
# defaultdict, so an absent key and a zero are the same thing in play — but
# not to a hash. Both clients therefore start from the same explicit set.
DEFAULT_SIDE_CONDITIONS = (
    "stealthrock",
    "spikes",
    "toxicspikes",
    "tailwind",
    "reflect",
    "lightscreen",
    "auroraveil",
    "protect",
)

# The role names used on the wire and in this module. "challenger" is the
# player who sent the challenge; the server uses the same two roles.
CHALLENGER = "challenger"
OPPONENT = "opponent"


class ResolutionError(ValueError):
    """A round that cannot be simulated from the inputs given."""


class RoundResult:
    """One resolved round: what to report, and the state to carry forward."""

    def __init__(self, state, hp, state_hash_hex, log_digest):
        self.state = state
        self.hp = hp
        self.state_hash = state_hash_hex
        self.log_digest = log_digest


def _new_side(pokemon, side_conditions=None):
    conditions = defaultdict(int, {name: 0 for name in DEFAULT_SIDE_CONDITIONS})
    for name, value in (side_conditions or {}).items():
        conditions[str(name)] = int(value)
    return Side(
        active=pokemon,
        reserve={},
        wish=(0, 0),
        side_conditions=conditions,
        future_sight=(0, 0),
    )


def seed_to_int(seed) -> int:
    """The server's hex seed as the integer `random.Random` wants."""
    if isinstance(seed, int):
        return seed
    try:
        return int(str(seed), 16)
    except (TypeError, ValueError) as exc:
        raise ResolutionError("resolution seed is not a hex value") from exc


def build_state(challenger_team, opponent_team, carried=None) -> State:
    """The battle state a round starts from.

    `carried` is the state left by the previous round, as produced by
    `dump_state`. Without it the battle starts fresh from the two teams —
    correct for round 1, and wrong for round 5, which is why the caller keeps
    it. A client that has lost its carried state will disagree with its
    opponent and the round will be replayed and then suspended: a visible
    stop, not a silently wrong battle.
    """
    if carried:
        return load_state(carried)
    return State(
        user=_new_side(load_team(challenger_team)),
        opponent=_new_side(load_team(opponent_team)),
        weather=None,
        field=None,
        trick_room=False,
    )


def dump_state(state) -> dict:
    """The state, as a plain dict, to carry into the next round."""
    return {
        CHALLENGER: {
            "active": serialize_pokemon(state.user.active),
            "side_conditions": {
                str(name): int(value)
                for name, value in sorted((state.user.side_conditions or {}).items())
            },
        },
        OPPONENT: {
            "active": serialize_pokemon(state.opponent.active),
            "side_conditions": {
                str(name): int(value)
                for name, value in sorted((state.opponent.side_conditions or {}).items())
            },
        },
        "weather": state.weather,
        "field": state.field,
        "trick_room": bool(state.trick_room),
    }


def load_state(carried: dict) -> State:
    if not isinstance(carried, dict) or CHALLENGER not in carried:
        raise ResolutionError("carried battle state is not usable")
    return State(
        user=_new_side(
            load_team(carried[CHALLENGER]["active"]),
            carried[CHALLENGER].get("side_conditions"),
        ),
        opponent=_new_side(
            load_team(carried[OPPONENT]["active"]),
            carried[OPPONENT].get("side_conditions"),
        ),
        weather=carried.get("weather"),
        field=carried.get("field"),
        trick_room=bool(carried.get("trick_room", False)),
    )


def _log_digest(outcome) -> str:
    """A digest of the instructions that produced the round.

    Reported alongside the state hash: two clients that land on the same HP
    by different instruction paths have still disagreed about the battle, and
    that is worth catching.
    """
    payload = json.dumps(
        {
            "instructions": [repr(instruction) for instruction in outcome.instructions],
            "percentage": round(float(outcome.percentage), 6),
        },
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        allow_nan=False,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def resolve_round(
    challenger_move,
    opponent_move,
    seed,
    challenger_team=None,
    opponent_team=None,
    carried_state=None,
) -> RoundResult:
    """Simulate the open round and return what to report to the server."""
    state = build_state(challenger_team, opponent_team, carried_state)
    mutator = StateMutator(state)
    try:
        outcomes = get_all_state_instructions(
            mutator,
            normalize_name(challenger_move or "splash"),
            normalize_name(opponent_move or "splash"),
        )
    except Exception as exc:  # engine refusal is a resolution failure, not a crash
        raise ResolutionError("the engine could not resolve this round: {}".format(exc))
    if not outcomes:
        raise ResolutionError("the engine produced no outcome for this round")

    outcomes = sorted(outcomes, key=canonical_outcome_key)
    rng = random.Random(seed_to_int(seed))
    weights = [outcome.percentage for outcome in outcomes]
    chosen = rng.choices(outcomes, weights=weights, k=1)[0]
    mutator.apply(chosen.instructions)

    return RoundResult(
        state=state,
        hp={
            CHALLENGER: max(0, int(state.user.active.hp)),
            OPPONENT: max(0, int(state.opponent.active.hp)),
        },
        state_hash_hex=state_hash(state),
        log_digest=_log_digest(chosen),
    )
