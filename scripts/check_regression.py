#!/usr/bin/env python3
"""F8-3 / NF-7: prove that the Phase 2 additions changed no Phase 1 artefact.

Usage::

    python3 scripts/check_regression.py --snapshot   # before a change
    python3 scripts/check_regression.py              # after it

The snapshot records a sha256 per file plus the numeric payload of the four
artefacts F8-3 names explicitly, so a byte difference and a *value* difference
are both reported.  Byte equality alone is not enough: a reformatted CSV with
the same numbers must not be reported as a regression, and a single changed
digit must not slip through because the file was rewritten.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SNAPSHOT = ROOT / "scripts" / "phase1-snapshot.json"
TRACKED_DIRS = ("outputs", "reports", "figures")
TRACKED_ROOT_FILES = ("REPORT.md", "DECISIONS.md")
VALUE_FILES = (
    "outputs/sim_rule.xm.csv",
    "outputs/sim_optimized.xm.csv",
    "outputs/sim_rule.sh.csv",
    "outputs/sim_optimized.sh.csv",
    "outputs/sim_rule.xm.json",
    "outputs/sim_optimized.xm.json",
    "outputs/sim_rule.sh.json",
    "outputs/sim_optimized.sh.json",
    "outputs/strategy_comparison.xm.json",
    "outputs/strategy_comparison.sh.json",
    "outputs/key_numbers.json",
    "outputs/summary.xm.json",
    "outputs/summary.sh.json",
)
# Markdown is not value-checked, so a changed file in this set is reported as
# "text changed" rather than being waved through as additive.
TEXT_FILES = ("REPORT.md", "DECISIONS.md")


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _values(relative: str) -> object:
    """The numbers inside an artefact, independent of formatting.

    Only JSON and CSV are parsed.  Anything else returns ``None``, which makes
    the caller report the file as "changed, NOT value-checked" rather than
    silently reading prose as tabular data.
    """
    path = ROOT / relative
    if not path.exists():
        return None
    if path.suffix == ".json":
        return json.loads(path.read_text(encoding="utf-8"))
    if path.suffix != ".csv":
        return None
    import csv

    with path.open(newline="", encoding="utf-8") as handle:
        rows = list(csv.DictReader(handle))
    parsed = []
    for row in rows:
        entry = {}
        for key, value in row.items():
            try:
                entry[key] = float(value)
            except (TypeError, ValueError):
                entry[key] = value
        parsed.append(entry)
    return parsed


def snapshot() -> dict:
    files = {}
    for directory in TRACKED_DIRS:
        base = ROOT / directory
        if not base.exists():
            continue
        for path in sorted(base.rglob("*")):
            if path.is_file():
                files[str(path.relative_to(ROOT))] = _sha(path)
    for name in TRACKED_ROOT_FILES:
        path = ROOT / name
        if path.exists():
            files[name] = _sha(path)
    return {
        "files": files,
        "values": {
            name: _values(name)
            for name in (*VALUE_FILES, "outputs/summary.xm.json", "outputs/summary.sh.json")
        },
    }


def _preserved(old: object, new: object, path: str = "") -> list[str]:
    """Every path present in ``old`` must still hold the same value in ``new``.

    Additive change is allowed -- Phase 2 is *supposed* to add artefacts and to
    add rows to ``key_numbers.json``.  What must not happen is a value that
    already existed moving, disappearing, or being re-derived differently.
    """
    problems: list[str] = []
    if isinstance(old, dict) and isinstance(new, dict):
        for key, value in old.items():
            if key not in new:
                problems.append(f"{path}/{key} disappeared")
            else:
                problems.extend(_preserved(value, new[key], f"{path}/{key}"))
        return problems
    if isinstance(old, list) and isinstance(new, list):
        # Lists keyed by a stable identifier (key_numbers entries, CSV rows) are
        # matched by that key, so insertion order and new rows do not matter.
        key_field = _list_key(old)
        if key_field is not None:
            index = {str(item.get(key_field)): item for item in new if isinstance(item, dict)}
            for item in old:
                if not isinstance(item, dict):
                    continue
                identity = str(item.get(key_field))
                if identity not in index:
                    problems.append(f"{path}[{identity}] disappeared")
                else:
                    problems.extend(_preserved(item, index[identity], f"{path}[{identity}]"))
            return problems
        if len(old) > len(new):
            problems.append(f"{path} shrank from {len(old)} to {len(new)} rows")
        for position, value in enumerate(old[: len(new)]):
            problems.extend(_preserved(value, new[position], f"{path}[{position}]"))
        return problems
    if old != new:
        problems.append(f"{path or '<root>'}: {old!r} -> {new!r}")
    return problems


def _list_key(rows: list) -> str | None:
    """A field that identifies each row, when the list is a table."""
    for candidate in ("metric", "cell_id", "time_bucket"):
        if rows and all(isinstance(row, dict) and candidate in row for row in rows):
            return candidate
    return None


def compare(before: dict, after: dict) -> int:
    old_files, new_files = before["files"], after["files"]
    changed = sorted(name for name in old_files if name in new_files and old_files[name] != new_files[name])
    missing = sorted(name for name in old_files if name not in new_files)
    added = sorted(name for name in new_files if name not in old_files)

    value_problems: dict[str, list[str]] = {}
    text_changed: list[str] = []
    unverified: list[str] = []
    for name in changed:
        if name in TEXT_FILES:
            # Narrative files legitimately grow; they are called out, not hidden.
            text_changed.append(name)
            continue
        old_value, new_value = before["values"].get(name), after["values"].get(name)
        if old_value is None or new_value is None:
            unverified.append(name)
            continue
        problems = _preserved(old_value, new_value, name)
        if problems:
            value_problems[name] = problems

    print(f"pre-existing artefacts : {len(old_files)}")
    print(f"byte-identical         : {len(old_files) - len(changed) - len(missing)}")
    print(f"touched                : {len(changed)}")
    for name in changed:
        if name in value_problems:
            status = "VALUE CHANGED"
        elif name in text_changed:
            status = "text changed (narrative, not value-checked)"
        elif name in unverified:
            status = "changed, NOT value-checked"
        else:
            status = "additive only (values preserved)"
        print(f"   ~ {name}  [{status}]")
    print(f"missing                : {len(missing)}")
    for name in missing:
        print(f"   - {name}")
    print(f"added                  : {len(added)}")
    for name in added:
        print(f"   + {name}")

    if value_problems:
        print("\nVALUE DIFFERENCES in pre-existing artefacts:")
        for name, problems in value_problems.items():
            print(f"  {name}")
            for problem in problems[:20]:
                print(f"     {problem}")
        print("\nREGRESSION: a value that existed before Phase 2 no longer holds.")
        return 1
    if unverified:
        print("\nREGRESSION: a tracked artefact changed but has no recorded values to compare.")
        return 1

    print("\nOK: every pre-existing value is preserved; changes are purely additive.")
    if text_changed:
        print(f"    (narrative files updated: {', '.join(text_changed)})")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--snapshot", action="store_true", help="record the current state as the baseline")
    parser.add_argument("--baseline", default=str(SNAPSHOT), help="snapshot file to compare against")
    args = parser.parse_args(argv)

    baseline_path = Path(args.baseline)
    if args.snapshot:
        baseline_path.parent.mkdir(parents=True, exist_ok=True)
        baseline_path.write_text(json.dumps(snapshot(), indent=2), encoding="utf-8")
        print(f"snapshot written to {baseline_path}")
        return 0

    if not baseline_path.exists():
        print(f"no snapshot at {baseline_path}; run with --snapshot first", file=sys.stderr)
        return 2
    return compare(json.loads(baseline_path.read_text(encoding="utf-8")), snapshot())


if __name__ == "__main__":
    raise SystemExit(main())
