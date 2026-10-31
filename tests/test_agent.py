"""F11: the agent layer must be optional, auditable and traceable to tool output.

These tests treat the agent as an untrusted narrator: they read its run record
and prove, mechanically, that (a) the main pipeline never imports it, (b) it
never touches the filesystem, (c) every number it quotes came out of a tool
response in the same run, and (d) it labels the result as a simulation.
"""

from __future__ import annotations

import ast
import json
import re
import subprocess
import sys
from pathlib import Path

import pytest

from bike_agent.agent import ToolGateway, render_markdown, replay_review
from bike_agent.config import Layout, load_config

ROOT = Path(__file__).resolve().parents[1]
AGENT_DIR = ROOT / "bike_agent" / "agent"
FORBIDDEN_READS = {"open", "read_csv", "read_parquet", "read_json", "read_text", "read_bytes", "read_excel"}
FORBIDDEN_DIRS = ("outputs/", "data/processed")
NUMBER = re.compile(r"\d+(?:\.\d+)?")


def _agent_sources() -> list[Path]:
    return sorted(AGENT_DIR.glob("*.py"))


def _has_artifacts() -> bool:
    layout = Layout()
    return layout.sim_path("xm", "rule", "csv").exists()


needs_artifacts = pytest.mark.skipif(not _has_artifacts(), reason="run `bike-agent repro` first")


@pytest.fixture(scope="module")
def record() -> dict:
    if not _has_artifacts():
        pytest.skip("run `bike-agent repro` first")
    gateway = ToolGateway(load_config(), Layout())
    return replay_review(gateway, source="xm", strategy="rule", top_k=5).to_dict()


# -- F11-3 ------------------------------------------------------------------
def test_agent_never_reads_files_or_artifact_directories():
    """AST scan: no file-reading call, no artefact path literal."""
    offenders = []
    for path in _agent_sources():
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                name = ""
                if isinstance(node.func, ast.Name):
                    name = node.func.id
                elif isinstance(node.func, ast.Attribute):
                    name = node.func.attr
                if name in FORBIDDEN_READS:
                    offenders.append(f"{path.name}:{node.lineno} calls {name}()")
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                for directory in FORBIDDEN_DIRS:
                    if directory in node.value:
                        offenders.append(f"{path.name}:{node.lineno} mentions {directory!r}")
    assert not offenders, "agent layer reads artefacts directly:\n" + "\n".join(offenders)


def test_agent_only_imports_the_tool_handlers():
    """Every project import inside the agent package must be a tool contract."""
    allowed_prefixes = ("bike_agent.tools.handlers", "bike_agent.agent")
    offenders = []
    for path in _agent_sources():
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module and node.module.startswith("bike_agent"):
                if not node.module.startswith(allowed_prefixes):
                    offenders.append(f"{path.name}:{node.lineno} imports {node.module}")
            if isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.name.startswith("bike_agent") and not alias.name.startswith(allowed_prefixes):
                        offenders.append(f"{path.name}:{node.lineno} imports {alias.name}")
    assert not offenders, "agent imports outside the tool contracts:\n" + "\n".join(offenders)


# -- F11-1 / F11-2 ----------------------------------------------------------
_BLOCKER = r"""
import sys, importlib.abc

class Blocker(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname == "bike_agent.agent" or fullname.startswith("bike_agent.agent."):
            raise ImportError("agent layer is not installed")
        return None

sys.meta_path.insert(0, Blocker())

import bike_agent.cli
from bike_agent.cli import build_parser, main

parser = build_parser()
commands = sorted(parser._subparsers._group_actions[0].choices)
assert "repro" in commands and "serve" in commands
assert main(["tools", "--schema"]) == 0
assert main(["serve", "--check"]) == 0
leaked = sorted(m for m in sys.modules if m.startswith("bike_agent.agent"))
assert not leaked, leaked
print("OK")
"""


@needs_artifacts
def test_pipeline_runs_when_the_agent_layer_cannot_be_imported():
    """F11-1: removing the module must not disturb anything else."""
    result = subprocess.run(
        [sys.executable, "-c", _BLOCKER], cwd=ROOT, capture_output=True, text=True, timeout=600
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip().endswith("OK")


def _import_closure(entry: Path) -> set[str]:
    """Every ``bike_agent`` module reachable from ``entry`` by *module-level* imports.

    Function-local imports are excluded on purpose: they run only when their
    function is called, and ``cmd_agent`` deliberately imports the agent layer
    that way so ``repro`` never pays for it.
    """
    seen: set[str] = set()
    queue = [entry]
    while queue:
        path = queue.pop()
        if not path.exists():
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        modules: list[str] = []
        for node in tree.body:  # module scope only
            if isinstance(node, ast.ImportFrom) and node.module:
                modules.append(node.module)
                modules += [f"{node.module}.{alias.name}" for alias in node.names]
            elif isinstance(node, ast.Import):
                modules += [alias.name for alias in node.names]
        for module in modules:
            if not module.startswith("bike_agent") or module in seen:
                continue
            seen.add(module)
            queue.append(ROOT / (module.replace(".", "/") + ".py"))
            queue.append(ROOT / module.replace(".", "/") / "__init__.py")
    return seen


def test_repro_import_path_does_not_reach_the_agent():
    """F11-2: the import graph behind ``bike-agent repro`` excludes the agent.

    A superset of the execution path, so passing here is strictly stronger than
    watching one run.  ``cmd_agent`` imports the layer lazily inside the
    function, which is why the module-level graph stays clean.
    """
    closure = _import_closure(ROOT / "bike_agent" / "cli.py")
    agent_modules = sorted(m for m in closure if m.startswith("bike_agent.agent"))
    assert not agent_modules, f"repro's import graph reaches {agent_modules}"

    # And the lazy import really is lazy: importing the CLI must not load it.
    result = subprocess.run(
        [sys.executable, "-c", "import bike_agent.cli, sys; "
         "print(sorted(m for m in sys.modules if m.startswith('bike_agent.agent')))"],
        cwd=ROOT, capture_output=True, text=True, timeout=120,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "[]"


# -- F11-4 ------------------------------------------------------------------
@needs_artifacts
def test_run_record_is_complete_and_readable(record, tmp_path):
    assert record["task"] == "replay_review"
    assert record["steps"], "a run must record at least one tool call"
    for step in record["steps"]:
        assert step["tool"] in {"get_forecast", "get_risk_ranking", "run_simulation"}
        assert isinstance(step["input"], dict)
        assert step["return_summary"], "each step needs a readable summary"
        assert isinstance(step["return"], dict), "the raw response must be kept for audit"
    assert record["conclusion"]

    markdown = render_markdown(record)
    assert "调用轨迹" in markdown and "结论" in markdown
    for step in record["steps"]:
        assert step["tool"] in markdown
    assert record["conclusion"] in markdown


# -- F11-5 ------------------------------------------------------------------
@needs_artifacts
def test_every_quoted_number_appears_in_a_tool_response(record):
    """No number may enter the conclusion that a tool did not actually return."""
    haystack = "\n".join(
        json.dumps(step["return"], ensure_ascii=False, sort_keys=True) for step in record["steps"]
    )
    quoted = NUMBER.findall(record["conclusion"])
    assert quoted, "the conclusion should cite numbers"
    unmatched = sorted({token for token in quoted if token not in haystack})
    assert not unmatched, f"numbers with no tool-response source: {unmatched}"


@needs_artifacts
def test_step_summaries_are_also_traceable(record):
    haystack = "\n".join(
        json.dumps(step["return"], ensure_ascii=False, sort_keys=True) for step in record["steps"]
    )
    for step in record["steps"]:
        unmatched = sorted({t for t in NUMBER.findall(step["return_summary"]) if t not in haystack})
        assert not unmatched, f"{step['tool']} summary quotes unsourced numbers: {unmatched}"


# -- F11-6 ------------------------------------------------------------------
@needs_artifacts
def test_record_is_labelled_as_a_simulation(record):
    text = "\n".join([record["conclusion"], *record["notes"], render_markdown(record)])
    assert "模拟" in text
    for phrase in ("真实运营", "实际节省"):
        assert phrase not in text, f"record must not claim {phrase!r}"


@needs_artifacts
def test_cli_writes_both_records():
    result = subprocess.run(
        [sys.executable, "-m", "bike_agent.cli", "agent", "--source", "xm", "--strategy", "rule"],
        cwd=ROOT, capture_output=True, text=True, timeout=600,
    )
    assert result.returncode == 0, result.stderr
    json_path = ROOT / "outputs" / "agent_run.xm.json"
    md_path = ROOT / "outputs" / "agent_run.xm.md"
    assert json_path.exists() and md_path.exists()
    payload = json.loads(json_path.read_text(encoding="utf-8"))
    assert payload["steps"] and payload["conclusion"]
    assert "模拟" in md_path.read_text(encoding="utf-8")


# -- attribution honesty ----------------------------------------------------
@needs_artifacts
def test_ranking_from_a_different_bucket_is_not_used_to_explain_the_peak(record):
    """The risk ranking covers the horizon's last bucket, not necessarily the peak.

    When the two differ the conclusion must say so and must not present the
    ranking's cells as the cause of the peak bucket's loss.
    """
    steps = record["steps"]
    ranking = next(s for s in steps if s["tool"] == "get_risk_ranking")["return"]
    simulation = next(s for s in steps if s["tool"] == "run_simulation")["return"]
    peak = max(
        simulation["trajectory"],
        key=lambda row: (row["unmet_requests"], row["borrow_requests"]),
    )["time_bucket"]

    if str(ranking["time_bucket"]) == str(peak):
        assert "风险清单前三格" in record["conclusion"]
    else:
        assert str(ranking["time_bucket"]) in record["conclusion"], "the different bucket must be named"
        assert "不用于解释本单元的损失" in record["conclusion"]
        assert "该单元的风险清单前三格" not in record["conclusion"]


@needs_artifacts
def test_reusing_a_gateway_does_not_duplicate_audit_steps():
    """The planner must record only the calls it made, even on a shared gateway."""
    gateway = ToolGateway(load_config(), Layout())
    first = replay_review(gateway, source="xm", strategy="rule", top_k=3)
    second = replay_review(gateway, source="xm", strategy="rule", top_k=3)
    assert len(first.to_dict()["steps"]) == len(second.to_dict()["steps"]) == 3
    assert [s["tool"] for s in second.to_dict()["steps"]] == [
        "run_simulation",
        "get_forecast",
        "get_risk_ranking",
    ]
