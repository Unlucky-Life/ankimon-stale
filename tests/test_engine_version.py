"""Tests for the engine version string (Phase D, item D3)."""

import importlib.util
import json
import os
import sys
import types

import pytest

SRC = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "src"))
ANKIMON = os.path.join(SRC, "Ankimon")


def _stub_package(name, path=None):
    module = types.ModuleType(name)
    module.__path__ = [path] if path else []
    sys.modules[name] = module
    return module


@pytest.fixture
def engine_version():
    saved = dict(sys.modules)
    try:
        _stub_package("Ankimon", ANKIMON)
        _stub_package("Ankimon.multiplayer", os.path.join(ANKIMON, "multiplayer"))
        name = "Ankimon.multiplayer.engine_version"
        spec = importlib.util.spec_from_file_location(
            name, os.path.join(ANKIMON, "multiplayer", "engine_version.py")
        )
        module = importlib.util.module_from_spec(spec)
        sys.modules[name] = module
        spec.loader.exec_module(module)
        yield module
    finally:
        sys.modules.clear()
        sys.modules.update(saved)


@pytest.fixture
def data_dir(tmp_path):
    directory = tmp_path / "data"
    directory.mkdir()
    # write_bytes, not write_text: on Windows write_text translates \n to \r\n,
    # which would make the line-ending test assert against the wrong baseline.
    (directory / "moves.json").write_bytes(b'{"tackle": {"basePower": 40}}\n')
    (directory / "pokedex.json").write_bytes(b'{"squirtle": {"num": 7}}\n')
    return directory


@pytest.fixture
def manifest(tmp_path):
    path = tmp_path / "manifest.json"
    path.write_text(json.dumps({"name": "Ankimon", "version": "1.52-E"}), encoding="utf-8")
    return path


def test_version_combines_addon_version_and_data_digest(engine_version, data_dir, manifest):
    version = engine_version.engine_version(data_dir, manifest)
    prefix, _, digest = version.partition("+")
    assert prefix == "1.52-E"
    assert len(digest) == engine_version.DIGEST_LENGTH


def test_same_data_gives_the_same_digest(engine_version, data_dir, manifest):
    assert engine_version.engine_version(data_dir, manifest) == engine_version.engine_version(
        data_dir, manifest
    )


def test_changed_move_data_changes_the_digest(engine_version, data_dir, manifest):
    before = engine_version.engine_version(data_dir, manifest)
    (data_dir / "moves.json").write_text('{"tackle": {"basePower": 50}}\n', encoding="utf-8")
    assert engine_version.engine_version(data_dir, manifest) != before


def test_line_endings_do_not_change_the_digest(engine_version, data_dir, manifest):
    # The same file checked out on Windows and Linux differs by CRLF alone.
    # That is not a data difference and must not refuse the match.
    before = engine_version.engine_data_digest(data_dir)
    for name in engine_version.RESOLUTION_DATA_FILES:
        path = data_dir / name
        path.write_bytes(path.read_bytes().replace(b"\n", b"\r\n"))
    assert engine_version.engine_data_digest(data_dir) == before

    # Bare CR (a classic-Mac checkout) folds in too.
    for name in engine_version.RESOLUTION_DATA_FILES:
        path = data_dir / name
        path.write_bytes(path.read_bytes().replace(b"\r\n", b"\r"))
    assert engine_version.engine_data_digest(data_dir) == before


def test_team_generation_data_is_not_part_of_the_digest(engine_version, data_dir):
    # Team datasets never take part in resolving a round; hashing them would
    # turn a harmless difference into a refused match.
    before = engine_version.engine_data_digest(data_dir)
    (data_dir / "team_datasets.json").write_text("{}", encoding="utf-8")
    (data_dir / "random_battle_sets.json").write_text("{}", encoding="utf-8")
    assert engine_version.engine_data_digest(data_dir) == before


def test_swapping_two_files_contents_changes_the_digest(engine_version, data_dir):
    before = engine_version.engine_data_digest(data_dir)
    moves = (data_dir / "moves.json").read_bytes()
    pokedex = (data_dir / "pokedex.json").read_bytes()
    (data_dir / "moves.json").write_bytes(pokedex)
    (data_dir / "pokedex.json").write_bytes(moves)
    assert engine_version.engine_data_digest(data_dir) != before


def test_real_addon_reports_a_version(engine_version):
    version = engine_version.engine_version()
    prefix, sep, digest = version.partition("+")
    assert sep == "+" and prefix and len(digest) == engine_version.DIGEST_LENGTH
    # Second call must hit the cache and agree.
    assert engine_version.engine_version() == version
