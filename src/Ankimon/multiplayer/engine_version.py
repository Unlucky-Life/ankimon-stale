"""Identity of the battle engine a client is running (Phase D, item D3).

Two clients on different addon versions can hold different move data or base
stats and legitimately compute different outcomes for the same round. That is
a version skew, not cheating, and Phase D has to tell the two apart: a
mismatch here is answered with "update Ankimon to battle this player", never
with a loss.

The version string is the addon version plus a digest of the engine data that
actually decides a turn.

Two deliberate narrowings:

- Only `moves.json` and `pokedex.json` are hashed. The other data files drive
  team *generation*, which no PvP round consults, and including them would
  turn harmless differences into refused matches.
- Line endings are normalized before hashing. The same file checked out on
  Windows and on Linux can differ by CRLF alone, which changes no data and
  must not split the player base in half.
"""

import hashlib
import json
from pathlib import Path

# Data files whose contents can change how a turn resolves.
RESOLUTION_DATA_FILES = ("moves.json", "pokedex.json")

DIGEST_LENGTH = 12

_ENGINE_DATA_DIR = Path(__file__).parents[1] / "poke_engine" / "data"
_MANIFEST_PATH = Path(__file__).parents[1] / "manifest.json"

_cached_version = None


def _normalized_bytes(path: Path) -> bytes:
    # CRLF vs LF is a checkout artifact, not a data difference; bare CR is
    # folded in for the same reason.
    #
    # A file that has been through CRLF translation *twice* holds \r\r\n,
    # which is genuinely ambiguous - one newline or two - and comes out as a
    # different digest. That is the right answer: such a file is damaged, and
    # reporting a version mismatch is better than pretending it matches.
    return path.read_bytes().replace(b"\r\n", b"\n").replace(b"\r", b"\n")


def engine_data_digest(data_dir=None) -> str:
    """Digest of the engine data files that affect resolution."""
    directory = Path(data_dir) if data_dir else _ENGINE_DATA_DIR
    digest = hashlib.sha256()
    for filename in sorted(RESOLUTION_DATA_FILES):
        path = directory / filename
        # The filename goes into the hash too, so two files swapping contents
        # cannot produce the same digest.
        digest.update(filename.encode("utf-8"))
        digest.update(b"\0")
        digest.update(_normalized_bytes(path))
        digest.update(b"\0")
    return digest.hexdigest()[:DIGEST_LENGTH]


def addon_version(manifest_path=None) -> str:
    path = Path(manifest_path) if manifest_path else _MANIFEST_PATH
    with open(path, "r", encoding="utf-8") as f:
        return str(json.load(f).get("version") or "unknown")


def engine_version(data_dir=None, manifest_path=None) -> str:
    """The string clients exchange, e.g. ``1.52-E+3f9a1c0b2d4e``.

    Cached: the data files cannot change while Anki is running, and hashing
    them is a couple of megabytes of reads.
    """
    global _cached_version
    if data_dir is None and manifest_path is None:
        if _cached_version is None:
            _cached_version = "{}+{}".format(addon_version(), engine_data_digest())
        return _cached_version
    return "{}+{}".format(addon_version(manifest_path), engine_data_digest(data_dir))
