"""Adapter protocol: every data source normalises into the trip contract."""

from __future__ import annotations

import json
from abc import ABC, abstractmethod
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pandas as pd

from bike_agent.config import Layout, source_config
from bike_agent.data.contract import normalise_contract
from bike_agent.utils import sha256_file, write_json


class DataSourceError(RuntimeError):
    """Raised when raw data cannot be obtained or parsed."""


class DataSourceAdapter(ABC):
    """Base class for a data source.

    Subclasses only implement :meth:`fetch` (download + extract) and
    :meth:`load_trips` (raw -> contract).  Everything downstream consumes the
    contract table produced here (FR-DATA-2 / FR-DATA-6).
    """

    name: str = "base"

    def __init__(self, config: dict[str, Any], layout: Layout, force: bool = False):
        self.config = config
        self.layout = layout
        self.force = force
        self.source_cfg = source_config(config, self.name)
        self.stats: dict[str, Any] = {}

    # -- raw data -----------------------------------------------------------
    @property
    @abstractmethod
    def raw_dir(self) -> Path:
        ...

    @abstractmethod
    def fetch(self) -> dict[str, Any]:
        """Download and stage raw files. Returns a manifest entry."""

    @abstractmethod
    def load_trips(self) -> pd.DataFrame:
        """Return the source's trips in the contract columns."""

    def load_extras(self) -> dict[str, pd.DataFrame]:
        """Optional source-specific tables (fences, weather)."""
        return {}

    # -- helpers ------------------------------------------------------------
    def _record_manifest(self, files: list[Path], extra: dict[str, Any] | None = None) -> dict[str, Any]:
        manifest_path = self.layout.raw / "manifest.json"
        manifest: dict[str, Any] = {}
        if manifest_path.exists():
            try:
                manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                manifest = {}
        entry = {
            "source": self.name,
            "url": self.source_cfg.get("url"),
            "license": self.source_cfg.get("license"),
            "fetched_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "files": {},
        }
        for path in files:
            if path.exists():
                rel = str(path.relative_to(self.layout.root))
                entry["files"][rel] = {
                    "sha256": sha256_file(path),
                    "bytes": path.stat().st_size,
                }
        if extra:
            entry.update(extra)
        manifest[self.name] = entry
        write_json(manifest_path, manifest)
        return entry

    def contract_frame(self) -> pd.DataFrame:
        return normalise_contract(self.load_trips())
