"""Raid boss encounter integration for Ankimon multiplayer.

This module keeps the reviewer battle target aligned with multiplayer state:
if the controller has a live raid cached, wild encounter rolls are replaced
with that raid boss Pokemon. Otherwise, an active friend/bot battle can replace
the wild encounter with the opponent's battle Pokemon. It does not perform
network I/O; the controller refreshes state asynchronously elsewhere.
"""

import random
from typing import Any, Optional

from ..functions.pokedex_functions import (
    get_all_pokemon_moves,
    get_base_experience,
    get_effort_values,
    get_growth_rate,
    search_pokedex,
    search_pokedex_by_id,
)
from ..functions.pokemon_functions import pick_random_gender
from ..utils import get_ev_spread


def _active_raid_from_controller(controller: Any) -> Optional[dict]:
    state = getattr(controller, "state", {}) or {}
    raid = state.get("raid")
    if not isinstance(raid, dict):
        return None
    if raid.get("defeated") or raid.get("ended"):
        return None
    if int(raid.get("boss_hp") or 0) <= 0:
        return None
    return raid


def _active_pvp_match_from_controller(controller: Any) -> Optional[dict]:
    state = getattr(controller, "state", {}) or {}
    pvp = state.get("pvp") or {}
    matches = pvp.get("matches") or []
    for match in matches:
        if not isinstance(match, dict):
            continue
        if match.get("status") != "active":
            continue
        opponent = match.get("opponent_pokemon")
        if not isinstance(opponent, dict):
            continue
        if int(opponent.get("id") or 0) <= 0:
            continue
        if int(opponent.get("hp") or 1) <= 0:
            continue
        return match
    return None


def _pick_raid_boss_moves(name: str, level: int) -> list[str]:
    moves = get_all_pokemon_moves(name, level)
    if not moves:
        return ["Struggle"]
    if len(moves) <= 4:
        return moves
    return random.sample(moves, 4)


def _pick_raid_boss_ability(name: str) -> str:
    possible_abilities = search_pokedex(name, "abilities")
    if not possible_abilities:
        return "no_ability"

    numeric_abilities = {
        key: value for key, value in possible_abilities.items() if str(key).isdigit()
    }
    if not numeric_abilities:
        return "no_ability"
    return random.choice(list(numeric_abilities.values()))


def _build_pokemon_tuple(
    pokemon: dict,
    fallback_level: int,
    tier: str,
    ankimon_tracker_obj: Any,
) -> Optional[tuple]:
    pokemon_id = int(pokemon.get("id") or 0)
    if pokemon_id <= 0:
        return None

    name = search_pokedex_by_id(pokemon_id)
    if not name or "not found" in str(name).lower():
        name = str(pokemon.get("name") or "").lower()
    if not name:
        return None

    level = int(pokemon.get("level") or fallback_level or 5)
    level = max(1, min(level, 100))

    pokemon_type = search_pokedex(name, "types")
    base_stats = search_pokedex(name, "baseStats")
    if not pokemon_type or not base_stats:
        return None

    actual_id = search_pokedex(name, "actual_id") or pokemon_id
    stat_names = ["hp", "atk", "def", "spa", "spd", "spe"]
    ev = get_ev_spread("uniform")
    iv = {stat: random.randint(16, 31) for stat in stat_names}

    ankimon_tracker_obj.pokemon_encounter = 0
    ankimon_tracker_obj.cards_battle_round = 0

    return (
        name,
        pokemon_id,
        level,
        _pick_raid_boss_ability(name),
        pokemon_type,
        base_stats,
        _pick_raid_boss_moves(name, level),
        get_base_experience(actual_id),
        get_growth_rate(pokemon_id),
        ev,
        iv,
        pick_random_gender(name),
        "fighting",
        base_stats,
        tier,
        get_effort_values(actual_id),
        False,
    )


def _build_raid_boss_tuple(
    raid: dict,
    main_pokemon_level: int,
    ankimon_tracker_obj: Any,
) -> Optional[tuple]:
    return _build_pokemon_tuple(
        {
            "id": raid.get("boss_id"),
            "name": raid.get("boss_name"),
            "level": raid.get("boss_level"),
        },
        main_pokemon_level,
        "Raid Boss",
        ankimon_tracker_obj,
    )


def _build_pvp_opponent_tuple(
    match: dict,
    main_pokemon_level: int,
    ankimon_tracker_obj: Any,
) -> Optional[tuple]:
    opponent = match.get("opponent_pokemon") or {}
    opponent_name = match.get("opponent") or "opponent"
    return _build_pokemon_tuple(
        opponent,
        opponent.get("level") or main_pokemon_level,
        f"PvP: {opponent_name}",
        ankimon_tracker_obj,
    )


def install_raid_boss_encounter_patch(controller: Any, caller_globals: dict) -> None:
    """Patch encounter generation so multiplayer battles use their opponent.

    `caller_globals` is the addon __init__ module namespace. It has already
    imported generate_random_pokemon directly, so the patch updates both that
    local binding for startup encounters and the encounter_functions module
    binding used later by new_pokemon().
    """
    from ..functions import encounter_functions

    original = encounter_functions.generate_random_pokemon
    if getattr(original, "_ankimon_raid_boss_patch", False):
        caller_globals["generate_random_pokemon"] = original
        return

    def generate_multiplayer_or_random(main_pokemon_level, ankimon_tracker_obj):
        raid = _active_raid_from_controller(controller)
        if raid is not None:
            try:
                raid_boss = _build_raid_boss_tuple(
                    raid,
                    main_pokemon_level,
                    ankimon_tracker_obj,
                )
                if raid_boss is not None:
                    logger = getattr(controller, "logger", None)
                    if logger is not None:
                        logger.log("game", f"Loaded raid boss encounter: {raid_boss[0]}")
                    return raid_boss
            except Exception as exc:
                logger = getattr(controller, "logger", None)
                if logger is not None:
                    logger.log("warning", f"Could not load raid boss encounter: {exc}")
        match = _active_pvp_match_from_controller(controller)
        if match is not None:
            try:
                pvp_opponent = _build_pvp_opponent_tuple(
                    match,
                    main_pokemon_level,
                    ankimon_tracker_obj,
                )
                if pvp_opponent is not None:
                    logger = getattr(controller, "logger", None)
                    if logger is not None:
                        logger.log("game", f"Loaded PvP encounter: {pvp_opponent[0]}")
                    return pvp_opponent
            except Exception as exc:
                logger = getattr(controller, "logger", None)
                if logger is not None:
                    logger.log("warning", f"Could not load PvP encounter: {exc}")
        return original(main_pokemon_level, ankimon_tracker_obj)

    generate_multiplayer_or_random._ankimon_raid_boss_patch = True
    generate_multiplayer_or_random._ankimon_original = original
    encounter_functions.generate_random_pokemon = generate_multiplayer_or_random
    caller_globals["generate_random_pokemon"] = generate_multiplayer_or_random
