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
import re
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
    var bucketBeforeJump = q('#bucket-label') ? q('#bucket-label').getAttribute('data-value') : null;
    var eventButton = q('[data-event]:not([disabled])');
    if (eventButton) eventButton.click();
    out.steps.push(snap('event-jump'));
    out.eventId = eventButton ? eventButton.getAttribute('data-event') : null;
    // The forecast cursor is a second view of the same frame; if it did not
    // follow the jump, the page would show one bucket's prediction next to
    // another bucket's map.
    out.eventJump = {
      frameBefore: bucketBeforeJump,
      frameAfter: q('#bucket-label') ? q('#bucket-label').getAttribute('data-value') : null,
      forecastBucket: q('#forecast-bucket') ? q('#forecast-bucket').getAttribute('data-value') : null,
      playingAfter: q('#play-state') ? q('#play-state').getAttribute('data-playing') : null
    };
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

    // -- Phase 5: projection switch keeps the same cell and frame ------------
    var cellBefore = q('#state-cell') ? q('#state-cell').getAttribute('data-value') : null;
    var frameBefore = q('#frame-label') ? q('#frame-label').getAttribute('data-value') : null;
    click(q('[data-projection="geo"]'), 'projection geo');
    out.steps.push(snap('geo'));
    // Measure the side view: it is the one that also carries the move lines, so
    // the same pass covers "the basemap is drawn" and "the arrows sit on it".
    click(q('[data-mode="side"]'), 'side mode for geo');
    // Only the geo pane matching the current mode is shown, and a hidden
    // subtree has no boxes -- so measure the visible one, not all three.
    var geoMaps = qa('svg.geo-map').filter(function (svg) {
      var host = svg.parentNode;
      return host && !host.hidden && host.getBoundingClientRect().width > 0;
    });
    var geoMap = geoMaps[0] || null;
    var geoBox = geoMap ? geoMap.getBoundingClientRect() : null;
    var geoRects = geoMap ? Array.prototype.slice.call(geoMap.querySelectorAll('rect.cell')) : [];
    var geoBad = 0;
    geoRects.forEach(function (r) {
      var b = r.getBoundingClientRect();
      var w = parseFloat(r.getAttribute('width'));
      if (!isFinite(w) || w <= 0 || !(b.width > 0 && b.height > 0)) geoBad++;
    });
    // Every move line must start and end on a cell centre: recompute both ends
    // from the published rects and compare with what the page drew.
    var moveLines = geoMap ? Array.prototype.slice.call(geoMap.querySelectorAll('line.geo-move')) : [];
    var moveBad = 0;
    moveLines.forEach(function (line) {
      var from = geoMap.querySelector('rect.cell[data-cell="' + line.getAttribute('data-from') + '"]');
      var to = geoMap.querySelector('rect.cell[data-cell="' + line.getAttribute('data-to') + '"]');
      if (!from || !to) { moveBad++; return; }
      var fx = parseFloat(from.getAttribute('x')) + parseFloat(from.getAttribute('width')) / 2;
      var fy = parseFloat(from.getAttribute('y')) + parseFloat(from.getAttribute('height')) / 2;
      var tx = parseFloat(to.getAttribute('x')) + parseFloat(to.getAttribute('width')) / 2;
      var ty = parseFloat(to.getAttribute('y')) + parseFloat(to.getAttribute('height')) / 2;
      if (Math.abs(fx - parseFloat(line.getAttribute('x1'))) > 0.5 ||
          Math.abs(fy - parseFloat(line.getAttribute('y1'))) > 0.5 ||
          Math.abs(tx - parseFloat(line.getAttribute('x2'))) > 0.5 ||
          Math.abs(ty - parseFloat(line.getAttribute('y2'))) > 0.5) moveBad++;
    });
    out.geo = {
      visible: !!geoBox && geoBox.width > 0 && geoBox.height > 0,
      cells: geoRects.length,
      zeroSize: geoBad,
      moves: moveLines.length,
      movesOffCentre: moveBad,
      scaleBar: !!geoMap && !!geoMap.querySelector('.geo-scale'),
      selected: geoMap ? geoMap.querySelectorAll('rect.cell.selected').length : 0,
      cellBefore: cellBefore,
      cellAfter: q('#state-cell') ? q('#state-cell').getAttribute('data-value') : null,
      frameBefore: frameBefore,
      frameAfter: q('#frame-label') ? q('#frame-label').getAttribute('data-value') : null
    };
    click(q('[data-mode="delta"]'), 'back to delta for the forecast step');
    click(q('[data-projection="grid"]'), 'projection grid');
    out.steps.push(snap('grid-again'));

    // -- Phase 5: clicking a point on the prediction curve jumps and pauses ---
    var chartPoint = q('#forecast-chart circle[data-forecast-bucket]');
    var beforePoint = {
      frame: q('#frame-label') ? q('#frame-label').getAttribute('data-value') : null,
      cell: q('#state-cell') ? q('#state-cell').getAttribute('data-value') : null,
      playing: q('#play-state') ? q('#play-state').getAttribute('data-playing') : null
    };
    click(q('[data-control="play"]'), 'play before forecast click');
    var playingBeforeClick = q('#play-state').getAttribute('data-playing');
    var targetPoint = null;
    var points = qa('#forecast-chart circle[data-forecast-bucket]');
    for (var p = 0; p < points.length; p++) {
      var idx = parseInt(points[p].getAttribute('data-forecast-bucket'), 10);
      var currentFrame = parseInt((q('#frame-label') || {}).getAttribute ? q('#frame-label').getAttribute('data-value') : '1', 10) - 1;
      if (idx !== currentFrame) { targetPoint = points[p]; break; }
    }
    if (targetPoint) {
      targetPoint.dispatchEvent(new MouseEvent('click', { bubbles: true, cancelable: true }));
    }
    out.forecast = {
      points: points.length,
      clicked: targetPoint ? targetPoint.getAttribute('data-forecast-bucket') : null,
      playingBeforeClick: playingBeforeClick,
      frameBefore: beforePoint.frame,
      frameAfter: q('#frame-label') ? q('#frame-label').getAttribute('data-value') : null,
      playingAfterClick: q('#play-state') ? q('#play-state').getAttribute('data-playing') : null,
      cellBefore: beforePoint.cell,
      cellAfter: q('#state-cell') ? q('#state-cell').getAttribute('data-value') : null,
      mae: q('#forecast-mae') ? q('#forecast-mae').getAttribute('data-value') : null,
      maeN: q('#forecast-n') ? q('#forecast-n').getAttribute('data-value') : null,
      range: q('#forecast-range-label') ? q('#forecast-range-label').getAttribute('data-value') : null,
      forecastBucket: q('#forecast-bucket') ? q('#forecast-bucket').getAttribute('data-value') : null,
      frameBucket: q('#bucket-label') ? q('#bucket-label').getAttribute('data-value') : null,
      error: q('#forecast-error') ? q('#forecast-error').getAttribute('data-value') : null,
      absError: q('#forecast-abs-error') ? q('#forecast-abs-error').getAttribute('data-value') : null,
      actual: q('#forecast-actual') ? q('#forecast-actual').getAttribute('data-value') : null,
      pred: q('#forecast-pred') ? q('#forecast-pred').getAttribute('data-value') : null,
      evalText: q('#forecast-eval') ? q('#forecast-eval').textContent : null
    };

    // -- Phase 5: metric and range switches keep the same cell and frame ------
    function activeAttr(selector, attribute) {
      var node = document.querySelector(selector + '.active');
      return node ? node.getAttribute(attribute) : null;
    }
    var cellBeforeSwitch = q('#state-cell') ? q('#state-cell').getAttribute('data-value') : null;
    var frameBeforeSwitch = q('#frame-label') ? q('#frame-label').getAttribute('data-value') : null;
    var metricBefore = activeAttr('[data-forecast-metric]', 'data-forecast-metric');
    var metricSeriesBefore = qa('#forecast-chart .series').length;
    click(q('[data-forecast-metric="in"]'), 'forecast metric in');
    out.steps.push(snap('forecast-metric-in'));
    var metricAfterIn = activeAttr('[data-forecast-metric]', 'data-forecast-metric');
    var metricSeriesAfter = qa('#forecast-chart .series').length;
    click(q('[data-forecast-metric="net"]'), 'forecast metric net');
    out.steps.push(snap('forecast-metric-net'));
    var metricAfterNet = activeAttr('[data-forecast-metric]', 'data-forecast-metric');
    var netSeries = qa('#forecast-chart .series').length;
    // Range: cell -> whole source.  The sample count must grow and the selected
    // cell must not move; the MAE is then a different quantity, with its own label.
    var maeCell = q('#forecast-mae') ? q('#forecast-mae').getAttribute('data-value') : null;
    var nCell = q('#forecast-n') ? q('#forecast-n').getAttribute('data-value') : null;
    var maeLabelCell = q('#forecast-mae-label') ? q('#forecast-mae-label').textContent.trim() : null;
    click(q('[data-forecast-range="source"]'), 'forecast range source');
    out.steps.push(snap('forecast-range-source'));
    out.forecastSwitch = {
      metricBefore: metricBefore,
      metricAfterIn: metricAfterIn,
      metricAfterNet: metricAfterNet,
      metricSeriesBefore: metricSeriesBefore,
      metricSeriesAfter: metricSeriesAfter,
      netSeries: netSeries,
      range: q('#forecast-range-label') ? q('#forecast-range-label').getAttribute('data-value') : null,
      maeCell: maeCell,
      nCell: nCell,
      maeLabelCell: maeLabelCell,
      maeLabelSource: q('#forecast-mae-label') ? q('#forecast-mae-label').textContent.trim() : null,
      maeSource: q('#forecast-mae') ? q('#forecast-mae').getAttribute('data-value') : null,
      nSource: q('#forecast-n') ? q('#forecast-n').getAttribute('data-value') : null,
      cellBefore: cellBeforeSwitch,
      cellAfter: q('#state-cell') ? q('#state-cell').getAttribute('data-value') : null,
      frameBefore: frameBeforeSwitch,
      frameAfter: q('#frame-label') ? q('#frame-label').getAttribute('data-value') : null
    };
    // Put the page back where the shot recipes expect it.
    click(q('[data-forecast-range="cell"]'), 'forecast range cell');
    click(q('[data-forecast-metric="out"]'), 'forecast metric out');
    out.steps.push(snap('forecast-click'));
  } catch (error) {
    out.errors.push('exception: ' + (error && error.message ? error.message : String(error)));
    out.stack = error && error.stack ? String(error.stack).slice(0, 800) : null;
  }
  finish();
})();
"""


def _as_int(value: object) -> int | None:
    try:
        return int(str(value))
    except (TypeError, ValueError):
        return None


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


_RELATIVE_REF = re.compile(r'((?:src|href)\s*=\s*")([^"#]+)(")', re.IGNORECASE)

def _absolute_refs(html: str, base_dir: Path) -> str:
    """Point every relative asset at its real file on disk.

    The smoke page used to be written inside ``replay/<source>/`` so relative
    references would resolve.  That made a test artifact part of the shipped
    site: an interrupted run left a stray page behind, and two validators
    running at once saw each other's page as a ``stray_files`` failure.  The
    page now lives in a temporary directory, so its references are rewritten to
    absolute ``file://`` URIs -- still offline, still the same bytes, but the
    shipped tree is never written to.
    """

    def replace(match: re.Match[str]) -> str:
        target = match.group(2)
        if target.startswith(("http:", "https:", "//", "data:", "file:", "/")):
            return match.group(0)
        resolved = (base_dir / target).resolve()
        return f"{match.group(1)}{resolved.as_uri()}{match.group(3)}"

    return _RELATIVE_REF.sub(replace, html)

def _run_once(source: str, page: Path, chrome: str, timeout: int) -> dict:

    # Clean up pages a previous version of this script left inside the shipped
    # tree; new runs never write there.
    for stale in (SITE / source / "workbench.smoke.html", SITE / "smoke-driver.js"):
        stale.unlink(missing_ok=True)

    html = page.read_text(encoding="utf-8")
    # The driver is inlined: under file:// a separate script would need its own
    # file on disk, which is one more thing a crash could leave behind.
    injected = html.replace("</body>", f"<script>\n{DRIVER}\n</script>\n</body>")
    if injected == html:
        return {"ok": False, "reason": "could not inject the smoke driver", "steps": []}
    injected = _absolute_refs(injected, page.parent)

    with tempfile.TemporaryDirectory() as scratch:
        smoke_path = Path(scratch) / "workbench.smoke.html"
        smoke_path.write_text(injected, encoding="utf-8")
        profile = Path(scratch) / "profile"
        try:
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
            # A browser that never answered produced no result, which is exactly
            # the case ``run()`` is allowed to retry once.  Letting the exception
            # escape would skip the retry and turn a loaded machine into a page
            # defect.
            return {
                "ok": False,
                "ran": False,
                "reason": f"the browser did not finish within {timeout}s",
                "steps": [],
            }

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

    # Phase 5: a projection switch must not disturb the analysis position, and a
    # click on the prediction curve must pause before it jumps.
    geo = result.get("geo") or {}
    forecast = result.get("forecast") or {}
    geo_ok = (
        geo.get("visible") is True
        and geo.get("cells", 0) > 0
        and geo.get("zeroSize", 1) == 0
        and geo.get("movesOffCentre", 1) == 0
        and geo.get("scaleBar") is True
        and geo.get("selected", 0) == 1
        and geo.get("cellBefore") == geo.get("cellAfter")
        and geo.get("frameBefore") == geo.get("frameAfter")
    )
    def _num(value):
        try:
            return float(value)
        except (TypeError, ValueError):
            return None

    error, abs_error = _num(forecast.get("error")), _num(forecast.get("absError"))
    actual, pred = _num(forecast.get("actual")), _num(forecast.get("pred"))
    forecast_ok = (
        forecast.get("points", 0) > 0
        and forecast.get("clicked") is not None
        and forecast.get("frameAfter") != forecast.get("frameBefore")
        and forecast.get("playingAfterClick") == "false"
        # "Pauses before jumping" only means something if it was playing.
        and forecast.get("playingBeforeClick") == "true"
        and forecast.get("cellBefore") == forecast.get("cellAfter")
        and forecast.get("mae") not in (None, "")
        and forecast.get("maeN") not in (None, "")
        # The cursor and the map must show the same bucket.
        and forecast.get("forecastBucket") == forecast.get("frameBucket")
        # The current-point error is recomputed from the two displayed values.
        and error is not None and abs_error is not None
        and actual is not None and pred is not None
        and abs(error - (pred - actual)) <= 1e-6
        and abs(abs_error - abs(pred - actual)) <= 1e-6
        # The cited model-vs-baseline comparison is actually on the page, and it
        # tells the truth when the model loses (XM's borrowed-bike forecast does).
        and forecast.get("evalText")
        and "基线" in forecast["evalText"]
        and "模型" in forecast["evalText"]
        and ("模型未优于基线" in forecast["evalText"] or "模型更好" in forecast["evalText"])
        and (source != "xm" or "模型未优于基线" in forecast["evalText"])
    )
    # Metric and range switches must change what is shown and nothing else:
    # same cell, same frame, three series each time, and the whole-source sample
    # count must exceed the single cell's (a per-cell MAE relabelled as source-wide
    # would be the easiest way to fake this).  The MAE *label* has to change too:
    # a source-wide number wearing "局部" is the wrong quantity.
    switch = result.get("forecastSwitch") or {}
    switch_ok = (
        switch.get("metricBefore") == "out"
        and switch.get("metricAfterIn") == "in"
        and switch.get("metricAfterNet") == "net"
        and switch.get("metricSeriesBefore") == 3
        and switch.get("metricSeriesAfter") == 3
        and switch.get("netSeries") == 3
        and switch.get("range") == "source"
        and switch.get("cellBefore") == switch.get("cellAfter")
        and switch.get("frameBefore") == switch.get("frameAfter")
        and _as_int(switch.get("nSource")) is not None
        and _as_int(switch.get("nCell")) is not None
        and _as_int(switch.get("nSource")) > _as_int(switch.get("nCell"))
        and switch.get("maeSource") not in (None, "")
        and switch.get("maeLabelCell") == "局部 MAE"
        and switch.get("maeLabelSource") == "全源 MAE"
    )
    # The event jump moves the forecast cursor with the frame.
    jump = result.get("eventJump") or {}
    jump_ok = bool(jump) and jump.get("forecastBucket") == jump.get("frameAfter")
    result["geoOk"] = geo_ok
    result["forecastOk"] = forecast_ok
    result["forecastSwitchOk"] = switch_ok
    result["eventJumpOk"] = jump_ok
    result["ok"] = (
        not result["errors"]
        and result["ready"] is True
        and drawn
        and traced
        and geo_ok
        and forecast_ok
        and switch_ok
        and jump_ok
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
