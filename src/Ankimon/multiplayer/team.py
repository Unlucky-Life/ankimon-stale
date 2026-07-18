"""Helpers for sending the current Ankimon team to multiplayer."""

import json
from typing import List

from ..resources import mainpokemon_path, mypokemon_path, team_pokemon_path


def _load_json_list(path) -> list:
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError):
        return []
    return data if isinstance(data, list) else []


def current_player_team_summary() -> List[dict]:
    """Return selected team Pokemon as small API-safe dicts.

    The team picker stores only individual_id in team.json. Resolve those ids
    through mypokemon.json, and fall back to mainpokemon.json if no team has
    been selected yet.
    """
    owned = _load_json_list(mypokemon_path)
    owned_by_id = {
        str(pokemon.get("individual_id")): pokemon
        for pokemon in owned
        if isinstance(pokemon, dict) and pokemon.get("individual_id")
    }

    selected = []
    for slot in _load_json_list(team_pokemon_path):
        if not isinstance(slot, dict):
            continue
        pokemon = owned_by_id.get(str(slot.get("individual_id")))
        if pokemon:
            selected.append(pokemon)

    if not selected:
        selected = [p for p in _load_json_list(mainpokemon_path) if isinstance(p, dict)]

    team = []
    for pokemon in selected[:6]:
        try:
            pokemon_id = int(pokemon.get("id") or 0)
            level = int(pokemon.get("level") or 1)
        except (TypeError, ValueError):
            continue
        if pokemon_id <= 0:
            continue
        team.append(
            {
                "name": str(pokemon.get("name") or ""),
                "id": pokemon_id,
                "level": max(1, min(level, 100)),
            }
        )
    return team
