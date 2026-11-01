#!/usr/bin/env python3
"""NF-27: one real-browser smoke run of the workbench, for development only.

The site itself never depends on a browser.  This script exists so that "the
controls are wired up" is a measured fact rather than a claim read off the
markup: it loads the shipped page in headless Chrome, clicks the real buttons,
and records what the DOM said after each click.

Nothing is installed and nothing is downloaded.  If no Chrome/Chromium binary is
found the script says so and exits 3, which the caller reports as "not verified"
rather than "passed".

Usage::

    python3 scripts/browser_smoke.py --source xm [--json]
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SITE = ROOT / "replay"

CANDIDATES = (
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    "/Applications/Chromium.app/Contents/MacOS/Chromium",
    "/usr/bin/google-chrome",
    "/usr/bin/chromium",
    "/usr/bin/chromium-browser",
    "google-chrome",
    "chromium",
)

DRIVER = r"""
(function () {
  var out = { source: document.body.getAttribute('data-source'), steps: [], errors: [] };
  function finish() {
    var pre = document.createElement('pre');
    pre.id = 'smoke-result';
    pre.textContent = JSON.stringify(out);
    document.body.appendChild(pre);
    document.title = 'SMOKE-DONE';
  }
  function q(s) { return document.querySelector(s); }
  function qa(s) { return Array.prototype.slice.call(document.querySelectorAll(s)); }
  function click(el, what) {
    if (!el) { out.errors.push('missing element: ' + what); return false; }
    el.click();
    return true;
  }
  function snap(label) {
    var net = q('[data-metric="delta_net"]');
    return {
      label: label,
      chapter: q('#guide-chapter') ? q('#guide-chapter').getAttribute('data-value') : null,
      title: q('#guide-title') ? q('#guide-title').textContent : null,
      frame: q('#frame-label') ? q('#frame-label').getAttribute('data-value') : null,
      bucket: q('#bucket-label') ? q('#bucket-label').getAttribute('data-value') : null,
      mode: q('#state-mode') ? q('#state-mode').getAttribute('data-value') : null,
      cell: q('#state-cell') ? q('#state-cell').getAttribute('data-value') : null,
      scenario: q('#state-scenario') ? q('#state-scenario').getAttribute('data-value') : null,
      playing: q('#play-state') ? q('#play-state').getAttribute('data-playing') : null,
      deltaNet: net ? net.getAttribute('data-value') : null,
      deltaCells: qa('svg.delta-map rect.cell').length,
      historyRows: qa('.history-table tbody tr').length,
      ledgerCell: q('#ledger-cell') ? q('#ledger-cell').getAttribute('data-value') : null,
      activeChapters: qa('.chapter[data-active="true"]').length
    };
  }

  function ledgerValues() {
    // What the DOM actually says, per strategy -- not what the bundle holds.
    var out = {};
    ['rule', 'optimized'].forEach(function (strategy) {
      var pane = q('[data-ledger-strategy="' + strategy + '"]');
      if (!pane) { out[strategy] = null; return; }
      var values = {};
      var nodes = pane.querySelectorAll('[data-metric]');
      for (var i = 0; i < nodes.length; i++) {
        values[nodes[i].getAttribute('data-metric')] = nodes[i].getAttribute('data-value');
      }
      out[strategy] = values;
    });
    return out;
  }

  // A rect that exists but has no geometry is invisible: counting cells is not
  // evidence that anything was drawn, so measure the rendered boxes too.
  function deltaGeometry() {
    // The delta view is hidden until its mode is selected, and a hidden subtree
    // has no boxes -- so the measurement is only meaningful while it is shown.
    var view = q('[data-view="delta"]');
    var visible = !!view && !view.hidden;
    var rects = qa('svg.delta-map rect.cell');
    var bad = 0;
    rects.forEach(function (r) {
      var box = r.getBoundingClientRect();
      var w = parseFloat(r.getAttribute('width'));
      var x = parseFloat(r.getAttribute('x'));
      if (!isFinite(w) || w <= 0 || !isFinite(x)) bad++;
      else if (visible && !(box.width > 0 && box.height > 0)) bad++;
    });
    return { cells: rects.length, zeroSize: bad, visible: visible };
  }

  // Recompute a displayed derived value from the operands the page itself
  // publishes, and check its join key names the frame on screen.
  function provenanceCheck() {
    var pane = q('[data-ledger-strategy="rule"]');
    if (!pane) return { ok: false, reason: 'no ledger pane' };
    var end = pane.querySelector('[data-metric="inventory_end"]');
    if (!end) return { ok: false, reason: 'no inventory_end' };
    var ops = JSON.parse(end.getAttribute('data-operands') || 'null');
    var frame = q('#bucket-label') ? q('#bucket-label').getAttribute('data-value') : null;
    var join = end.getAttribute('data-join-key') || '';
    var recomputed = ops
      ? ops.inventory_start + ops.moved_in - ops.moved_out + ops.returns_applied - ops.borrow_met
      : null;
    var displayed = parseInt(end.getAttribute('data-value'), 10);
    return {
      displayed: displayed,
      recomputed: recomputed,
      joinKey: join,
      frame: frame,
      joinMatchesFrame: !!frame && join.indexOf('time_bucket=' + frame) >= 0,
      ok: recomputed === displayed && !!frame && join.indexOf('time_bucket=' + frame) >= 0
    };
  }

  try {
    out.ready = window.__WORKBENCH_READY__ === true;
    out.steps.push(snap('initial'));
    out.provenanceInitial = provenanceCheck();

    click(q('[data-guide-action="start"]'), 'guide start');
    out.steps.push(snap('start'));

    for (var i = 0; i < 4; i++) {
      click(q('[data-guide-action="next"]'), 'guide next ' + i);
      out.steps.push(snap('next-' + i));
    }
    click(q('[data-guide-action="prev"]'), 'guide prev');
    out.steps.push(snap('prev'));
    click(q('[data-guide-action="exit"]'), 'guide exit');
    out.steps.push(snap('exit'));
    click(q('[data-guide-action="restart"]'), 'guide restart');
    out.steps.push(snap('restart'));
    click(q('[data-guide-action="exit"]'), 'guide exit 2');

    click(q('[data-mode="delta"]'), 'delta mode');
    out.steps.push(snap('delta-mode'));
    out.deltaGeometryDelta = deltaGeometry();
    out.provenanceDelta = provenanceCheck();
    var firstDeltaCell = q('svg.delta-map rect.cell');
    if (firstDeltaCell) {
      // SVG elements have no .click(); dispatch the same bubbling event a
      // pointer would produce so the page's delegated handler sees it.
      firstDeltaCell.dispatchEvent(new MouseEvent('click', { bubbles: true, cancelable: true }));
    }
    out.steps.push(snap('delta-cell-click'));

    click(q('[data-control="play"]'), 'play');
    out.playingBeforeJump = q('#play-state').getAttribute('data-playing');
    var eventButton = q('[data-event]:not([disabled])');
    if (eventButton) eventButton.click();
    out.steps.push(snap('event-jump'));
    out.eventId = eventButton ? eventButton.getAttribute('data-event') : null;
    out.deltaGeometryAfterJump = deltaGeometry();
    out.provenanceAfterJump = provenanceCheck();

    var scenarios = qa('button[data-scenario]').filter(function (b) {
      return !b.classList.contains('baseline-return');
    });
    out.scenarioIds = scenarios.map(function (b) { return b.getAttribute('data-scenario'); });
    var scenarioSnaps = [];
    scenarios.forEach(function (b) {
      b.click();
      scenarioSnaps.push(snap('scenario:' + b.getAttribute('data-scenario')));
    });
    out.scenarios = scenarioSnaps;
    click(q('.baseline-return'), 'back to baseline');
    out.steps.push(snap('baseline-return'));

    var select = q('[data-cell-select]');
    if (select && select.options.length > 2) {
      select.value = select.options[2].value;
      select.dispatchEvent(new Event('change', { bubbles: true }));
    }
    out.steps.push(snap('cell-select'));
    // The numbers the ledger shows, so the caller can check them against the
    // parquet instead of trusting that the wiring ran.
    out.ledger = ledgerValues();
    out.ledgerContext = {
      source: document.body.getAttribute('data-source'),
      frame: q('#frame-label') ? parseInt(q('#frame-label').getAttribute('data-value'), 10) - 1 : null,
      time_bucket: q('#bucket-label') ? q('#bucket-label').getAttribute('data-value') : null,
      cell: q('#ledger-cell') ? q('#ledger-cell').getAttribute('data-value') : null,
      historyRows: qa('.history-table tbody tr').length,
      historyCurrent: qa('.history-table tr[data-current="true"]').length
    };
  } catch (error) {
    out.errors.push('exception: ' + (error && error.message ? error.message : String(error)));
    out.stack = error && error.stack ? String(error.stack).slice(0, 800) : null;
  }
  finish();
})();
"""


def find_chrome() -> str | None:
    for candidate in CANDIDATES:
        found = shutil.which(candidate)
        if found:
            return found
    return None


def run(source: str, timeout: int = 300, attempts: int = 2) -> dict:
    """Drive the shipped page once; retry only when the browser never ran.

    A retry is allowed *only* when the page produced no result at all -- that is
    a failed browser launch or a starved machine, not a page defect.  If the
    driver ran and reported errors, or reported a broken state, the failure is
    returned as-is with no retry, and ``attempts`` records how many launches
    were needed so a flaky environment is visible rather than hidden.
    """
    chrome = find_chrome()
    if chrome is None:
        return {"ok": False, "reason": "no Chrome/Chromium binary found; browser smoke not run", "steps": []}
    page = SITE / source / "workbench.html"
    if not page.exists():
        return {"ok": False, "reason": f"missing {page}", "steps": []}

    last: dict = {"ok": False, "reason": "not attempted", "steps": []}
    for attempt in range(1, max(attempts, 1) + 1):
        last = _run_once(source, page, chrome, timeout)
        last["attempts"] = attempt
        if last.get("ran"):
            break
    last.pop("ran", None)
    return last


def _run_once(source: str, page: Path, chrome: str, timeout: int) -> dict:

    smoke_name = "workbench.smoke.html"
    smoke_path = SITE / source / smoke_name
    # A previous interrupted run must not leave a page inside the shipped site.
    for stale in (smoke_path, SITE / "smoke-driver.js"):
        stale.unlink(missing_ok=True)

    html = page.read_text(encoding="utf-8")
    # The driver is inlined: under file:// a separate script would need its own
    # file inside the site, which is one more thing a crash could leave behind.
    injected = html.replace("</body>", f"<script>\n{DRIVER}\n</script>\n</body>")
    if injected == html:
        return {"ok": False, "reason": "could not inject the smoke driver", "steps": []}

    smoke_path.write_text(injected, encoding="utf-8")
    try:
        with tempfile.TemporaryDirectory() as profile:
            completed = subprocess.run(
                [
                    chrome,
                    "--headless=new",
                    "--disable-gpu",
                    "--no-sandbox",
                    "--no-first-run",
                    f"--user-data-dir={profile}",
                    "--virtual-time-budget=10000",
                    "--dump-dom",
                    smoke_path.as_uri(),
                ],
                capture_output=True,
                text=True,
                timeout=timeout,
                check=False,
            )
    except subprocess.TimeoutExpired:
        # A browser that never answered produced no result, which is exactly the
        # case ``run()`` is allowed to retry once.  Letting the exception escape
        # would skip the retry and turn a loaded machine into a page defect.
        return {
            "ok": False,
            "ran": False,
            "reason": f"the browser did not finish within {timeout}s",
            "steps": [],
        }
    finally:
        smoke_path.unlink(missing_ok=True)

    dom = completed.stdout
    marker = 'id="smoke-result">'
    if marker not in dom:
        return {
            "ok": False,
            "ran": False,
            "reason": "the page did not produce a smoke result (browser launch failed or timed out)",
            "stderr": completed.stderr[-800:],
            "steps": [],
        }
    payload = dom.split(marker, 1)[1].split("</pre>", 1)[0]
    payload = (
        payload.replace("&quot;", '"').replace("&amp;", "&").replace("&lt;", "<").replace("&gt;", ">")
    )
    result = json.loads(payload)
    # "The cells exist" is not the same as "the cells are drawn", and a number is
    # only traceable if its own operands recompute to it.  Both are gates.
    geometry = [
        result.get("deltaGeometryDelta"),
        result.get("deltaGeometryAfterJump"),
    ]
    drawn = all(
        g and g["visible"] and g["cells"] > 0 and g["zeroSize"] == 0 for g in geometry
    )
    provenance = [
        result.get("provenanceInitial"),
        result.get("provenanceDelta"),
        result.get("provenanceAfterJump"),
    ]
    traced = all(p and p.get("ok") for p in provenance)
    result["geometryOk"] = drawn
    result["provenanceOk"] = traced
    result["ok"] = (
        not result["errors"] and result["ready"] is True and drawn and traced
    )
    result["chrome"] = chrome
    result["ran"] = True
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", default="xm", choices=["xm", "sh"])
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--timeout", type=int, default=300, help="seconds to wait for Chrome")
    args = parser.parse_args(argv)
    result = run(args.source, timeout=args.timeout)
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        if result["ok"]:
            print(f"[smoke] {args.source}: {len(result['steps'])} steps, no errors")
        else:
            print(f"[smoke] {args.source}: FAILED - {result.get('reason', 'errors reported')}")
    if result.get("reason") and "no Chrome" in result["reason"]:
        return 3
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
