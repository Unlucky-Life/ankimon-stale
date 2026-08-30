"""What a battle's current state says to the player (Phase D, item 6).

Peer-verified rounds add three states a player can land in that are nobody's
fault and that look like bugs if the UI stays silent: a round waiting on the
opponent's client to confirm, a round the two games disagreed on and replayed,
and a battle suspended because they disagreed twice. A suspension in
particular ends a battle with no winner — if the window just shows "suspended"
with no reason, that reads as the addon losing the match.

Kept as plain functions over the state dict, with no Qt in sight, so the
wording can be tested without a window.
"""

from datetime import datetime, timezone

STATUS_ACTIVE = "active"
STATUS_SUSPENDED = "suspended"
STATUS_STALLED = "stalled"
STATUS_FINISHED = "finished"


def _parse_time(value):
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None


def _now(now=None):
    return now or datetime.now(timezone.utc)


def describe_wait(seconds: float) -> str:
    """A rough, honest wait — never "in 0 minutes"."""
    if seconds <= 60:
        return "in under a minute"
    minutes = int(seconds // 60)
    if minutes < 60:
        return "in {} minute{}".format(minutes, "" if minutes == 1 else "s")
    hours = int(round(minutes / 60))
    return "in about {} hour{}".format(hours, "" if hours == 1 else "s")


def claim_available_at(match, now=None):
    """Seconds until a stalled battle can be claimed, or None."""
    if (match or {}).get("status") != STATUS_STALLED:
        return None
    claimable_at = _parse_time(match.get("claimable_at"))
    if claimable_at is None:
        return None
    return (claimable_at - _now(now)).total_seconds()


def can_claim(match, now=None) -> bool:
    remaining = claim_available_at(match, now)
    return remaining is not None and remaining <= 0


def match_row_text(match) -> str:
    """One line for the battle list."""
    opponent = match.get("opponent", "?")
    status = match.get("status", "?")
    if match.get("incoming_challenge"):
        return f"{opponent} challenged you!"
    if status == STATUS_SUSPENDED:
        return f"{opponent} - suspended, no winner"
    if status == STATUS_STALLED:
        return f"{opponent} - waiting on them to confirm"
    if status != STATUS_ACTIVE:
        return f"{opponent} - {status}"

    round_no = match.get("round", 1)
    resolution = match.get("resolution") or {}
    if resolution:
        if resolution.get("attempt", 1) > 1:
            return f"{opponent} - round {round_no} replayed, resolving"
        if resolution.get("you_submitted") and not resolution.get(
            "opponent_submitted"
        ):
            return f"{opponent} - round {round_no}, waiting on them to confirm"
        return f"{opponent} - round {round_no}, resolving"
    you = "yes" if match.get("your_move_committed") else "no"
    them = "yes" if match.get("opponent_move_committed") else "no"
    return f"{opponent} - round {round_no} (you {you} / them {them})"


def match_status_line(match, now=None) -> str:
    """The sentence under the list explaining what is happening and why."""
    if not match:
        return ""
    opponent = match.get("opponent", "your opponent")
    status = match.get("status")

    if status == STATUS_SUSPENDED:
        reason = match.get("suspended_reason") or "the two games could not agree"
        return (
            f"Battle suspended - {reason}. No winner and no rating change, "
            "and the turn tokens for that round were returned."
        )

    if status == STATUS_STALLED:
        remaining = claim_available_at(match, now)
        if remaining is None:
            return f"{opponent} has not confirmed the round yet."
        if remaining <= 0:
            return (
                f"{opponent} never confirmed the round. You can claim this "
                "battle now - it ends on forfeit, not on that round's damage."
            )
        return (
            f"{opponent} has not confirmed the round. If they do not come "
            f"back you can claim the battle {describe_wait(remaining)}."
        )

    resolution = match.get("resolution") or {}
    if status == STATUS_ACTIVE and resolution:
        if resolution.get("attempt", 1) > 1:
            return (
                "The two games disagreed on this round, so it is being "
                "replayed with a new seed. This is usually nothing - no "
                "damage was applied."
            )
        if not resolution.get("you_submitted"):
            return "Both moves are in - your game is working out the round."
        if not resolution.get("opponent_submitted"):
            return (
                f"Waiting for {opponent}'s game to confirm the round. Both "
                "sides have to agree before any damage counts."
            )
        return "Both sides confirmed - applying the round."
    return ""
