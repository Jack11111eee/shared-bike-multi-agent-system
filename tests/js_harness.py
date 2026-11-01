"""Run the shipped workbench scripts in a real JS engine (node), no browser.

NF-27 asks for behaviour verification, not just "the control exists".  The
navigation rules live in ``replay/workbench-core.js``, which never touches the
DOM, so node can drive it directly against the *published* ``presentation.js``
and ``replay-data.js`` files -- the same bytes a browser loads.

No extra dependency is installed: ``vm`` and ``fs`` are part of node.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SITE = ROOT / "replay"


def node_path() -> str | None:
    return shutil.which("node")


def run_node(body: str, sources: tuple[str, ...] = ("xm", "sh"), timeout: int = 120) -> dict:
    """Load the published bundles + core, run ``body``, return its JSON result.

    ``body`` runs with ``core`` (a state machine for the first source) and
    ``window`` in scope, and must ``console.log(JSON.stringify(...))`` once.
    """
    node = node_path()
    if node is None:
        raise RuntimeError("node is required to verify the workbench state machine")
    pairs = "\n".join(
        f"""vm.runInContext(fs.readFileSync(path.join(SITE, {source!r}, 'replay-data.js'), 'utf8'), ctx);
vm.runInContext(fs.readFileSync(path.join(SITE, {source!r}, 'presentation.js'), 'utf8'), ctx);"""
        for source in sources
    )
    driver = f"""
const fs = require('fs'), path = require('path'), vm = require('vm');
const SITE = {str(SITE)!r};
const ctx = {{ window: {{}}, console: console }};
vm.createContext(ctx);
vm.runInContext(fs.readFileSync(path.join(SITE, 'workbench-core.js'), 'utf8'), ctx);
{pairs}
const window = ctx.window;
const P = window.PRESENTATION_DATA;
const core = window.WorkbenchCore.create({{ source: {sources[0]!r}, sources: P }});
{body}
"""
    with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False, encoding="utf-8") as handle:
        handle.write(driver)
        script = handle.name
    try:
        completed = subprocess.run(
            [node, script], capture_output=True, text=True, timeout=timeout, cwd=str(ROOT), check=False
        )
    finally:
        Path(script).unlink(missing_ok=True)
    if completed.returncode != 0:
        raise AssertionError(
            f"node driver failed ({completed.returncode}):\n{completed.stderr[-4000:]}"
        )
    lines = [line for line in completed.stdout.splitlines() if line.strip()]
    if not lines:
        raise AssertionError(f"node driver printed nothing:\n{completed.stderr[-2000:]}")
    return json.loads(lines[-1])
