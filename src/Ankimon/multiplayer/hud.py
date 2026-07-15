"""Reviewer-HUD fragment for multiplayer state.

Returns plain (html, css) strings that Reviewer_Manager appends inside the
existing #ankimon-hud Shadow-DOM portal. Everything renders from the
controller's cached state — building this fragment must never block.
"""

from typing import Optional, Tuple

MAX_TOKENS = 3


def build_hud_fragment(state: dict) -> Optional[Tuple[str, str]]:
    raid = state.get("raid") or {}
    pvp = state.get("pvp") or {}
    matches = pvp.get("matches", [])
    active_matches = [m for m in matches if m.get("status") == "active"]

    if not raid and not active_matches:
        return None

    html_parts = ['<div id="ankimon-mp" class="Ankimon">']

    if raid and raid.get("boss_max_hp"):
        pct = max(0, min(100, 100 * raid.get("boss_hp", 0) / raid["boss_max_hp"]))
        boss = raid.get("boss_name", "Raid boss")
        html_parts.append(
            '<div id="ankimon-mp-raid">'
            f'<span id="ankimon-mp-raid-label">RAID {boss} {int(pct)}%</span>'
            '<div id="ankimon-mp-raid-track">'
            f'<div id="ankimon-mp-raid-fill" style="width:{pct:.1f}%"></div>'
            "</div></div>"
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
        border-radius: 5px; padding: 3px 6px; min-width: 140px;
    }
    #ankimon-hud #ankimon-mp-raid-track {
        height: 5px; border-radius: 3px; background: rgba(255,255,255,0.25);
        overflow: hidden; margin-top: 2px;
    }
    #ankimon-hud #ankimon-mp-raid-fill {
        height: 100%; border-radius: 3px; background: #E74C3C;
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
