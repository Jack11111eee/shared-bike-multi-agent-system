#!/usr/bin/env python3
"""F28-2 / §6.2: one real-browser smoke run of the collaboration page.

This is not part of the site's runtime.  It loads the shipped ``collab.html``
over ``file://``, switches a multi-run selector first so a non-default run is
visible, then clicks a real message node, an evidence chip and a real candidate
row *inside that run* and checks the detail names the owning ``run_id`` (ids
restart per run, so a globally-keyed lookup would show another run's record).
It then follows real deep links to the player and the workbench, and repeats
the message click at 400x900 in dark mode.  The result is written to
``outputs/multi_agent_browser_smoke.phase6.json``.

Exit codes: 0 = all interactions verified, 1 = a check failed, 3 = no Chrome.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.cdp import Browser, ToolMissing, find_chrome, tool_versions

SITE = ROOT / "replay"
REPORT = ROOT / "outputs" / "multi_agent_browser_smoke.phase6.json"

async def _smoke() -> dict:
    collab = SITE / "collab.html"
    if not collab.exists():
        raise FileNotFoundError(f"{collab} is missing; run `bike-agent replay --sources xm,sh` first")
    checks: dict[str, object] = {}
    async with Browser(width=1440, height=900) as browser:
        await browser.goto(collab.resolve().as_uri(), settle=1.2)
        checks["collab_loaded"] = bool(await browser.evaluate("!!document.getElementById('collab-data')"))
        checks["file_url"] = (await browser.evaluate("location.protocol")) == "file:"

        # Per-run ids restart at msg-0001, so a lookup map keyed only by the
        # bare id shows another run's record.  Switch a multi-run source to its
        # second run first (making it the visible one), then click a node whose
        # id also appears in the other runs and demand the right provenance.
        switched = await browser.evaluate(
            """(() => {
              for (const select of document.querySelectorAll('.run-picker select')) {
                if (select.options.length < 2) continue;
                const source = select.dataset.source;
                const before = [...document.querySelectorAll('section.run[data-source="' + source + '"]')]
                  .filter(s => !s.hidden).map(s => s.dataset.runId);
                select.value = select.options[1].value;
                select.dispatchEvent(new Event('change', {bubbles: true}));
                const after = [...document.querySelectorAll('section.run[data-source="' + source + '"]')]
                  .filter(s => !s.hidden).map(s => s.dataset.runId);
                return {source, picked: select.value, options: select.options.length, before, after};
              }
              return null;
            })()"""
        )
        if not switched or not switched["after"]:
            raise RuntimeError("no multi-run source to scope the detail panels with")
        pick = await browser.evaluate(
            """(() => {
              const runId = __RUN_ID__;
              const section = document.querySelector('section.run[data-run-id="' + runId + '"]');
              if (!section || section.hidden) return null;
              const item = section.querySelector('[data-message-id]');
              if (!item || !section.querySelector('.chip') || !section.querySelector('.candidate-link')) return null;
              const id = item.dataset.messageId;
              const elsewhere = [...document.querySelectorAll('section.run')]
                .filter(s => s.dataset.runId !== runId)
                .some(s => !!s.querySelector('[data-message-id="' + id + '"]'));
              const first = document.querySelector('section.run');
              return {
                run_id: runId,
                source: section.dataset.source,
                message_id: id,
                id_collides_elsewhere: elsewhere,
                first_run_id: first ? first.dataset.runId : null,
                n_sections: document.querySelectorAll('section.run').length,
              };
            })()""".replace("__RUN_ID__", json.dumps(switched["after"][0]))
        )
        if not pick:
            raise RuntimeError("the switched run has no usable message/chip/candidate")
        section = f'section.run[data-run-id="{pick["run_id"]}"]'
        message_selector = f'{section} [data-message-id="{pick["message_id"]}"]'
        clicked = await browser.click(message_selector + " .node-head")
        message_visible = await browser.evaluate(
            f"!document.querySelector({json.dumps(message_selector + ' .node-body')}).hidden"
        )
        message_text = await browser.evaluate(
            f"document.querySelector({json.dumps(message_selector + ' .node-detail')}).textContent"
        )
        checks["message"] = {
            "id": pick["message_id"],
            "run_id": pick["run_id"],
            "source": pick["source"],
            "n_sections": pick["n_sections"],
            "run_switched_before_click": switched,
            "id_collides_elsewhere": pick["id_collides_elsewhere"],
            "clicked": clicked,
            "detail_visible": message_visible,
            "detail_contains_id": pick["message_id"] in (message_text or ""),
            "provenance_matches": f"run_id: {pick['run_id']}" in (message_text or ""),
        }

        # F28-2: an evidence chip must open the record it points at, from the
        # same run that owns the chip.
        chip_selector = section + " .chip"
        chip_ref = await browser.evaluate(
            f"document.querySelector({json.dumps(chip_selector)}).getAttribute('data-ref')"
        )
        chip_clicked = await browser.click(chip_selector)
        chip_text = await browser.evaluate(
            f"document.querySelector({json.dumps(chip_selector)}).parentNode"
            f".querySelector('.node-body .node-detail').textContent"
        )
        checks["evidence_chip"] = {
            "ref": chip_ref,
            "run_id": pick["run_id"],
            "clicked": chip_clicked,
            "detail_contains_ref": chip_ref in (chip_text or ""),
            "provenance_matches": f"run_id: {pick['run_id']}" in (chip_text or ""),
        }

        candidate_selector = section + " .candidate-link"
        candidate_id = await browser.evaluate(
            f"document.querySelector({json.dumps(candidate_selector)}).getAttribute('data-candidate-id')"
        )
        candidate_clicked = await browser.click(candidate_selector)
        candidate_visible = await browser.evaluate(
            "!document.getElementById('candidate-detail').hidden"
        )
        candidate_text = await browser.evaluate(
            "document.getElementById('candidate-detail-body').textContent"
        )
        checks["candidate"] = {
            "id": candidate_id,
            "run_id": pick["run_id"],
            "clicked": candidate_clicked,
            "detail_visible": candidate_visible,
            "detail_contains_id": candidate_id in (candidate_text or ""),
            "provenance_matches": f"run_id: {pick['run_id']}" in (candidate_text or ""),
        }

        player_href = await browser.evaluate(
            "document.querySelector('a[href*=\"player.html#frame=\"]').getAttribute('href')"
        )
        player_frame = int(re.search(r"#frame=(\d+)", player_href).group(1))
        player_path, _, player_fragment = player_href.partition("#")
        player_target = (collab.parent / player_path).resolve()
        await browser.goto(player_target.as_uri() + "#" + player_fragment, settle=1.0)
        player_label = await browser.evaluate("document.getElementById('frame-label').textContent")
        player_bucket = await browser.evaluate("document.getElementById('bucket-label').textContent")
        checks["player_deep_link"] = {
            "href": player_href,
            "expected_frame": player_frame,
            "label": player_label,
            "bucket": player_bucket,
            "restored": str(player_frame + 1) == str(player_label),
        }

        await browser.goto(collab.resolve().as_uri(), settle=0.8)
        workbench_href = await browser.evaluate(
            "document.querySelector('a[href*=\"workbench.html#frame=\"]').getAttribute('href')"
        )
        workbench_frame = int(re.search(r"#frame=(\d+)", workbench_href).group(1))
        cell_match = re.search(r"[&?]cell=([^&]+)", workbench_href)
        workbench_path, _, workbench_fragment = workbench_href.partition("#")
        workbench_target = (collab.parent / workbench_path).resolve()
        await browser.goto(workbench_target.as_uri() + "#" + workbench_fragment, settle=1.2)
        workbench_label = await browser.evaluate(
            "document.getElementById('frame-label').getAttribute('data-value')"
        )
        workbench_cell = await browser.evaluate(
            "document.getElementById('state-cell').getAttribute('data-value')"
        )
        checks["workbench_deep_link"] = {
            "href": workbench_href,
            "expected_frame": workbench_frame,
            "expected_cell": cell_match.group(1) if cell_match else None,
            "label": workbench_label,
            "cell": workbench_cell,
            "restored": str(workbench_frame + 1) == str(workbench_label)
            and (cell_match is None or cell_match.group(1) == workbench_cell),
        }

        # F28-4: the switch above is the selector test -- it ran on a fresh page
        # load, and it is the same switch whose scoping the detail panels use.
        checks["run_selector"] = {
            "present": True,
            "source": switched["source"],
            "options": switched["options"],
            "before": switched["before"],
            "after": switched["after"],
            "switched": len(switched["before"]) == 1
            and len(switched["after"]) == 1
            and switched["before"] != switched["after"],
        }

        # §6.2: the core interactions must still work at ~400px and in dark
        # mode.  Both are emulated here, then the same message node is clicked
        # again -- a page that only works at 1440px is not usable.
        await browser.call(
            "Emulation.setDeviceMetricsOverride",
            {"width": 400, "height": 900, "deviceScaleFactor": 1, "mobile": False},
        )
        await browser.call(
            "Emulation.setEmulatedMedia",
            {"features": [{"name": "prefers-color-scheme", "value": "dark"}]},
        )
        await browser.goto(collab.resolve().as_uri(), settle=1.0)
        narrow_id = await browser.evaluate(
            "document.querySelector('[data-message-id]').getAttribute('data-message-id')"
        )
        narrow_clicked = await browser.click(f'[data-message-id="{narrow_id}"] .node-head')
        narrow_visible = await browser.evaluate(
            f'!document.querySelector(\'[data-message-id="{narrow_id}"] .node-body\').hidden'
        )
        narrow_overflow = await browser.evaluate(
            "document.documentElement.scrollWidth - window.innerWidth"
        )
        dark_bg = await browser.evaluate(
            "getComputedStyle(document.body).backgroundColor"
        )
        checks["narrow_dark"] = {
            "viewport": "400x900",
            "message_clicked": narrow_clicked,
            "detail_visible": narrow_visible,
            "horizontal_overflow_px": narrow_overflow,
            "body_background": dark_bg,
            "usable": bool(narrow_clicked) and bool(narrow_visible)
            and isinstance(narrow_overflow, (int, float)) and narrow_overflow <= 1,
        }
        await browser.call("Emulation.clearDeviceMetricsOverride")
    return checks

def _all_ok(checks: dict) -> bool:
    return all(
        [
            checks.get("collab_loaded") is True,
            checks.get("file_url") is True,
            checks.get("message", {}).get("detail_visible") is True,
            checks.get("message", {}).get("detail_contains_id") is True,
            checks.get("message", {}).get("provenance_matches") is True,
            checks.get("message", {}).get("id_collides_elsewhere") is True,
            checks.get("evidence_chip", {}).get("detail_contains_ref") is True,
            checks.get("evidence_chip", {}).get("provenance_matches") is True,
            checks.get("candidate", {}).get("detail_visible") is True,
            checks.get("candidate", {}).get("detail_contains_id") is True,
            checks.get("candidate", {}).get("provenance_matches") is True,
            checks.get("player_deep_link", {}).get("restored") is True,
            checks.get("workbench_deep_link", {}).get("restored") is True,
            checks.get("run_selector", {}).get("switched") is True,
            checks.get("narrow_dark", {}).get("usable") is True,
        ]
    )

def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    if find_chrome() is None:
        print("browser smoke: Chrome/Chromium not found; browser checks are NOT verified", file=sys.stderr)
        return 3
    try:
        checks = asyncio.run(_smoke())
    except ToolMissing as exc:
        print(f"browser smoke: {exc}", file=sys.stderr)
        return 3
    except Exception as exc:  # noqa: BLE001 - report, do not hide
        print(f"browser smoke failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    report = {
        "phase": "6",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "browser": tool_versions().get("chrome"),
        "checks": checks,
        "result": "pass" if _all_ok(checks) else "fail",
    }
    REPORT.parent.mkdir(parents=True, exist_ok=True)
    REPORT.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2) if args.json else json.dumps(report, ensure_ascii=False))
    print(f"browser smoke: {report['result']} -> {REPORT.relative_to(ROOT)}")
    return 0 if report["result"] == "pass" else 1

if __name__ == "__main__":
    raise SystemExit(main())
