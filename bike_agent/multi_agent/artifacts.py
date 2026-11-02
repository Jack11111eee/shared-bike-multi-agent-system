"""Complete, immutable simulation results for individual model candidates."""
from __future__ import annotations

import hashlib
import math
from pathlib import Path

import pandas as pd

from bike_agent.multi_agent.journal import encoded, write_json
from bike_agent.multi_agent.task import verify_unchanged
from bike_agent.sim.pipeline import simulation_bundle

VIOLATIONS = ("conservation", "capacity", "budget", "negative_inventory", "clamped_moves")
METRICS = ("unmet_requests", "distance_bike_km", "composite_cost", "service_rate")
STATE_FIELDS = ("inventory_start", "borrow_requests", "borrow_met", "unmet_requests",
                "returns", "returns_applied", "returns_overflow", "bikes_moved", "inventory_end")


def engine_digest() -> str:
    root = Path(__file__).resolve().parents[1]
    paths = sorted((root / "sim").glob("*.py")) + [
        root / "prep" / "grid.py", root / "utils.py",
        Path(__file__),
    ]
    digest = hashlib.sha256()
    for path in paths:
        digest.update(path.name.encode())
        digest.update(path.read_bytes())
    return digest.hexdigest()


def validate_bundle(result, budget: int) -> None:
    summary, steps, states, moves = result.summary, result.steps, result.cell_states, result.moves
    if set(result.violations) != set(VIOLATIONS):
        raise ValueError("仿真约束计数字段不完整")
    if any(not math.isfinite(float(summary[key])) for key in METRICS):
        raise ValueError("仿真指标含非有限值")
    if steps.empty or states.empty or states.duplicated(["time_bucket", "cell_id"]).any():
        raise ValueError("仿真空间状态缺失或重复")
    totals = states.groupby("time_bucket")[list(STATE_FIELDS)].sum().sort_index()
    expected = steps.set_index("time_bucket")[list(STATE_FIELDS)].sort_index()
    pd.testing.assert_frame_equal(totals, expected, check_dtype=False, check_freq=False)
    for key in ("borrow_requests", "borrow_met", "unmet_requests", "bikes_moved"):
        if int(steps[key].sum()) != int(summary[key]):
            raise ValueError(f"汇总与逐时结果不一致：{key}")
    if (steps["bikes_moved"] > budget).any():
        raise ValueError("实际模拟搬运量超出预算")
    if not moves.empty:
        if int(moves.bikes.sum()) != int(summary["bikes_moved"]):
            raise ValueError("搬运记录与汇总不一致")
        for (bucket, cell), row in states.set_index(["time_bucket", "cell_id"]).iterrows():
            outbound = moves[(moves.time_bucket == bucket) & (moves.from_cell == cell)].bikes.sum()
            inbound = moves[(moves.time_bucket == bucket) & (moves.to_cell == cell)].bikes.sum()
            if int(row.bikes_moved) != int(outbound):
                raise ValueError("逐格搬出账本不一致")
            end = row.inventory_start + inbound - outbound + row.returns_applied - row.borrow_met
            if int(end) != int(row.inventory_end):
                raise ValueError("逐格库存账本不守恒")
    distance = float(steps.distance_bike_km.sum())
    if not math.isclose(distance, float(summary["distance_bike_km"]), abs_tol=1e-7):
        raise ValueError("搬运工作量汇总不一致")
    weights = summary["assumptions"]
    cost = summary["unmet_requests"] * weights["unmet_penalty"] + distance * weights["distance_cost_per_bike_km"]
    if not math.isclose(cost, summary["composite_cost"], abs_tol=1e-7):
        raise ValueError("综合代价公式不一致")


class ArtifactStore:
    def __init__(self, config, layout, task, manifest, run_dir: Path):
        self.config, self.layout, self.task = config, layout, task
        self.manifest, self.run_dir = manifest, run_dir
        self.engine_id = engine_digest()
        self.cache: dict[tuple[str, int], dict] = {}

    def evaluate(self, candidate: dict) -> tuple[dict, bool]:
        verify_unchanged(self.config, self.layout, self.task.source, self.manifest)
        if engine_digest() != self.engine_id:
            raise ValueError("运行期间仿真引擎发生变化")
        key = (candidate["strategy"], candidate["budget"])
        if key in self.cache:
            return self.cache[key], True
        result = simulation_bundle(self.config, self.layout, self.task.source, *key)
        verify_unchanged(self.config, self.layout, self.task.source, self.manifest)
        validate_bundle(result, candidate["budget"])
        identity = {"input": self.manifest.digest, "engine": self.engine_id,
                    "source": self.task.source, "strategy": key[0], "budget": key[1]}
        digest = hashlib.sha256(encoded(identity).encode()).hexdigest()[:24]
        artifact_id, result_id = "art-" + digest, "res-" + digest
        target = self.run_dir / "artifacts" / artifact_id
        target.mkdir(parents=True, exist_ok=False)
        write_json(target / "summary.json", result.summary)
        result.steps.to_csv(target / "steps.csv", index=False)
        result.cell_states.to_parquet(target / "cell_states.parquet", index=False)
        write_json(target / "moves.json", result.moves.to_dict("records"))
        files = {p.name: hashlib.sha256(p.read_bytes()).hexdigest()
                 for p in sorted(target.iterdir()) if p.is_file()}
        write_json(target / "manifest.json", {
            "schema_version": "2.0", **identity, "result_id": result_id,
            "artifact_set_id": artifact_id, "status": "ready", "files": files,
        })
        metrics = {key: result.summary[key] for key in METRICS}
        defects = [f"{k}={v}" for k, v in result.violations.items() if v != 0]
        threshold = self.task.min_service_rate
        if threshold is not None and metrics["service_rate"] + 1e-9 < threshold:
            defects.append(f"模拟借车满足率 {metrics['service_rate']:.6f} < {threshold:.6f}")
        output = {
            "schema_version": "2.0", "source": self.task.source,
            "result_id": result_id, "artifact_set_id": artifact_id,
            "strategy": key[0], "budget": key[1], "simulated": True,
            "metrics": metrics, "violations": result.violations,
            "feasible": not defects, "defects": defects,
            "summary": result.summary, "trajectory": result.steps.to_dict("records"),
            "frozen_context_id": self.manifest.digest, "engine_digest": self.engine_id,
        }
        self.cache[key] = output
        return output, False


def load_spatial(run_dir: Path, result: dict) -> dict:
    artifact_id = result["artifact_set_id"]
    root = run_dir / "artifacts" / artifact_id
    import json
    manifest = json.loads((root / "manifest.json").read_text())
    if manifest["status"] != "ready" or manifest["result_id"] != result["result_id"]:
        raise ValueError("候选空间产物身份不匹配")
    for name, digest in manifest["files"].items():
        if Path(name).name != name or hashlib.sha256((root / name).read_bytes()).hexdigest() != digest:
            raise ValueError("候选空间产物摘要不匹配")
    return {
        "result_id": result["result_id"], "artifact_set_id": artifact_id,
        "frames": pd.read_csv(root / "steps.csv").to_dict("records"),
        "states": pd.read_parquet(root / "cell_states.parquet").assign(
            time_bucket=lambda f: f.time_bucket.astype(str)).to_dict("records"),
        "moves": json.loads((root / "moves.json").read_text()),
    }
