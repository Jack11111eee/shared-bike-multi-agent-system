"""Read-only projections: never invoke models or silently substitute results."""
from __future__ import annotations

import json
import hashlib
import re
from pathlib import Path

import pandas as pd

from bike_agent.multi_agent.artifacts import load_spatial


def read(path: Path, default=None):
    return json.loads(path.read_text()) if path.is_file() else default


def lines(path: Path):
    return [json.loads(s) for s in path.read_text().splitlines() if s.strip()] if path.is_file() else []


def resolve_run(layout, run_id: str) -> Path:
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,140}", run_id):
        raise ValueError("非法运行 ID")
    root = (layout.outputs / "multi_agent").resolve()
    path = (root / run_id).resolve()
    if path.parent != root or not path.is_dir():
        raise FileNotFoundError("运行不存在")
    return path


def list_runs(layout, *, include_tests=False) -> list[dict]:
    root = layout.outputs / "multi_agent"
    rows = []
    for path in sorted(root.iterdir(), reverse=True) if root.exists() else []:
        if not path.is_dir() or not (path / "decision.json").is_file():
            continue
        d, index = read(path / "decision.json"), read(path / "run.json", {})
        auth = d.get("authenticity_status", "rule_history")
        if auth == "test_fixture" and not include_tests:
            continue
        rows.append({
            "run_id": path.name, "source": d.get("source", index.get("source", "xm")),
            "mode": d.get("mode", "deterministic"),
            "authenticity_status": auth, "terminal_state": d.get("terminal_state"),
            "started_at": index.get("started_at", ""),
            "reason": d.get("reason", ""), "model": index.get("model"),
            "schema_version": d.get("schema_version", "1.0"),
        })
    # Default to a usable demonstration, while retaining unsuccessful attempts.
    return sorted(rows, key=lambda r: (
        r["authenticity_status"] == "real_model_complete",
        {"accepted": 3, "baseline_retained": 2, "no_feasible_candidate": 1}.get(r["terminal_state"], 0),
        r["started_at"]), reverse=True)


def build_run_view(config, layout, run_id: str) -> dict:
    root = resolve_run(layout, run_id)
    d = read(root / "decision.json")
    if d.get("schema_version") != "2.0":
        return {
            "identity": {"run_id": run_id, "mode": "deterministic", "authenticity_status": "rule_history"},
            "decision": d, "capabilities": {"model_evidence": False, "spatial": False},
            "task": read(root / "task.json", {}).get("task", {}),
            "execution": read(root / "run.json", {}),
            "events": [], "candidates": [], "spatial": {}, "geometry": None,
            "messages": [], "tool_calls": [], "model_calls": [],
        }
    task = read(root / "task.json")["task"]
    candidates = read(root / "candidates.json", [])
    spatial = {}
    for c in candidates:
        if c.get("result"):
            spatial[c["candidate_id"]] = load_spatial(root, c["result"])
    input_manifest = read(root / "input_manifest.json", {})
    cell_entry = next((entry for entry in input_manifest.get("files", [])
                       if Path(entry["path"]).name.startswith("cells." + task["source"] + ".")), None)
    cells = []
    if cell_entry:
        cell_path = layout.processed / Path(cell_entry["path"]).name
        if not cell_path.is_file() or hashlib.sha256(cell_path.read_bytes()).hexdigest() != cell_entry["sha256"]:
            raise ValueError("本次冻结的网格几何已变化或缺失，拒绝用其他网格重新发布旧结果")
        cells = pd.read_parquet(cell_path).to_dict("records")
    basemap_path = layout.root / "replay" / task["source"] / "basemap.js"
    geometry = None
    if basemap_path.is_file():
        match = re.search(r'store\["' + task["source"] + r'"\]\s*=\s*(\{.*\});', basemap_path.read_text(), re.S)
        if match:
            geometry = json.loads(match[1])
            cell_map = {row[0]: row for row in geometry["cells"]}
            projection = geometry["projection"]
            bbox = projection["bbox"]
            valid = bool(cells) and set(cell_map) == {c["cell_id"] for c in cells}
            for cell in cells:
                row = cell_map.get(cell["cell_id"])
                if row is None:
                    valid = False
                    break
                x = (cell["lon"] - bbox[0]) * projection["px_per_degree_lon"]
                y = (bbox[3] - cell["lat"]) * projection["px_per_degree_lat"]
                if abs(x - row[1] - row[3]/2) > 1 or abs(y - row[2] - row[4]/2) > 1:
                    valid = False
                    break
            if not valid:
                geometry = None
    return {
        "schema_version": "2.0",
        "identity": {"run_id": run_id, "task_id": task["task_id"], "source": task["source"],
                     "mode": "llm", "authenticity_status": d["authenticity_status"],
                     "model_roles": d["model_roles"]},
        "task": task, "execution": read(root / "run.json"), "decision": d,
        "events": lines(root / "events.v2.jsonl"), "messages": lines(root / "messages.jsonl"),
        "model_calls": lines(root / "model_calls.jsonl"), "tool_calls": lines(root / "tool_calls.jsonl"),
        "candidates": candidates, "spatial": spatial, "geometry": geometry, "cells": cells,
        "capabilities": {"model_evidence": d["llm_connected"], "spatial": bool(spatial),
                         "live_execution": False},
    }
