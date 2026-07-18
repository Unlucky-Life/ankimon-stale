"""Shared battle rules."""

from typing import Any


def is_trainer_enemy_pokemon(pokemon: Any) -> bool:
    """Multiplayer enemies are opponents, not wild catch encounters."""
    tier = str(getattr(pokemon, "tier", "") or "").lower()
    return tier.startswith("pvp:") or tier == "raid boss"
