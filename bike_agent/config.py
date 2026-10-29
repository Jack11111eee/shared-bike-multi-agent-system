"""Configuration loading and project path helpers.

The pipeline is driven by ``bike_agent/config/default.yaml``; a user-supplied
``--config`` file is deep-merged on top of it.  Nothing that changes a result
is hard-coded in the modules: grid size, split fraction, simulation
assumptions and the random seed all live here (D6 / NF-1).
"""

from __future__ import annotations

import copy
import os
from pathlib import Path
from typing import Any

import yaml

_DEFAULTS_PATH = Path(__file__).resolve().parent / "config" / "default.yaml"


class ConfigError(RuntimeError):
    """Raised when configuration is missing or malformed."""


def _deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    merged = copy.deepcopy(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = _deep_merge(merged[key], value)
        else:
            merged[key] = copy.deepcopy(value)
    return merged


def load_config(path: str | os.PathLike[str] | None = None) -> dict[str, Any]:
    """Load the default configuration, optionally overridden by ``path``."""
    if not _DEFAULTS_PATH.exists():
        raise ConfigError(f"default configuration missing: {_DEFAULTS_PATH}")
    with _DEFAULTS_PATH.open("r", encoding="utf-8") as fh:
        config = yaml.safe_load(fh) or {}
    if path is not None:
        override_path = Path(path)
        if not override_path.exists():
            raise ConfigError(f"config override not found: {override_path}")
        with override_path.open("r", encoding="utf-8") as fh:
            override = yaml.safe_load(fh) or {}
        if not isinstance(override, dict):
            raise ConfigError(f"config override must be a mapping: {override_path}")
        config = _deep_merge(config, override)
    return config


def source_config(config: dict[str, Any], source: str) -> dict[str, Any]:
    sources = config.get("sources", {})
    if source not in sources:
        raise ConfigError(
            f"unknown data source {source!r}; configured sources: {sorted(sources)}"
        )
    return sources[source]


def project_root(start: str | os.PathLike[str] | None = None) -> Path:
    """Resolve the project root.

    Priority: ``BIKE_AGENT_ROOT`` env var, then the nearest ancestor of this
    file (or ``start``) that contains ``pyproject.toml``, then the current
    working directory.
    """
    env = os.environ.get("BIKE_AGENT_ROOT")
    if env:
        return Path(env).expanduser().resolve()
    candidates = []
    if start is not None:
        candidates.append(Path(start).resolve())
    candidates.append(Path(__file__).resolve())
    for candidate in candidates:
        for parent in [candidate, *candidate.parents]:
            if (parent / "pyproject.toml").exists():
                return parent
    return Path.cwd().resolve()


class Layout:
    """Filesystem layout rooted at a project directory."""

    def __init__(self, root: str | os.PathLike[str] | None = None):
        self.root = Path(root).resolve() if root is not None else project_root()
        self.raw = self.root / "data" / "raw"
        self.processed = self.root / "data" / "processed"
        self.outputs = self.root / "outputs"
        self.reports = self.root / "reports"
        self.figures = self.root / "figures"

    def ensure(self) -> "Layout":
        for path in (
            self.raw,
            self.processed,
            self.outputs,
            self.reports,
            self.figures,
        ):
            path.mkdir(parents=True, exist_ok=True)
        return self

    def raw_dir(self, source: str) -> Path:
        path = self.raw / source
        path.mkdir(parents=True, exist_ok=True)
        return path

    # -- processed artefacts ------------------------------------------------
    def contract_path(self, source: str) -> Path:
        return self.processed / f"contract.{source}.parquet"

    def clean_path(self, source: str) -> Path:
        return self.processed / f"clean.{source}.parquet"

    def panel_path(self, source: str, cell_size_m: int) -> Path:
        return self.processed / f"panel.{source}.{cell_size_m}m.parquet"

    def cells_path(self, source: str, cell_size_m: int) -> Path:
        return self.processed / f"cells.{source}.{cell_size_m}m.parquet"

    def fences_path(self, source: str) -> Path:
        return self.processed / f"fences.{source}.parquet"

    def weather_path(self, source: str) -> Path:
        return self.processed / f"weather.{source}.parquet"

    # -- outputs ------------------------------------------------------------
    def forecast_path(self, source: str) -> Path:
        return self.outputs / f"forecast.{source}.parquet"

    def forecast_meta_path(self, source: str) -> Path:
        return self.outputs / f"forecast_meta.{source}.json"

    def priority_replenish_path(self, source: str) -> Path:
        return self.outputs / f"priority_replenish.{source}.csv"

    def priority_clear_path(self, source: str) -> Path:
        return self.outputs / f"priority_clear.{source}.csv"

    def sim_path(self, source: str, strategy: str, suffix: str) -> Path:
        return self.outputs / f"sim_{strategy}.{source}.{suffix}"

    def sensitivity_path(self, source: str) -> Path:
        return self.outputs / f"sensitivity.{source}.json"

    def key_numbers_path(self) -> Path:
        return self.outputs / "key_numbers.json"

    # -- reports ------------------------------------------------------------
    def report_path(self, name: str) -> Path:
        return self.reports / name
