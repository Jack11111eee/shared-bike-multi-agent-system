#!/usr/bin/env python3
"""Verify a published real-model run without calling models or simulations."""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from bike_agent.config import Layout, load_config
from bike_agent.presentation.run_view import build_run_view, list_runs, resolve_run
from bike_agent.presentation.console import ASSETS, UI_FILES


def check(config, layout, run_id):
    view = build_run_view(config, layout, run_id)
    path = resolve_run(layout, run_id)
    results = []
    def add(name, ok, detail=""):
        results.append({"name": name, "ok": bool(ok), "detail": detail})
    decision, calls = view["decision"], view["model_calls"]
    add("real_four_role_run", view["identity"]["authenticity_status"] == "real_model_complete")
    add("successful_business_terminal", decision["terminal_state"] in
        {"accepted", "baseline_retained", "no_feasible_candidate"})
    by_id = {c["model_call_id"]: c for c in calls if c["status"] == "ok" and c["real_provider"]}
    for role in ("coordinator", "diagnosis", "dispatch", "evaluation"):
        ids = decision["model_roles"].get(role, [])
        add("provider_evidence_" + role, ids and all(i in by_id and by_id[i]["role"] == role for i in ids))
    sequences = [e["sequence"] for e in view["events"]]
    add("monotonic_events", sequences == list(range(1,len(sequences)+1)))
    for candidate in view["candidates"]:
        result = candidate.get("result")
        if result:
            spatial = view["spatial"].get(candidate["candidate_id"], {})
            add("spatial_identity_" + candidate["candidate_id"], spatial.get("result_id") == result["result_id"])
            actual = sum(f["unmet_requests"] for f in spatial["frames"])
            add("spatial_total_" + candidate["candidate_id"], actual == result["metrics"]["unmet_requests"])
    selected = decision.get("selected")
    if selected:
        add("selected_feasible", selected["result"]["feasible"] and not any(selected["result"]["violations"].values()))
        csv_path = path / "presentation" / "dispatch-moves.csv"
        rows = list(csv.DictReader(csv_path.open(encoding="utf-8-sig"))) if csv_path.exists() else []
        add("export_identity", csv_path.exists() and all(r["run_id"] == run_id and
             r["result_id"] == selected["result"]["result_id"] for r in rows))
        add("export_total", sum(int(r["bikes"]) for r in rows) == selected["result"]["summary"]["bikes_moved"])
    manifest = json.loads((path/"presentation/manifest.json").read_text())
    add("published_hashes", all(hashlib.sha256((path/"presentation"/name).read_bytes()).hexdigest() == digest
                               for name,digest in manifest["files"].items()))
    add("current_ui_assets", all(hashlib.sha256((ASSETS/name).read_bytes()).hexdigest() ==
                                manifest["files"].get(name) for name in UI_FILES))
    add("offline_entry", (path/"presentation/index.html").is_file() and (path/"presentation/replay-data.js").is_file())
    result = {"run_id": run_id, "ok": all(r["ok"] for r in results), "checks": results}
    return result


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument("--run-id")
    args=parser.parse_args()
    layout=Layout(ROOT)
    rows=[r for r in list_runs(layout) if r["authenticity_status"] == "real_model_complete"]
    if not args.run_id and not rows:
        print("No completed real model run available",file=sys.stderr)
        return 1
    run_id=args.run_id or rows[0]["run_id"]
    result=check(load_config(),layout,run_id)
    (layout.outputs/"phase7_validation.json").write_text(json.dumps(result,ensure_ascii=False,indent=2)+"\n")
    print(json.dumps(result,ensure_ascii=False,indent=2))
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
