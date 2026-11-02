"""F27: the ``bike-agent multi-agent`` entry point and its failure paths."""

from __future__ import annotations

import inspect
import json
from pathlib import Path

import pytest

from bike_agent import cli


def test_parser_accepts_the_documented_invocation():
    parser = cli.build_parser()
    args = parser.parse_args(
        [
            "multi-agent",
            "--source",
            "xm",
            "--mode",
            "deterministic",
            "--max-budget",
            "400",
            "--min-service-rate",
            "0.92",
            "--max-rounds",
            "2",
        ]
    )
    assert args.func is cli.cmd_multi_agent
    assert args.source == "xm"
    assert args.mode == "deterministic"
    assert args.max_budget == 400
    assert args.min_service_rate == 0.92
    assert args.max_rounds == 2

def test_invalid_budget_ceiling_exits_nonzero_without_a_run(tmp_path):
    # build_task fails before the orchestrator (and therefore before any run
    # directory) is created.
    code = cli.main(["multi-agent", "--source", "xm", "--max-budget", "50"])
    assert code == 1

def test_missing_inputs_produce_a_failed_audit_run(tmp_path, capsys):
    code = cli.main(
        ["--root", str(tmp_path), "multi-agent", "--source", "xm", "--mode", "deterministic"]
    )
    assert code == 1
    runs = list((tmp_path / "outputs" / "multi_agent").glob("*/decision.json"))
    assert len(runs) == 1
    decision = json.loads(runs[0].read_text(encoding="utf-8"))
    assert decision["terminal_state"] == "failed"
    assert "missing" in decision["reason"] or "FileNotFound" in decision["reason"]

class _FakeOutcome:
    def __init__(self, exit_code: int, run_dir: Path):
        self.exit_code = exit_code
        self.run_dir = run_dir

    def summary(self) -> dict:
        return {"run_id": self.run_dir.name, "terminal_state": "accepted", "exit_code": self.exit_code}

def test_cli_propagates_the_terminal_exit_code(monkeypatch, tmp_path):
    class FakeOrchestrator:
        def __init__(self, config, layout, task):
            self.task = task

        def run(self):
            return _FakeOutcome(0, tmp_path / "run")

    monkeypatch.setattr(
        "bike_agent.multi_agent.orchestrator.Orchestrator", FakeOrchestrator
    )
    assert cli.main(["multi-agent", "--source", "xm", "--mode", "deterministic"]) == 0

    class FailingOrchestrator(FakeOrchestrator):
        def run(self):
            return _FakeOutcome(1, tmp_path / "run-failed")

    monkeypatch.setattr(
        "bike_agent.multi_agent.orchestrator.Orchestrator", FailingOrchestrator
    )
    assert cli.main(["multi-agent", "--source", "sh", "--mode", "deterministic"]) == 1

def test_repro_does_not_reference_the_multi_agent_package():
    source = inspect.getsource(cli.cmd_repro)
    assert "multi_agent" not in source
    module_source = (Path(cli.__file__)).read_text(encoding="utf-8")
    # The only import is lazy, inside cmd_multi_agent.
    assert "from bike_agent.multi_agent" in module_source
    assert module_source.count("from bike_agent.multi_agent") == 2

def test_unknown_mode_is_rejected_by_argparse():
    with pytest.raises(SystemExit) as error:
        cli.build_parser().parse_args(["multi-agent", "--source", "xm", "--mode", "llm"])
    assert error.value.code == 2

def test_repro_site_import_does_not_load_the_agent_runtime(tmp_path):
    """NF-34: building the site may read the contracts, never run a role.

    ``bike-agent repro`` imports the replay builder, and the builder imports the
    collaboration page, which imports ``multi_agent.contracts`` for labels.
    Everything that could execute an agent (orchestrator, gateway, roles,
    message store, task freezing) must stay out of that import graph.
    """
    import subprocess
    import sys

    blocker = """
import sys, importlib.abc

BLOCKED = (
    "bike_agent.multi_agent.orchestrator",
    "bike_agent.multi_agent.gateway",
    "bike_agent.multi_agent.roles",
    "bike_agent.multi_agent.messages",
    "bike_agent.multi_agent.task",
    "bike_agent.multi_agent.audit",
)

class Blocker(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if any(fullname == name or fullname.startswith(name + ".") for name in BLOCKED):
            raise ImportError("agent runtime blocked: " + fullname)
        return None

sys.meta_path.insert(0, Blocker())
import bike_agent.replay.site  # noqa: F401
from bike_agent.presentation import collab  # noqa: F401
leaked = sorted(name for name in sys.modules if name in BLOCKED)
assert not leaked, leaked
print("OK")
"""
    result = subprocess.run(
        [sys.executable, "-c", blocker],
        cwd=Path(__file__).resolve().parents[1],
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip().endswith("OK")

def test_multi_agent_imports_and_runs_with_every_llm_sdk_blocked(tmp_path):
    """F27-3 / M32: no SDK, no credentials, no network -- still importable.

    The run itself fails on the empty root (that is the point: the failure is
    about missing inputs, not about a missing SDK), and nothing from the SDK
    list may end up in ``sys.modules``.
    """
    import subprocess
    import sys

    from test_zero_llm import LLM_SDKS

    blocker = r"""
import sys, importlib.abc

BLOCKED = set(%r)

class Blocker(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname.split(".")[0] in BLOCKED:
            raise ImportError("LLM SDK %%r is unavailable" %% fullname)
        return None

sys.meta_path.insert(0, Blocker())

from bike_agent.cli import main
# Missing inputs -> a failed audit run, exit 1.  Nothing here needs a model.
code = main(["--root", %r, "multi-agent", "--source", "xm", "--mode", "deterministic"])
assert code == 1, code
leaked = sorted(m for m in sys.modules if m.split(".")[0] in BLOCKED)
assert not leaked, leaked
print("OK")
""" % (LLM_SDKS, str(tmp_path))  # noqa: UP031 - %-format keeps the payload readable
    result = subprocess.run(
        [sys.executable, "-c", blocker],
        cwd=Path(__file__).resolve().parents[1],
        capture_output=True,
        text=True,
        timeout=300,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip().endswith("OK")
    runs = list((tmp_path / "outputs" / "multi_agent").glob("*/decision.json"))
    assert len(runs) == 1
    decision = json.loads(runs[0].read_text(encoding="utf-8"))
    assert decision["llm_connected"] is False
