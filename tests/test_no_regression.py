"""NF-7 / F8-3: Phase 2 must not move any number Phase 1 already published.

``scripts/phase1-snapshot.json`` was captured before the first Phase 2 commit.
This test replays that comparison, so the "no regression" claim is checked on
every run rather than asserted once in a write-up.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "check_regression.py"
SNAPSHOT = ROOT / "scripts" / "phase1-snapshot.json"


def _load():
    spec = importlib.util.spec_from_file_location("check_regression", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def checker():
    if not SNAPSHOT.exists():
        pytest.skip("no Phase 1 snapshot to compare against")
    if not (ROOT / "outputs" / "key_numbers.json").exists():
        pytest.skip("run `bike-agent repro` first")
    return _load()


def test_no_phase1_value_moved(checker, capsys):
    import json

    before = json.loads(SNAPSHOT.read_text(encoding="utf-8"))
    after = checker.snapshot()
    exit_code = checker.compare(before, after)
    captured = capsys.readouterr().out
    assert exit_code == 0, captured


def test_the_four_named_f8_3_artefacts_are_byte_identical(checker):
    """F8-3 names these files explicitly; for them, byte equality is required."""
    import json

    before = json.loads(SNAPSHOT.read_text(encoding="utf-8"))["files"]
    after = checker.snapshot()["files"]
    named = [
        name
        for name in before
        if name.startswith(("outputs/sim_rule.", "outputs/sim_optimized."))
        or name.startswith("outputs/strategy_comparison.")
        or name == "outputs/key_numbers.json"
    ]
    # key_numbers.json legitimately gains rows; the simulation artefacts do not move.
    strict = [name for name in named if name != "outputs/key_numbers.json"]
    assert strict, "the snapshot should cover the simulation artefacts"
    moved = [name for name in strict if before[name] != after.get(name)]
    assert not moved, f"F8-3 artefacts changed: {moved}"
