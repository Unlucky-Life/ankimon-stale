"""Cross-machine determinism probe for peer-verified PvP (Phase D).

`PVP_HUMAN_ENABLED` must not be turned on because the resolver is
deterministic on one machine. Two runs on one box share a Python build, a
data directory and a line-ending convention, so they cannot catch the two
failure modes that actually decide matches in the wild:

- **D3, version skew** — different move data or base stats between clients;
- **D4, representation instability** — a float, a set order or a dict order
  that differs between builds.

So this is a probe two *different* machines run, printing one fingerprint
line to compare. Same fingerprint on both means the two clients would agree
on a round. A differing one is not a mystery: the per-seed rows below it say
whether the engine data differs (D3) or the same data resolved differently
(D4).

    python tools/pvp_determinism_probe.py                # human readable
    python tools/pvp_determinism_probe.py --json         # machine readable
    python tools/pvp_determinism_probe.py --compare a.json

The battle it runs is fixed and lives in this file, not in the player's save:
a probe that depended on the local Pokemon would differ between machines for
an uninteresting reason.
"""

import argparse
import hashlib
import json
import os
import platform
import sys
import types

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC = os.path.join(REPO, "src")
ANKIMON = os.path.join(SRC, "Ankimon")

# Seeds are arbitrary but fixed: what matters is that both machines draw the
# same outcome from the same one. Several, because a single seed can agree by
# landing on the same overwhelmingly likely outcome.
SEEDS = (
    "0000000000000001",
    "0f0f0f0f0f0f0f0f",
    "123456789abcdef0",
    "ffffffffffffffff",
    "5eed5eed5eed5eed",
)

# Two ordinary Pokemon and two ordinary moves. Deliberately plain: the probe
# is checking that the machines agree, not exercising the engine's corners.
CHALLENGER_TEAM = {
    "id": "squirtle",
    "level": 50,
    "hp": 150,
    "maxhp": 150,
    "ability": None,
    "item": None,
    "attack": 100,
    "defense": 100,
    "special_attack": 100,
    "special_defense": 100,
    "speed": 100,
    "nature": "serious",
    "evs": [85] * 6,
    "attack_boost": 0,
    "defense_boost": 0,
    "special_attack_boost": 0,
    "special_defense_boost": 0,
    "speed_boost": 0,
    "accuracy_boost": 0,
    "evasion_boost": 0,
    "status": None,
    "terastallized": False,
    "types": ["water"],
    "volatile_status": [],
    "moves": [
        {"id": "watergun", "disabled": False, "current_pp": 32},
        {"id": "tackle", "disabled": False, "current_pp": 32},
    ],
}

OPPONENT_TEAM = dict(
    CHALLENGER_TEAM,
    id="charmander",
    types=["fire"],
    moves=[
        {"id": "ember", "disabled": False, "current_pp": 32},
        {"id": "scratch", "disabled": False, "current_pp": 32},
    ],
)

ROUNDS = (
    ("watergun", "ember"),
    ("tackle", "scratch"),
)


def _stub_package(name, path=None):
    module = types.ModuleType(name)
    module.__path__ = [path] if path else []
    sys.modules[name] = module
    return module


def _load():
    """Import the resolver without Anki — the probe runs from a shell."""
    if SRC not in sys.path:
        sys.path.insert(0, SRC)
    _stub_package("Ankimon", ANKIMON)
    _stub_package("Ankimon.multiplayer", os.path.join(ANKIMON, "multiplayer"))
    _stub_package("Ankimon.poke_engine", os.path.join(ANKIMON, "poke_engine"))
    import importlib

    return (
        importlib.import_module("Ankimon.multiplayer.pvp_resolution"),
        importlib.import_module("Ankimon.multiplayer.engine_version"),
    )


def probe() -> dict:
    pvp, engine_version_module = _load()

    rows = []
    for seed in SEEDS:
        carried = None
        for round_number, (challenger_move, opponent_move) in enumerate(ROUNDS, start=1):
            result = pvp.resolve_round(
                challenger_move,
                opponent_move,
                seed,
                challenger_team=json.dumps(CHALLENGER_TEAM, sort_keys=True),
                opponent_team=json.dumps(OPPONENT_TEAM, sort_keys=True),
                carried_state=carried,
            )
            # The second round starts from the first one's state, so a
            # divergence that only shows up once a battle is under way -
            # a status, a boost, a side condition - is caught too.
            carried = pvp.dump_state(result.state)
            rows.append(
                {
                    "seed": seed,
                    "round": round_number,
                    "state_hash": result.state_hash,
                    "log_digest": result.log_digest,
                    "hp": result.hp,
                }
            )

    engine = engine_version_module.engine_version()
    data_digest = engine_version_module.engine_data_digest()
    fingerprint = hashlib.sha256(
        json.dumps({"engine": engine, "rounds": rows}, sort_keys=True).encode("utf-8")
    ).hexdigest()[:16]

    return {
        "fingerprint": fingerprint,
        "engine_version": engine,
        "engine_data_digest": data_digest,
        "python": platform.python_version(),
        "platform": platform.platform(),
        "rounds": rows,
    }


def render(report: dict) -> str:
    lines = [
        "PvP determinism probe",
        "  fingerprint        {}".format(report["fingerprint"]),
        "  engine_version     {}".format(report["engine_version"]),
        "  engine data digest {}".format(report["engine_data_digest"]),
        "  python             {} on {}".format(report["python"], report["platform"]),
        "",
        "  {:<18} {:<6} {:<20} {}".format("seed", "round", "state_hash (16)", "hp"),
    ]
    for row in report["rounds"]:
        lines.append(
            "  {:<18} {:<6} {:<20} {}".format(
                row["seed"],
                row["round"],
                row["state_hash"][:16],
                "{}/{}".format(row["hp"]["challenger"], row["hp"]["opponent"]),
            )
        )
    lines += [
        "",
        "Run this on the other machine and compare the fingerprint.",
    ]
    return "\n".join(lines)


def compare(report: dict, other: dict) -> int:
    if report["fingerprint"] == other["fingerprint"]:
        print("MATCH - these two clients would agree on a round.")
        return 0

    print("MISMATCH - these two clients would not agree on a round.\n")
    if report["engine_version"] != other["engine_version"]:
        # D3: a legitimate skew. The server refuses such a match with
        # "update Ankimon" rather than treating it as cheating.
        print("  engine_version differs:")
        print("    this : {}".format(report["engine_version"]))
        print("    other: {}".format(other["engine_version"]))
        if report["engine_data_digest"] != other["engine_data_digest"]:
            print("  the bundled move/pokedex data differs (D3).")
        else:
            print("  the addon version differs but the data matches (D3).")
        return 1

    # Same engine, same data, different result: the dangerous one.
    print("  engine_version matches, so this is a representation")
    print("  difference (D4) - a float, set order or dict order that is not")
    print("  stable across these two builds. First differing round:\n")
    for mine, theirs in zip(report["rounds"], other["rounds"]):
        if mine != theirs:
            print("    seed {} round {}".format(mine["seed"], mine["round"]))
            print("      this : hash {} hp {}".format(mine["state_hash"][:16], mine["hp"]))
            print("      other: hash {} hp {}".format(theirs["state_hash"][:16], theirs["hp"]))
            break
    return 1


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--json", action="store_true", help="print the raw report")
    parser.add_argument(
        "--compare",
        metavar="REPORT.json",
        help="compare against another machine's --json output",
    )
    args = parser.parse_args(argv)

    report = probe()
    if args.compare:
        with open(args.compare, "r", encoding="utf-8") as handle:
            other = json.load(handle)
        return compare(report, other)
    if args.json:
        print(json.dumps(report, sort_keys=True, indent=2))
        return 0
    print(render(report))
    return 0


if __name__ == "__main__":
    sys.exit(main())
