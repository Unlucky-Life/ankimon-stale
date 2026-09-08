"""Tests for the cross-machine determinism probe (Phase D).

The probe is the evidence that gates `PVP_HUMAN_ENABLED`, so it has to be
worth trusting: a fingerprint that changed between runs on one machine would
make every honest cross-machine comparison look like a mismatch, and a
comparison that blamed the wrong thing would send people chasing a data
difference that is really a build difference.

The `PYTHONHASHSEED` case is the closest thing to a second machine that one
machine can offer: it is the knob that reorders sets and dicts, which is
exactly the D4 instability the probe exists to catch.
"""

import json
import os
import subprocess
import sys

import pytest

REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
PROBE = os.path.join(REPO, "tools", "pvp_determinism_probe.py")


def run_probe(env_overrides=None, args=("--json",)):
    env = dict(os.environ)
    env.update(env_overrides or {})
    completed = subprocess.run(
        [sys.executable, PROBE, *args],
        capture_output=True,
        text=True,
        env=env,
        cwd=REPO,
    )
    assert completed.returncode == 0, completed.stderr
    return completed.stdout


@pytest.fixture(scope="module")
def report():
    return json.loads(run_probe())


def test_the_probe_reports_a_fingerprint_and_its_inputs(report):
    assert len(report["fingerprint"]) == 16
    assert report["engine_version"].endswith(report["engine_data_digest"])
    assert report["rounds"], "a probe with no rounds proves nothing"


def test_every_round_is_actually_resolved(report):
    for row in report["rounds"]:
        assert len(row["state_hash"]) == 64
        assert row["hp"]["challenger"] >= 0 and row["hp"]["opponent"] >= 0
    # The second round continues from the first, so it must not repeat it -
    # otherwise a divergence that only appears mid-battle would go unseen.
    first, second = report["rounds"][0], report["rounds"][1]
    assert first["state_hash"] != second["state_hash"]


def test_the_fingerprint_survives_a_reordered_hash_seed():
    # Sets and dicts iterate differently under a different PYTHONHASHSEED.
    # If that reached the hash, two honest clients would disagree.
    a = json.loads(run_probe({"PYTHONHASHSEED": "0"}))
    b = json.loads(run_probe({"PYTHONHASHSEED": "12345"}))
    assert a["fingerprint"] == b["fingerprint"]
    assert a["rounds"] == b["rounds"]


def test_the_human_output_leads_with_the_fingerprint():
    text = run_probe(args=())
    assert "fingerprint" in text
    assert "compare the fingerprint" in text


def _write(tmp_path, report, name="other.json"):
    path = tmp_path / name
    path.write_text(json.dumps(report), encoding="utf-8")
    return str(path)


def test_comparing_a_machine_against_itself_matches(report, tmp_path):
    completed = subprocess.run(
        [sys.executable, PROBE, "--compare", _write(tmp_path, report)],
        capture_output=True,
        text=True,
        cwd=REPO,
    )
    assert completed.returncode == 0
    assert "MATCH" in completed.stdout


def test_a_data_difference_is_reported_as_version_skew(report, tmp_path):
    # D3: legitimate, and answered with "update Ankimon", not with a loss.
    other = json.loads(json.dumps(report))
    other["fingerprint"] = "0" * 16
    other["engine_version"] = "1.52-E+deadbeefcafe"
    other["engine_data_digest"] = "deadbeefcafe"
    completed = subprocess.run(
        [sys.executable, PROBE, "--compare", _write(tmp_path, other)],
        capture_output=True,
        text=True,
        cwd=REPO,
    )
    assert completed.returncode == 1
    assert "MISMATCH" in completed.stdout
    assert "engine_version differs" in completed.stdout
    assert "(D3)" in completed.stdout


def test_a_same_data_difference_is_reported_as_representation_drift(report, tmp_path):
    # The dangerous case: same engine, same data, different result.
    other = json.loads(json.dumps(report))
    other["fingerprint"] = "0" * 16
    other["rounds"][1]["state_hash"] = "f" * 64
    completed = subprocess.run(
        [sys.executable, PROBE, "--compare", _write(tmp_path, other)],
        capture_output=True,
        text=True,
        cwd=REPO,
    )
    assert completed.returncode == 1
    assert "(D4)" in completed.stdout
    # And it points at the round that differs, not just "something differs".
    assert other["rounds"][1]["seed"] in completed.stdout
