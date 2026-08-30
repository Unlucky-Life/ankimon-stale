"""Tests for what a peer-verified battle tells the player (Phase D, item 6).

Peer verification adds three states that are nobody's fault and that look
like bugs when the UI stays quiet: a round waiting on the opponent's client,
a round replayed after a disagreement, and a battle suspended with no winner.
These pin that each one is named, that a suspension carries its reason, and
that "claim this battle" only appears once the grace window has actually
passed — claiming early would just fail against the server.
"""

import importlib.util
import os
import sys
import types
from datetime import datetime, timedelta, timezone

import pytest

SRC = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "src"))
ANKIMON = os.path.join(SRC, "Ankimon")

NOW = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)


def _stub_package(name, path=None):
    module = types.ModuleType(name)
    module.__path__ = [path] if path else []
    sys.modules[name] = module
    return module


@pytest.fixture
def status():
    """Load the module directly - the package __init__ needs aqt."""
    saved = dict(sys.modules)
    try:
        _stub_package("Ankimon", ANKIMON)
        _stub_package("Ankimon.multiplayer", os.path.join(ANKIMON, "multiplayer"))
        name = "Ankimon.multiplayer.pvp_status"
        spec = importlib.util.spec_from_file_location(
            name, os.path.join(ANKIMON, "multiplayer", "pvp_status.py")
        )
        module = importlib.util.module_from_spec(spec)
        sys.modules[name] = module
        spec.loader.exec_module(module)
        yield module
    finally:
        sys.modules.clear()
        sys.modules.update(saved)


def match(**overrides):
    base = {
        "id": "m1",
        "opponent": "gary",
        "status": "active",
        "round": 3,
        "incoming_challenge": False,
        "your_move_committed": False,
        "opponent_move_committed": False,
    }
    base.update(overrides)
    return base


def resolution(**overrides):
    base = {
        "round": 3,
        "attempt": 1,
        "seed": "0f0f",
        "you_submitted": False,
        "opponent_submitted": False,
    }
    base.update(overrides)
    return base


def test_a_round_being_resolved_is_not_shown_as_a_normal_turn(status):
    row = status.match_row_text(match(resolution=resolution()))
    assert "resolving" in row
    assert "you no / them no" not in row


def test_waiting_on_the_opponents_client_says_so(status):
    m = match(resolution=resolution(you_submitted=True))
    assert "waiting on them" in status.match_row_text(m)
    line = status.match_status_line(m)
    assert "gary" in line and "agree" in line


def test_a_replayed_round_is_explained_as_harmless(status):
    m = match(resolution=resolution(attempt=2))
    assert "replayed" in status.match_row_text(m)
    line = status.match_status_line(m)
    assert "replayed" in line and "no damage" in line


def test_a_suspension_carries_its_reason_and_says_no_winner(status):
    m = match(status="suspended", suspended_reason="the two games kept disagreeing")
    assert "no winner" in status.match_row_text(m)
    line = status.match_status_line(m)
    assert "kept disagreeing" in line
    assert "No winner" in line
    assert "returned" in line


def test_a_suspension_without_a_reason_still_explains_itself(status):
    line = status.match_status_line(match(status="suspended"))
    assert line and "No winner" in line


def test_a_stalled_battle_counts_down_to_the_claim(status):
    m = match(
        status="stalled",
        claimable_at=(NOW + timedelta(hours=3)).isoformat().replace("+00:00", "Z"),
    )
    assert not status.can_claim(m, now=NOW)
    line = status.match_status_line(m, now=NOW)
    assert "3 hours" in line


def test_the_claim_only_appears_once_the_window_has_passed(status):
    claimable = match(
        status="stalled",
        claimable_at=(NOW - timedelta(minutes=1)).isoformat().replace("+00:00", "Z"),
    )
    assert status.can_claim(claimable, now=NOW)
    line = status.match_status_line(claimable, now=NOW)
    assert "claim this battle now" in line
    # And it is claimed on forfeit, not on the unconfirmed round's damage -
    # the player should not expect that round to count.
    assert "forfeit" in line


def test_an_active_battle_is_never_claimable(status):
    assert not status.can_claim(match(resolution=resolution()), now=NOW)
    assert status.claim_available_at(match(), now=NOW) is None


def test_a_missing_claim_time_does_not_offer_a_claim(status):
    m = match(status="stalled")
    assert not status.can_claim(m, now=NOW)
    assert "not confirmed" in status.match_status_line(m, now=NOW)


def test_an_unparseable_claim_time_is_treated_as_unknown(status):
    m = match(status="stalled", claimable_at="whenever")
    assert not status.can_claim(m, now=NOW)


def test_an_incoming_challenge_still_reads_as_one(status):
    assert "challenged you" in status.match_row_text(
        match(incoming_challenge=True, status="pending")
    )


def test_a_plain_committed_turn_is_unchanged(status):
    m = match(your_move_committed=True)
    assert status.match_row_text(m) == "gary - round 3 (you yes / them no)"
    assert status.match_status_line(m) == ""


def test_waits_are_described_roughly_but_never_as_zero(status):
    assert status.describe_wait(30) == "in under a minute"
    assert status.describe_wait(60) == "in under a minute"
    assert status.describe_wait(90) == "in 1 minute"
    assert status.describe_wait(600) == "in 10 minutes"
    assert status.describe_wait(3600 * 2) == "in about 2 hours"
