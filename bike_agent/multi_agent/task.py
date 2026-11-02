"""Task construction, budget options and the frozen input manifest (F24 / F26).

The task is the run's constitution: source, objective, baseline strategy,
allowed strategies, budget options, the hard budget ceiling, the optional
service-rate constraint and every limit.  It is validated *before* the first
business call, and the input manifest pins the artefacts it was built from so a
mid-run data change stops the loop instead of mixing two datasets.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator

from bike_agent.config import Layout, source_config
from bike_agent.multi_agent.contracts import (
    OBJECTIVE,
    STRATEGIES,
    TASK_SCHEMA,
)


class TaskError(ValueError):
    """An invalid task definition; raised before any business call."""

class InputChangedError(RuntimeError):
    """A frozen input changed while the run was in progress."""

def _canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"))

def _sha256_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()

def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()

def budget_options(baseline_budget: int, max_budget: int) -> tuple[int, ...]:
    """The legal per-bucket budget tiers, derived from the baseline.

    The rule is fixed by REQUIREMENTS-PHASE6 §2.1: half, the baseline itself,
    and double -- de-duplicated, non-negative and capped by the user's hard
    limit.  With the default ceiling (``max_budget == baseline``) the doubled
    tier is not offered, which is exactly why "the default run cannot quietly
    spend 400" is a property of the code rather than of a reviewer's memory.
    """
    if baseline_budget < 0:
        raise TaskError("baseline_budget must be >= 0")
    if max_budget < 0:
        raise TaskError("max_budget must be >= 0")
    if max_budget < baseline_budget:
        raise TaskError(
            f"max_budget ({max_budget}) is below the baseline budget ({baseline_budget}); "
            "raise the ceiling explicitly or choose a legal baseline budget"
        )
    tiers = {baseline_budget // 2, baseline_budget, baseline_budget * 2}
    return tuple(sorted(tier for tier in tiers if 0 <= tier <= max_budget))

@dataclass(frozen=True)
class TaskSpec:
    task_id: str
    source: str
    objective: str
    baseline_strategy: str
    allowed_strategies: tuple[str, ...]
    baseline_budget: int
    budget_options: tuple[int, ...]
    max_budget: int
    min_service_rate: float | None
    max_rounds: int
    max_candidates_per_round: int
    max_distinct_candidates: int
    max_tool_calls: int
    tool_timeout_s: float
    task_timeout_s: float
    mode: str
    seed: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "source": self.source,
            "objective": self.objective,
            "baseline_strategy": self.baseline_strategy,
            "allowed_strategies": list(self.allowed_strategies),
            "baseline_budget": self.baseline_budget,
            "budget_options": list(self.budget_options),
            "max_budget": self.max_budget,
            "min_service_rate": self.min_service_rate,
            "max_rounds": self.max_rounds,
            "max_candidates_per_round": self.max_candidates_per_round,
            "max_distinct_candidates": self.max_distinct_candidates,
            "max_tool_calls": self.max_tool_calls,
            "tool_timeout_s": self.tool_timeout_s,
            "task_timeout_s": self.task_timeout_s,
            "mode": self.mode,
            "seed": self.seed,
        }

    def validate(self) -> None:
        """Schema plus the semantic rules the schema cannot express."""
        Draft202012Validator.check_schema(TASK_SCHEMA)
        errors = sorted(Draft202012Validator(TASK_SCHEMA).iter_errors(self.to_dict()), key=str)
        if errors:
            raise TaskError("; ".join(error.message for error in errors))
        if self.source not in ("xm", "sh"):
            raise TaskError(f"unsupported source {self.source!r}")
        if self.objective != OBJECTIVE:
            raise TaskError(f"objective must be {OBJECTIVE!r}")
        if self.baseline_strategy not in self.allowed_strategies:
            raise TaskError("baseline_strategy must be in allowed_strategies")
        if self.baseline_budget not in self.budget_options:
            raise TaskError("baseline_budget must be one of budget_options")
        if any(budget > self.max_budget for budget in self.budget_options):
            raise TaskError("budget_options contains a tier above max_budget")
        if self.max_budget < self.baseline_budget:
            raise TaskError("max_budget must be >= baseline_budget")
        if self.min_service_rate is not None and not 0.0 <= self.min_service_rate <= 1.0:
            raise TaskError("min_service_rate must be in [0, 1]")
        if self.max_rounds < 1:
            raise TaskError("max_rounds must be >= 1")
        if not 1 <= self.max_candidates_per_round <= 2:
            raise TaskError("max_candidates_per_round must be 1 or 2")
        if self.max_tool_calls < 1 or self.task_timeout_s <= 0 or self.tool_timeout_s <= 0:
            raise TaskError("limits must be positive")

def build_task(
    config: dict[str, Any],
    source: str,
    *,
    baseline_budget: int | None = None,
    max_budget: int | None = None,
    max_rounds: int = 3,
    min_service_rate: float | None = None,
    max_candidates_per_round: int = 2,
    max_distinct_candidates: int = 6,
    max_tool_calls: int = 20,
    tool_timeout_s: float = 60.0,
    task_timeout_s: float = 300.0,
    mode: str = "deterministic",
    seed: int | None = None,
    task_id: str | None = None,
) -> TaskSpec:
    """Build and validate the task before any business data is read."""
    if source not in config.get("sources", {}):
        raise TaskError(f"unknown source {source!r}; configured: {sorted(config.get('sources', {}))}")
    sim_cfg = config.get("simulation", {})
    baseline = int(sim_cfg.get("max_moves_per_bucket", 200)) if baseline_budget is None else int(baseline_budget)
    ceiling = baseline if max_budget is None else int(max_budget)
    options = budget_options(baseline, ceiling)
    task = TaskSpec(
        task_id=task_id or f"task-{source}-{int(seed if seed is not None else config.get('seed', 42))}",
        source=source,
        objective=OBJECTIVE,
        baseline_strategy="rule",
        allowed_strategies=tuple(STRATEGIES),
        baseline_budget=baseline,
        budget_options=options,
        max_budget=ceiling,
        min_service_rate=min_service_rate,
        max_rounds=int(max_rounds),
        max_candidates_per_round=int(max_candidates_per_round),
        max_distinct_candidates=int(max_distinct_candidates),
        max_tool_calls=int(max_tool_calls),
        tool_timeout_s=float(tool_timeout_s),
        task_timeout_s=float(task_timeout_s),
        mode=mode,
        seed=int(seed if seed is not None else config.get("seed", 42)),
    )
    task.validate()
    return task

# -- input freezing ---------------------------------------------------------
@dataclass(frozen=True)
class InputManifest:
    digest: str
    config_digest: str
    files: tuple[dict[str, Any], ...] = field(default_factory=tuple)

    def to_dict(self) -> dict[str, Any]:
        return {
            "digest": self.digest,
            "config_digest": self.config_digest,
            "files": [dict(entry) for entry in self.files],
        }

def _input_paths(config: dict[str, Any], layout: Layout, source: str) -> list[Path]:
    cell_size = int(source_config(config, source)["cell_size_m"])
    required = [
        layout.panel_path(source, cell_size),
        layout.forecast_path(source),
        layout.clean_path(source),
        layout.cells_path(source, cell_size),
        layout.priority_replenish_path(source),
        layout.priority_clear_path(source),
    ]
    optional = [
        layout.forecast_meta_path(source),
        layout.fences_path(source),
        layout.outputs / f"risk_summary.{source}.json",
    ]
    missing = [str(path) for path in required if not path.exists()]
    if missing:
        raise FileNotFoundError(
            "missing simulation/forecast input(s): " + ", ".join(missing) + "; run `bike-agent repro` first"
        )
    return required + [path for path in optional if path.exists()]

def _config_snapshot(config: dict[str, Any], source: str) -> dict[str, Any]:
    """Only the keys that can change a decision or an evaluation."""
    return {
        "seed": config.get("seed"),
        "primary_source": config.get("primary_source"),
        "source": source_config(config, source),
        "clean": config.get("clean", {}),
        "forecast": config.get("forecast", {}),
        "risk": config.get("risk", {}),
        "simulation": config.get("simulation", {}),
    }

def freeze_inputs(config: dict[str, Any], layout: Layout, source: str) -> InputManifest:
    """Pin every artefact the tools may read, plus the decision-relevant config."""
    files: list[dict[str, Any]] = []
    for path in _input_paths(config, layout, source):
        try:
            relative = str(path.relative_to(layout.root))
        except ValueError:
            relative = str(path)
        files.append({"path": relative, "sha256": _sha256_file(path), "bytes": path.stat().st_size})
    config_digest = _sha256_bytes(_canonical(_config_snapshot(config, source)).encode("utf-8"))
    digest = _sha256_bytes(
        _canonical({"config": config_digest, "files": files}).encode("utf-8")
    )
    return InputManifest(digest=digest, config_digest=config_digest, files=tuple(files))

def verify_unchanged(
    config: dict[str, Any], layout: Layout, source: str, manifest: InputManifest
) -> None:
    """Recompute the manifest and refuse to continue if anything moved."""
    current = freeze_inputs(config, layout, source)
    if current.digest == manifest.digest:
        return
    before = {entry["path"]: entry["sha256"] for entry in manifest.files}
    after = {entry["path"]: entry["sha256"] for entry in current.files}
    changed = sorted(
        path for path in set(before) | set(after) if before.get(path) != after.get(path)
    )
    raise InputChangedError(
        "frozen inputs changed during the run: " + ", ".join(changed)
    )
