"""Local import of server-awarded raid boss Pokemon."""

import json
import random
import uuid
from datetime import datetime
from typing import Optional

from ..functions.pokedex_functions import (
    get_all_pokemon_moves,
    get_base_experience,
    get_growth_rate,
    search_pokedex,
    search_pokedex_by_id,
)
from ..functions.pokemon_functions import pick_random_gender
from ..pyobj.pokemon_obj import PokemonObject
from ..resources import user_path
from ..utils import get_ev_spread

CLAIMED_REWARDS_PATH = user_path / "multiplayer_claimed_raid_rewards.json"


def _load_claimed_reward_ids() -> set[str]:
    try:
        with open(CLAIMED_REWARDS_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, list):
            return {str(value) for value in data}
    except (OSError, json.JSONDecodeError):
        pass
    return set()


def _save_claimed_reward_ids(reward_ids: set[str]) -> None:
    with open(CLAIMED_REWARDS_PATH, "w", encoding="utf-8") as f:
        json.dump(sorted(reward_ids), f, indent=2)


def reward_id_for(reward: dict) -> str:
    if not isinstance(reward, dict):
        return ""
    return str(reward.get("id") or "")


def is_raid_reward_claimed(reward: dict) -> bool:
    reward_id = reward_id_for(reward)
    return bool(reward_id and reward_id in _load_claimed_reward_ids())


def _pick_moves(name: str, level: int) -> list[str]:
    moves = get_all_pokemon_moves(name, level)
    if not moves:
        return ["Struggle"]
    if len(moves) <= 4:
        return moves
    return random.sample(moves, 4)


def _pick_ability(name: str) -> str:
    possible_abilities = search_pokedex(name, "abilities")
    if not possible_abilities:
        return "Run Away"
    numeric_abilities = {
        key: value for key, value in possible_abilities.items() if str(key).isdigit()
    }
    if not numeric_abilities:
        return "Run Away"
    return random.choice(list(numeric_abilities.values()))


def _build_reward_pokemon(reward: dict) -> Optional[PokemonObject]:
    boss_id = int(reward.get("boss_id") or 0)
    if boss_id <= 0:
        return None

    name = search_pokedex_by_id(boss_id)
    if not name or "not found" in str(name).lower():
        name = str(reward.get("boss_name") or "").lower()
    if not name:
        return None

    level = int(reward.get("level") or 30)
    level = max(30, min(level, 40))
    pokemon_type = search_pokedex(name, "types")
    base_stats = search_pokedex(name, "baseStats")
    if not pokemon_type or not base_stats:
        return None

    actual_id = search_pokedex(name, "actual_id") or boss_id
    stat_names = ["hp", "atk", "def", "spa", "spd", "spe"]
    ev = get_ev_spread("uniform")
    iv = {stat: random.randint(16, 31) for stat in stat_names}
    return PokemonObject(
        name=str(name).capitalize(),
        id=boss_id,
        level=level,
        ability=_pick_ability(name),
        type=pokemon_type,
        base_stats=base_stats,
        attacks=_pick_moves(name, level),
        base_experience=get_base_experience(actual_id),
        growth_rate=get_growth_rate(boss_id),
        ev=ev,
        iv=iv,
        gender=pick_random_gender(name),
        battle_status="Fighting",
        tier="Raid Boss",
        shiny=False,
        captured_date=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        individual_id=str(uuid.uuid4()),
    )


def claim_raid_reward(reward: dict) -> Optional[str]:
    if not isinstance(reward, dict):
        return None
    reward_id = reward_id_for(reward)
    if not reward_id:
        return None

    claimed = _load_claimed_reward_ids()
    if reward_id in claimed:
        return None

    pokemon = _build_reward_pokemon(reward)
    if pokemon is None:
        return None

    from ..functions.encounter_functions import save_caught_pokemon

    save_caught_pokemon(pokemon, pokemon.name, None)
    claimed.add(reward_id)
    _save_claimed_reward_ids(claimed)
    return f"Raid cleared! {pokemon.name} was caught at Lv. {pokemon.level}."
