"""Canonical ordering of a turn's possible outcomes (Phase D, item D2).

Kept in its own module because two callers need it and only one of them can
afford Anki's dependencies: the wild-battle hook
(``ankimon_hooks_to_poke_engine``) pulls in the settings and tracker
singletons, while the PvP resolver (``multiplayer/pvp_resolution.py``) must
run on a bare engine so a round can be reproduced — and tested — without a
profile loaded.
"""


def canonical_outcome_key(outcome):
    """Total order over one turn's possible outcomes.

    Ordering must depend only on the outcome's content, never on the order the
    engine happened to generate it in, so two clients drawing with the same
    seed land on the same outcome. The instruction list is tuples of strings
    and ints, so its repr is a faithful and stable key; the percentage breaks
    ties between outcomes with identical instructions.
    """
    return (repr(outcome.instructions), outcome.percentage)
