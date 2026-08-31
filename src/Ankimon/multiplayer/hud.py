"""Reviewer-HUD fragment for multiplayer state.

Returns plain (html, css) strings that Reviewer_Manager appends inside the
existing #ankimon-hud Shadow-DOM portal. Everything renders from the
controller's cached state — building this fragment must never block.

Raid bosses and friend/bot battles use the same panel shape: opponent sprite,
name, and a live HP bar, so both kinds of battle read identically while
answering cards.
"""

from typing import Optional, Tuple

from ..business import get_image_as_base64
from ..functions.sprite_functions import get_sprite_path

MAX_TOKENS = 3


def _escape(text) -> str:
    return (
        str(text)
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


def _sprite_html(pokemon_id, alt: str) -> str:
    try:
        pokemon_id = int(pokemon_id or 0)
    except (TypeError, ValueError):
        return ""
    if pokemon_id <= 0:
        return ""
    try:
        sprite_path = get_sprite_path("front", "png", pokemon_id, False, "M")
        image_base64 = get_image_as_base64(sprite_path)
    except Exception:
        return ""
    if not image_base64:
        return ""
    return (
        '<div class="ankimon-mp-sprite">'
        f'<img src="data:image/png;base64,{image_base64}" alt="{_escape(alt)}">'
        "</div>"
    )


def _percent(current, maximum) -> float:
    try:
        current = float(current or 0)
        maximum = float(maximum or 0)
    except (TypeError, ValueError):
        return 0.0
    if maximum <= 0:
        return 0.0
    return max(0.0, min(100.0, 100.0 * current / maximum))


def _panel_html(
    panel_id: str,
    sprite_html: str,
    label: str,
    pct: float,
    fill_class: str,
    footer_html: str = "",
) -> str:
    return (
        f'<div id="{panel_id}" class="ankimon-mp-panel">'
        f"{sprite_html}"
        '<div class="ankimon-mp-meta">'
        f'<span class="ankimon-mp-label">{label}</span>'
        '<div class="ankimon-mp-track">'
        f'<div class="ankimon-mp-fill {fill_class}" style="width:{pct:.1f}%"></div>'
        "</div>"
        f"{footer_html}"
        "</div></div>"
    )


def _active_raid(state: dict) -> dict:
    raid = state.get("raid") or {}
    if raid and (
        raid.get("defeated")
        or raid.get("ended")
        or int(raid.get("boss_hp") or 0) <= 0
    ):
        return {}
    return raid


def _battle_footer(pvp: dict, match: dict) -> str:
    tokens = min(int(pvp.get("tokens", 0) or 0), MAX_TOKENS)
    pips = "".join(
        f'<span class="ankimon-mp-pip{" filled" if i < tokens else ""}"></span>'
        for i in range(MAX_TOKENS)
    )

    if match.get("your_move_committed"):
        status = '<span class="ankimon-mp-status waiting">WAITING FOR OPPONENT</span>'
    elif tokens > 0:
        status = '<span class="ankimon-mp-status ready">ATTACK READY</span>'
    else:
        status = '<span class="ankimon-mp-status">ANSWER CARDS TO CHARGE</span>'

    return f'<div class="ankimon-mp-footer">{pips}{status}</div>'


def _battle_panel(pvp: dict, match: dict) -> str:
    opponent = match.get("opponent_pokemon") or {}
    opponent_name = str(match.get("opponent") or "opponent")
    pokemon_name = str(opponent.get("name") or "").strip()
    level = opponent.get("level")

    hp = opponent.get("hp")
    max_hp = opponent.get("max_hp")
    if hp is None:
        hp = match.get("opponent_hp")
    pct = _percent(hp, max_hp)

    title = _escape(opponent_name.upper())
    if pokemon_name:
        title += f" · {_escape(pokemon_name.capitalize())}"
    if level:
        title += f" Lv{int(level)}"
    label = f"{title} {int(pct)}%"

    return _panel_html(
        "ankimon-mp-battle",
        _sprite_html(opponent.get("id"), pokemon_name or opponent_name),
        label,
        pct,
        "ankimon-mp-fill-battle",
        _battle_footer(pvp, match),
    )


def _raid_panel(raid: dict) -> str:
    pct = _percent(raid.get("boss_hp"), raid.get("boss_max_hp"))
    boss = str(raid.get("boss_name") or "Raid boss")
    label = f"RAID {_escape(boss)} {int(pct)}%"
    return _panel_html(
        "ankimon-mp-raid",
        _sprite_html(raid.get("boss_id"), boss),
        label,
        pct,
        "ankimon-mp-fill-raid",
    )


def build_hud_fragment(state: dict) -> Optional[Tuple[str, str]]:
    raid = _active_raid(state)
    pvp = state.get("pvp") or {}
    matches = pvp.get("matches", [])
    active_matches = [m for m in matches if m.get("status") == "active"]

    if not raid and not active_matches:
        return None

    html_parts = ['<div id="ankimon-mp" class="Ankimon">']

    if raid and raid.get("boss_max_hp"):
        html_parts.append(_raid_panel(raid))

    for match in active_matches:
        html_parts.append(_battle_panel(pvp, match))

    html_parts.append("</div>")

    css = """
    #ankimon-hud #ankimon-mp {
        position: fixed; top: 8px; right: 8px; z-index: 9999;
        font-family: Arial, sans-serif; font-size: 11px;
        display: flex; flex-direction: column; gap: 4px; align-items: flex-end;
        pointer-events: none;
    }
    #ankimon-hud .ankimon-mp-panel {
        background: rgba(31,31,31,0.75); color: #fff;
        border-radius: 5px; padding: 4px 6px; min-width: 160px; max-width: 240px;
        display: flex; align-items: center; gap: 6px;
    }
    #ankimon-hud .ankimon-mp-sprite {
        width: 38px; height: 38px; flex: 0 0 38px;
        display: flex; align-items: center; justify-content: center;
    }
    #ankimon-hud .ankimon-mp-sprite img {
        max-width: 38px; max-height: 38px; image-rendering: auto;
    }
    #ankimon-hud .ankimon-mp-meta {
        min-width: 0; flex: 1;
    }
    #ankimon-hud .ankimon-mp-label {
        display: block; white-space: nowrap; overflow: hidden; text-overflow: ellipsis;
    }
    #ankimon-hud .ankimon-mp-track {
        height: 5px; border-radius: 3px; background: rgba(255,255,255,0.25);
        overflow: hidden; margin-top: 2px;
    }
    #ankimon-hud .ankimon-mp-fill { height: 100%; border-radius: 3px; }
    #ankimon-hud .ankimon-mp-fill-raid { background: #E74C3C; }
    #ankimon-hud .ankimon-mp-fill-battle { background: #5DADE2; }
    #ankimon-hud .ankimon-mp-footer {
        display: flex; align-items: center; gap: 3px; margin-top: 3px;
    }
    #ankimon-hud .ankimon-mp-pip {
        width: 7px; height: 7px; border-radius: 50%;
        background: rgba(255,255,255,0.25); display: inline-block;
    }
    #ankimon-hud .ankimon-mp-pip.filled { background: #F7DC6F; }
    #ankimon-hud .ankimon-mp-status {
        margin-left: 4px; font-size: 10px; color: rgba(255,255,255,0.65);
        white-space: nowrap; overflow: hidden; text-overflow: ellipsis;
    }
    #ankimon-hud .ankimon-mp-status.ready { color: #7FB3D5; font-weight: bold; }
    #ankimon-hud .ankimon-mp-status.waiting { color: #F5B7B1; }
    """
    return "".join(html_parts), css
