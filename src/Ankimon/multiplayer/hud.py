"""Reviewer-HUD fragment for multiplayer state.

Returns plain (html, css) strings that Reviewer_Manager appends inside the
existing #ankimon-hud Shadow-DOM portal. Everything renders from the
controller's cached state — building this fragment must never block.
"""

from html import escape
from typing import Optional, Tuple

from ..business import get_image_as_base64
from ..functions.sprite_functions import get_sprite_path

MAX_TOKENS = 3


def _pokemon_sprite_html(pokemon_id: int, name: str, element_id: str) -> str:
    pokemon_id = int(pokemon_id or 0)
    if pokemon_id <= 0:
        return ""
    safe_name = escape(str(name or "Pokemon"))
    try:
        sprite_path = get_sprite_path("front", "png", pokemon_id, False, "M")
        image_base64 = get_image_as_base64(sprite_path)
    except Exception:
        return ""
    if not image_base64:
        return ""
    return (
        f'<div id="{element_id}">'
        f'<img src="data:image/png;base64,{image_base64}" alt="{safe_name}">'
        "</div>"
    )


def _raid_boss_sprite_html(raid: dict) -> str:
    boss_id = int(raid.get("boss_id") or 0)
    if boss_id <= 0:
        return ""
    boss = raid.get("boss_name", "Raid boss")
    return _pokemon_sprite_html(boss_id, boss, "ankimon-mp-raid-sprite")


def build_hud_fragment(state: dict) -> Optional[Tuple[str, str]]:
    raid = state.get("raid") or {}
    if raid and (
        raid.get("defeated")
        or raid.get("ended")
        or int(raid.get("boss_hp") or 0) <= 0
    ):
        raid = {}
    pvp = state.get("pvp") or {}
    reward = state.get("raid_reward") or {}
    matches = pvp.get("matches", [])
    active_matches = [m for m in matches if m.get("status") == "active"]

    if not raid and not reward and not active_matches:
        return None

    html_parts = ['<div id="ankimon-mp" class="Ankimon">']

    if reward:
        boss = escape(str(reward.get("boss_name") or "Raid boss"))
        level = int(reward.get("level") or 0)
        sprite = _pokemon_sprite_html(
            int(reward.get("boss_id") or 0),
            boss,
            "ankimon-mp-reward-sprite",
        )
        level_text = f"Lv. {level}" if level else "Reward"
        html_parts.append(
            '<div id="ankimon-mp-reward">'
            f"{sprite}"
            '<div id="ankimon-mp-reward-meta">'
            '<span id="ankimon-mp-reward-kicker">RAID CLEARED</span>'
            f'<strong id="ankimon-mp-reward-name">{boss}</strong>'
            f'<span id="ankimon-mp-reward-detail">Caught {level_text}</span>'
            "</div></div>"
        )

    if raid and raid.get("boss_max_hp"):
        pct = max(0, min(100, 100 * raid.get("boss_hp", 0) / raid["boss_max_hp"]))
        boss = raid.get("boss_name", "Raid boss")
        html_parts.append(
            '<div id="ankimon-mp-raid">'
            f"{_raid_boss_sprite_html(raid)}"
            '<div id="ankimon-mp-raid-meta">'
            f'<span id="ankimon-mp-raid-label">RAID {boss} {int(pct)}%</span>'
            '<div id="ankimon-mp-raid-track">'
            f'<div id="ankimon-mp-raid-fill" style="width:{pct:.1f}%"></div>'
            "</div></div></div>"
        )

    if active_matches:
        tokens = min(pvp.get("tokens", 0), MAX_TOKENS)
        turn_ready = any(
            not m.get("your_move_committed", False) for m in active_matches
        )
        pips = "".join(
            f'<span class="ankimon-mp-pip{" filled" if i < tokens else ""}"></span>'
            for i in range(MAX_TOKENS)
        )
        ready_html = (
            '<span id="ankimon-mp-turn">YOUR TURN</span>'
            if turn_ready and tokens > 0
            else ""
        )
        html_parts.append(f'<div id="ankimon-mp-pvp">{pips}{ready_html}</div>')

    html_parts.append("</div>")

    css = """
    #ankimon-hud #ankimon-mp {
        position: fixed; top: 8px; right: 8px; z-index: 9999;
        font-family: Arial, sans-serif; font-size: 11px;
        display: flex; flex-direction: column; gap: 4px; align-items: flex-end;
        pointer-events: none;
    }
    #ankimon-hud #ankimon-mp-raid {
        background: rgba(31,31,31,0.75); color: #fff;
        border-radius: 5px; padding: 4px 6px; min-width: 160px;
        display: flex; align-items: center; gap: 6px;
    }
    #ankimon-hud #ankimon-mp-raid-sprite {
        width: 38px; height: 38px; flex: 0 0 38px;
        display: flex; align-items: center; justify-content: center;
    }
    #ankimon-hud #ankimon-mp-raid-sprite img,
    #ankimon-hud #ankimon-mp-reward-sprite img {
        max-width: 38px; max-height: 38px; image-rendering: auto;
    }
    #ankimon-hud #ankimon-mp-raid-meta {
        min-width: 0; flex: 1;
    }
    #ankimon-hud #ankimon-mp-raid-label {
        display: block; white-space: nowrap; overflow: hidden; text-overflow: ellipsis;
    }
    #ankimon-hud #ankimon-mp-raid-track {
        height: 5px; border-radius: 3px; background: rgba(255,255,255,0.25);
        overflow: hidden; margin-top: 2px;
    }
    #ankimon-hud #ankimon-mp-raid-fill {
        height: 100%; border-radius: 3px; background: #E74C3C;
    }
    #ankimon-hud #ankimon-mp-reward {
        background: linear-gradient(135deg, rgba(31,31,31,0.92), rgba(38,88,62,0.9));
        color: #fff; border: 1px solid rgba(247,220,111,0.9);
        border-radius: 6px; padding: 6px 8px; min-width: 180px;
        display: flex; align-items: center; gap: 7px;
        box-shadow: 0 4px 14px rgba(0,0,0,0.25);
    }
    #ankimon-hud #ankimon-mp-reward-sprite {
        width: 42px; height: 42px; flex: 0 0 42px;
        display: flex; align-items: center; justify-content: center;
    }
    #ankimon-hud #ankimon-mp-reward-sprite img {
        max-width: 42px; max-height: 42px;
    }
    #ankimon-hud #ankimon-mp-reward-meta {
        min-width: 0; display: flex; flex-direction: column; gap: 1px;
    }
    #ankimon-hud #ankimon-mp-reward-kicker {
        color: #F7DC6F; font-size: 10px; font-weight: bold;
    }
    #ankimon-hud #ankimon-mp-reward-name {
        font-size: 13px; line-height: 16px; white-space: nowrap;
        overflow: hidden; text-overflow: ellipsis;
    }
    #ankimon-hud #ankimon-mp-reward-detail {
        color: rgba(255,255,255,0.82); font-size: 11px;
    }
    #ankimon-hud #ankimon-mp-pvp {
        background: rgba(31,31,31,0.75); border-radius: 5px;
        padding: 3px 6px; display: flex; gap: 3px; align-items: center;
    }
    #ankimon-hud .ankimon-mp-pip {
        width: 7px; height: 7px; border-radius: 50%;
        background: rgba(255,255,255,0.25); display: inline-block;
    }
    #ankimon-hud .ankimon-mp-pip.filled { background: #F7DC6F; }
    #ankimon-hud #ankimon-mp-turn {
        color: #7FB3D5; font-weight: bold; margin-left: 4px;
    }
    """
    return "".join(html_parts), css
