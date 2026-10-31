"""NF-8 / F7-3: the main pipeline must not depend on any LLM SDK.

The environment this project is developed in happens to have ``openai``
installed for unrelated reasons, so "the test suite passes" is not by itself
proof of the zero-LLM boundary.  These tests prove it structurally instead:
nothing the pipeline imports is an LLM SDK, and nothing it imports names one.
"""

from __future__ import annotations

import ast
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
PACKAGE = ROOT / "bike_agent"

LLM_SDKS = (
    "openai",
    "anthropic",
    "langchain",
    "langchain_core",
    "llama_index",
    "transformers",
    "cohere",
    "mistralai",
    "vertexai",
    "google",
    "ollama",
    "litellm",
)


def _package_sources() -> list[Path]:
    """Every module in the package except the optional agent layer (F11)."""
    return [
        path
        for path in sorted(PACKAGE.rglob("*.py"))
        if "agent" not in path.relative_to(PACKAGE).parts
    ]


def test_no_module_imports_an_llm_sdk():
    offenders = []
    for path in _package_sources():
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            modules: list[str] = []
            if isinstance(node, ast.ImportFrom) and node.module:
                modules.append(node.module)
            elif isinstance(node, ast.Import):
                modules += [alias.name for alias in node.names]
            for module in modules:
                root = module.split(".")[0]
                if root in LLM_SDKS:
                    offenders.append(f"{path.relative_to(ROOT)}:{node.lineno} imports {module}")
    assert not offenders, "LLM SDK imports in the main pipeline:\n" + "\n".join(offenders)


def test_no_dependency_declares_an_llm_sdk():
    import tomllib

    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]
    declared = project["dependencies"] + list(project.get("optional-dependencies", {}).get("dev", []))
    offenders = [
        spec
        for spec in declared
        if spec.split(">")[0].split("=")[0].split("[")[0].strip().lower() in LLM_SDKS
    ]
    assert not offenders, f"pyproject declares LLM SDKs: {offenders}"


def test_pipeline_imports_cleanly_with_every_llm_sdk_blocked():
    """Import the CLI with LLM SDKs unavailable, then exercise a real command."""
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
assert main(["tools", "--schema"]) == 0
assert main(["serve", "--check"]) == 0
leaked = sorted(m for m in sys.modules if m.split(".")[0] in BLOCKED)
assert not leaked, leaked
print("OK")
""" % (LLM_SDKS,)
    result = subprocess.run(
        [sys.executable, "-c", blocker], cwd=ROOT, capture_output=True, text=True, timeout=600
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip().endswith("OK")


def test_repro_entry_point_is_reachable_without_llm_sdks():
    """`bike-agent repro` itself must import nothing that needs an LLM SDK."""
    blocker = r"""
import sys, importlib.abc

BLOCKED = set(%r)

class Blocker(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname.split(".")[0] in BLOCKED:
            raise ImportError(fullname)
        return None

sys.meta_path.insert(0, Blocker())

# Import the repro path without running it (the pipeline is exercised by repro).
from bike_agent.cli import build_parser, cmd_repro
parser = build_parser()
args = parser.parse_args(["repro", "--sources", "xm"])
assert callable(cmd_repro)
print("OK")
""" % (LLM_SDKS,)
    result = subprocess.run(
        [sys.executable, "-c", blocker], cwd=ROOT, capture_output=True, text=True, timeout=300
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip().endswith("OK")


@pytest.mark.parametrize("sdk", ["openai", "anthropic"])
def test_the_guard_actually_detects_a_violation(sdk, tmp_path):
    """Sanity-check the detector: a file importing an SDK must be flagged."""
    sample = tmp_path / "violating.py"
    sample.write_text(f"import {sdk}\n", encoding="utf-8")
    tree = ast.parse(sample.read_text(encoding="utf-8"))
    found = [
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.Import)
        for alias in node.names
        if alias.name.split(".")[0] in LLM_SDKS
    ]
    assert found == [sdk]
