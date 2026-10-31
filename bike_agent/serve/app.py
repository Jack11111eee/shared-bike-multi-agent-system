"""F9: a thin HTTP facade over the Phase 1 artefacts and the F7 tool contracts.

Nothing in this package computes anything.  Every response is either a row read
back from a persisted artefact or the return value of an existing function in
:mod:`bike_agent.tools.handlers` -- that is what makes the layer provably thin
(F9-3) and what keeps the CLI, the front-end and any agent on one contract.

The server is built on the standard library on purpose: it must start and serve
in an environment with no LLM SDK (F9-4) and with no extra HTTP framework
installed.
"""

from __future__ import annotations

import json
import posixpath
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable
from urllib.parse import parse_qs, unquote, urlparse

import pandas as pd

from bike_agent.config import Layout, load_config, source_config
from bike_agent.replay.player import build_replay_data
from bike_agent.tools import handlers
from bike_agent.utils import read_json

STRATEGIES = ("rule", "optimized")
# GET paths served as JSON even though they do not start with /api or /tools.
# ``_Handler._is_api`` reads this set, so adding an endpoint here is what keeps
# the static file handler from swallowing it.
LEGACY_API_PATHS = frozenset({"/sources", "/health", "/replay/index", "/replay/frame", "/replay/timeline"})
STATIC_TYPES = {
    ".html": "text/html",
    ".css": "text/css",
    ".js": "application/javascript",
    ".png": "image/png",
    ".svg": "image/svg+xml",
    ".json": "application/json",
    ".txt": "text/plain",
    ".ico": "image/x-icon",
}


class ServiceError(RuntimeError):
    """A request that cannot be served; carries the HTTP status to return."""

    def __init__(self, message: str, status: int = HTTPStatus.NOT_FOUND):
        super().__init__(message)
        self.status = int(status)


# -- shared helpers ---------------------------------------------------------
def _require(path, what: str):
    if not path.exists():
        raise ServiceError(f"missing {what}: {path}; run `bike-agent repro` first")
    return path


def _resolve_source(config: dict[str, Any], source: str | None) -> str:
    if source is None:
        return str(config.get("primary_source", "xm"))
    if source not in config.get("sources", {}):
        raise ServiceError(f"unknown source {source!r}", HTTPStatus.BAD_REQUEST)
    return source


def _resolve_strategy(strategy: str | None) -> str:
    if strategy is None:
        return "rule"
    if strategy not in STRATEGIES:
        raise ServiceError(f"strategy must be one of {list(STRATEGIES)}", HTTPStatus.BAD_REQUEST)
    return strategy


def _step_rows(layout: Layout, source: str, strategy: str) -> pd.DataFrame:
    path = _require(layout.sim_path(source, strategy, "csv"), f"simulation steps for {source}/{strategy}")
    frame = pd.read_csv(path)
    frame["time_bucket"] = pd.to_datetime(frame["time_bucket"])
    return frame.sort_values("time_bucket").reset_index(drop=True)


def _bucket_key(value: Any) -> str:
    return pd.Timestamp(value).strftime("%Y-%m-%d %H:%M:%S")


def _summary_of(row: pd.Series) -> dict[str, Any]:
    """One CSV row as a JSON-safe mapping, values unchanged (no rounding)."""
    return {
        "time_bucket": _bucket_key(row["time_bucket"]),
        "borrow_requests": int(row["borrow_requests"]),
        "borrow_met": int(row["borrow_met"]),
        "unmet_requests": int(row["unmet_requests"]),
        "returns": int(row["returns"]),
        "returns_applied": int(row["returns_applied"]),
        "returns_overflow": int(row["returns_overflow"]),
        "bikes_moved": int(row["bikes_moved"]),
        "distance_bike_km": float(row["distance_bike_km"]),
        "inventory_start": int(row["inventory_start"]),
        "inventory_end": int(row["inventory_end"]),
    }


# -- capabilities -----------------------------------------------------------
def list_sources(config: dict[str, Any], layout: Layout) -> dict[str, Any]:
    """Every configured source with its grid size and replay time range.

    A source that has not been prepared is reported as ``available: false``
    with the artefacts it is missing -- an explicit statement, never an empty
    object that could be mistaken for a successful answer (F9-5).
    """
    sources = []
    for source in config.get("sources", {}):
        cell_size = int(source_config(config, source)["cell_size_m"])
        entry: dict[str, Any] = {
            "source": source,
            "cell_size_m": cell_size,
            "n_cells": None,
            "n_buckets": None,
            "first_bucket": None,
            "last_bucket": None,
            "strategies": [],
            "frames_per_strategy": {},
            "frames_available": [],
            "strategies_aligned": False,
            "available": False,
            "missing": [],
        }
        cells_path = layout.cells_path(source, cell_size)
        if cells_path.exists():
            entry["n_cells"] = int(len(pd.read_parquet(cells_path)))
        else:
            entry["missing"].append(str(cells_path))
        # Frame counts are reported per strategy: the two strategies could in
        # principle replay different bucket sets, and collapsing them into one
        # number would hide that instead of surfacing it.
        entry["frames_per_strategy"] = {}
        entry["frames_available"] = []
        for strategy in STRATEGIES:
            steps_path = layout.sim_path(source, strategy, "csv")
            replay_path = layout.replay_path(source, strategy)
            if not steps_path.exists():
                entry["missing"].append(str(steps_path))
                continue
            steps = _step_rows(layout, source, strategy)
            entry["strategies"].append(strategy)
            entry["frames_per_strategy"][strategy] = int(len(steps))
            entry["n_buckets"] = int(len(steps))
            entry["first_bucket"] = _bucket_key(steps["time_bucket"].min())
            entry["last_bucket"] = _bucket_key(steps["time_bucket"].max())
            # The frame endpoint needs the per-cell state too; a source that has
            # step totals but no replay file can serve the index but not a frame.
            if replay_path.exists():
                entry["frames_available"].append(strategy)
            else:
                entry["missing"].append(str(replay_path))
        entry["available"] = entry["n_cells"] is not None and bool(entry["frames_available"])
        counts = set(entry["frames_per_strategy"].values())
        entry["strategies_aligned"] = len(counts) <= 1
        sources.append(entry)
    return {"sources": sources, "primary_source": str(config.get("primary_source", ""))}


def replay_index(config: dict[str, Any], layout: Layout, source: str | None, strategy: str | None) -> dict[str, Any]:
    """The time axis: one entry per persisted bucket, in order, no filling."""
    source = _resolve_source(config, source)
    strategy = _resolve_strategy(strategy)
    steps = _step_rows(layout, source, strategy)
    frames = [
        {
            "index": position,
            "time_bucket": _bucket_key(row["time_bucket"]),
            "unmet_requests": int(row["unmet_requests"]),
            "bikes_moved": int(row["bikes_moved"]),
        }
        for position, (_, row) in enumerate(steps.iterrows())
    ]
    return {"source": source, "strategy": strategy, "n_frames": len(frames), "frames": frames}


def replay_frame(
    config: dict[str, Any],
    layout: Layout,
    source: str | None,
    strategy: str | None,
    time_bucket: str,
) -> dict[str, Any]:
    """One frame: per-cell state + that bucket's moves + that bucket's totals."""
    source = _resolve_source(config, source)
    strategy = _resolve_strategy(strategy)
    steps = _step_rows(layout, source, strategy)
    try:
        target = pd.Timestamp(time_bucket)
    except (ValueError, TypeError) as exc:
        raise ServiceError(
            f"invalid time_bucket {time_bucket!r}: expected e.g. '2020-12-25 06:00:00'",
            HTTPStatus.BAD_REQUEST,
        ) from exc
    matched = steps.loc[steps["time_bucket"] == target]
    if matched.empty:
        raise ServiceError(
            f"no frame for source={source!r} strategy={strategy!r} time_bucket={time_bucket!r}",
            HTTPStatus.NOT_FOUND,
        )

    states = pd.read_parquet(_require(layout.replay_path(source, strategy), "per-cell replay state"))
    states["time_bucket"] = pd.to_datetime(states["time_bucket"])
    cells = states.loc[states["time_bucket"] == target].sort_values("cell_id")
    if cells.empty:
        raise ServiceError(
            f"replay state has no rows for {source}/{strategy} at {time_bucket!r}", HTTPStatus.NOT_FOUND
        )

    moves_path = _require(layout.outputs / f"sim_moves.{strategy}.{source}.json", "dispatch records")
    moves = [
        record
        for record in read_json(moves_path)
        if pd.Timestamp(record["time_bucket"]) == target
    ]

    return {
        "source": source,
        "strategy": strategy,
        "time_bucket": _bucket_key(target),
        "summary": _summary_of(matched.iloc[0]),
        "cells": [
            {
                "cell_id": str(row.cell_id),
                "inventory_start": int(row.inventory_start),
                "borrow_requests": int(row.borrow_requests),
                "borrow_met": int(row.borrow_met),
                "unmet_requests": int(row.unmet_requests),
                "returns": int(row.returns),
                "returns_applied": int(row.returns_applied),
                "returns_overflow": int(row.returns_overflow),
                "bikes_moved": int(row.bikes_moved),
                "inventory_end": int(row.inventory_end),
            }
            for row in cells.itertuples(index=False)
        ],
        "moves": moves,
        "n_moves": len(moves),
    }


# -- F15 static hosting + timeline -----------------------------------------
def replay_timeline(config: dict[str, Any], layout: Layout, source: str | None) -> dict[str, Any]:
    """The player bundle, served over HTTP (F15-3).

    This is the *same* :func:`bike_agent.replay.player.build_replay_data` the
    static site is built from -- not a second derivation.  A source whose
    artefacts are missing is an explicit 404, never an empty success (F15-4).
    """
    source = _resolve_source(config, source)
    try:
        return build_replay_data(config, layout, source)
    except FileNotFoundError as exc:
        raise ServiceError(str(exc), HTTPStatus.NOT_FOUND) from exc


def resolve_static(layout: Layout, request_path: str) -> Path:
    """Map a URL path to a file inside ``replay/``, or refuse.

    Defensive on purpose (F15-2): the path is decoded repeatedly (so a
    double-encoded traversal cannot slip through), any ``..`` segment or
    backslash is a 400, and the resolved target must stay inside the site
    directory even after symlink resolution.
    """
    decoded = request_path
    for _ in range(3):
        expanded = unquote(decoded)
        if expanded == decoded:
            break
        decoded = expanded
    if "\x00" in decoded or "\\" in decoded:
        raise ServiceError("invalid path", HTTPStatus.BAD_REQUEST)
    relative = decoded.lstrip("/")
    parts = [part for part in relative.split("/") if part not in ("", ".")]
    if any(part == ".." for part in parts):
        raise ServiceError("path traversal is not allowed", HTTPStatus.BAD_REQUEST)
    normalized = posixpath.normpath("/".join(parts)) if parts else "index.html"
    root = (layout.root / "replay").resolve()
    target = (root / normalized).resolve()
    if target != root and root not in target.parents:
        raise ServiceError("path traversal is not allowed", HTTPStatus.BAD_REQUEST)
    return target


# -- F7 tool contracts, re-exported unchanged -------------------------------
# Input validation happens here so a bad parameter is a 400, not a 404 that
# claims the artefact is missing.  Successful responses still come straight
# from the handlers, unchanged (F9-3).
def tool_forecast(
    config: dict[str, Any],
    layout: Layout,
    source: str | None,
    time_bucket: str | None,
    cell_ids: list[str] | None,
) -> dict[str, Any]:
    _resolve_source(config, source)
    return handlers.get_forecast(config, layout, source=source, cell_ids=cell_ids, time_bucket=time_bucket)


def tool_risk(
    config: dict[str, Any],
    layout: Layout,
    direction: str,
    top_k: int,
    source: str | None,
) -> dict[str, Any]:
    if direction not in ("replenish", "clear"):
        raise ServiceError("direction must be 'replenish' or 'clear'", HTTPStatus.BAD_REQUEST)
    if top_k < 1:
        raise ServiceError("top_k must be >= 1", HTTPStatus.BAD_REQUEST)
    _resolve_source(config, source)
    return handlers.get_risk_ranking(config, layout, direction=direction, top_k=top_k, source=source)


def tool_simulate(
    config: dict[str, Any],
    layout: Layout,
    strategy: str,
    source: str | None,
    budget: int | None,
) -> dict[str, Any]:
    _resolve_strategy(strategy)
    _resolve_source(config, source)
    return handlers.run_simulation(config, layout, strategy=strategy, budget=budget, source=source)


# -- HTTP plumbing ----------------------------------------------------------
def _json_default(value: Any) -> Any:
    if hasattr(value, "item"):
        return value.item()
    if isinstance(value, pd.Timestamp):
        return value.isoformat()
    raise TypeError(f"not JSON serialisable: {type(value)!r}")


def _encode(payload: dict[str, Any]) -> bytes:
    return json.dumps(payload, ensure_ascii=False, default=_json_default).encode("utf-8")


class _Handler(BaseHTTPRequestHandler):
    server_version = "bike-agent/0.1"

    # Injected by ``make_server``.
    config: dict[str, Any]
    layout: Layout

    def log_message(self, fmt: str, *args: Any) -> None:  # keep test output quiet
        pass

    def _send(self, status: int, payload: dict[str, Any]) -> None:
        body = _encode(payload)
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _query(self) -> dict[str, str]:
        parsed = urlparse(self.path)
        return {key: values[-1] for key, values in parse_qs(parsed.query).items()}

    def _run(self, action: Callable[[], dict[str, Any]]) -> None:
        """Run one request, mapping every failure mode to an explicit response.

        A missing artefact or a bad parameter must never look like success
        (F9-5), so there is exactly one place that decides status codes.
        """
        try:
            payload = action()
        except ServiceError as exc:
            self._send(exc.status, {"error": str(exc), "kind": "ServiceError"})
        except handlers.ToolError as exc:
            self._send(HTTPStatus.NOT_FOUND, {"error": str(exc), "kind": "ToolError"})
        except Exception as exc:  # pragma: no cover - last-resort guard
            self._send(HTTPStatus.INTERNAL_SERVER_ERROR, {"error": str(exc), "kind": type(exc).__name__})
        else:
            self._send(HTTPStatus.OK, payload)

    def _send_static(self, target: Path) -> None:
        if not target.is_file():
            raise ServiceError(f"not found: {target.name}", HTTPStatus.NOT_FOUND)
        content_type = STATIC_TYPES.get(target.suffix.lower())
        if content_type is None:
            raise ServiceError(f"refusing to serve {target.suffix!r}", HTTPStatus.NOT_FOUND)
        body = target.read_bytes()
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _run_static(self, path: str) -> None:
        try:
            target = resolve_static(self.layout, path)
            self._send_static(target)
        except ServiceError as exc:
            self._send(exc.status, {"error": str(exc), "kind": "ServiceError"})
        except Exception as exc:  # pragma: no cover - last-resort guard
            self._send(HTTPStatus.INTERNAL_SERVER_ERROR, {"error": str(exc), "kind": type(exc).__name__})

    @staticmethod
    def _is_api(path: str) -> bool:
        return path.startswith(("/api/", "/tools/")) or path in LEGACY_API_PATHS

    def _route_get(self, path: str, params: dict[str, str]) -> dict[str, Any]:
        if path in ("/api/sources", "/sources"):
            return list_sources(self.config, self.layout)
        if path in ("/api/replay/index", "/replay/index"):
            return replay_index(self.config, self.layout, params.get("source"), params.get("strategy"))
        if path in ("/api/replay/frame", "/replay/frame"):
            bucket = params.get("time_bucket") or params.get("bucket")
            if not bucket:
                raise ServiceError("missing query parameter `time_bucket`", HTTPStatus.BAD_REQUEST)
            return replay_frame(self.config, self.layout, params.get("source"), params.get("strategy"), bucket)
        if path in ("/api/replay/timeline", "/replay/timeline"):
            return replay_timeline(self.config, self.layout, params.get("source"))
        if path in ("/api/tools/forecast", "/tools/forecast"):
            cells = params.get("cells")
            return tool_forecast(
                self.config,
                self.layout,
                params.get("source"),
                params.get("time_bucket"),
                [c for c in cells.split(",") if c] if cells else None,
            )
        if path in ("/api/tools/risk", "/tools/risk"):
            try:
                top_k = int(params.get("top_k", 10))
            except ValueError as exc:
                raise ServiceError(f"top_k must be an integer: {exc}", HTTPStatus.BAD_REQUEST) from exc
            return tool_risk(
                self.config,
                self.layout,
                params.get("direction", "replenish"),
                top_k,
                params.get("source"),
            )
        if path in ("/api/health", "/health"):
            return {"status": "ok"}
        raise ServiceError(f"unknown path {path!r}", HTTPStatus.NOT_FOUND)

    def _route_post(self, path: str, body: dict[str, Any]) -> dict[str, Any]:
        if path in ("/api/tools/simulate", "/tools/simulate"):
            budget = body.get("budget")
            try:
                budget_value = int(budget) if budget is not None else None
            except (TypeError, ValueError) as exc:
                raise ServiceError(f"budget must be an integer: {exc}", HTTPStatus.BAD_REQUEST) from exc
            return tool_simulate(
                self.config,
                self.layout,
                str(body.get("strategy", "rule")),
                body.get("source"),
                budget_value,
            )
        raise ServiceError(f"unknown path {path!r}", HTTPStatus.NOT_FOUND)

    def do_GET(self) -> None:  # noqa: N802 - stdlib naming
        path = urlparse(self.path).path
        params = self._query()
        if not self._is_api(path):
            self._run_static(path)
            return
        self._run(lambda: self._route_get(path, params))

    def do_POST(self) -> None:  # noqa: N802 - stdlib naming
        path = urlparse(self.path).path
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            self._send(
                HTTPStatus.BAD_REQUEST,
                {"error": "Content-Length must be an integer", "kind": "ServiceError"},
            )
            return
        try:
            body = json.loads(self.rfile.read(length) or b"{}")
        except json.JSONDecodeError as exc:
            self._send(HTTPStatus.BAD_REQUEST, {"error": f"invalid JSON body: {exc}", "kind": "ServiceError"})
            return
        if not isinstance(body, dict):
            self._send(
                HTTPStatus.BAD_REQUEST,
                {"error": "request body must be a JSON object", "kind": "ServiceError"},
            )
            return
        self._run(lambda: self._route_post(path, body))


def make_server(config: dict[str, Any], layout: Layout, host: str = "127.0.0.1", port: int = 0) -> ThreadingHTTPServer:
    handler = type("BikeAgentHandler", (_Handler,), {"config": config, "layout": layout})
    server = ThreadingHTTPServer((host, port), handler)
    server.daemon_threads = True
    return server


def check(config: dict[str, Any] | None = None, layout: Layout | None = None) -> dict[str, Any]:
    """Validate that the service can answer every capability, without binding a port.

    F9-5 lives here too: a missing artefact is reported, never papered over with
    an empty payload.
    """
    config = config or load_config()
    layout = layout or Layout()
    checks: list[dict[str, Any]] = []
    listing = list_sources(config, layout)
    checks.append({"capability": "sources", "ok": bool(listing["sources"]), "detail": len(listing["sources"])})
    for entry in listing["sources"]:
        source = entry["source"]
        if not entry["available"]:
            checks.append(
                {
                    "capability": "source",
                    "ok": False,
                    "detail": f"{source}: unavailable, missing {', '.join(entry['missing'])}",
                }
            )
            continue
        for strategy in entry["strategies"]:
            try:
                index = replay_index(config, layout, source, strategy)
                checks.append(
                    {
                        "capability": "replay_index",
                        "ok": index["n_frames"] > 0,
                        "detail": f"{source}/{strategy}: {index['n_frames']} frames",
                    }
                )
                if index["frames"]:
                    frame = replay_frame(config, layout, source, strategy, index["frames"][0]["time_bucket"])
                    checks.append(
                        {
                            "capability": "replay_frame",
                            "ok": len(frame["cells"]) > 0,
                            "detail": f"{source}/{strategy}: {len(frame['cells'])} cells, {frame['n_moves']} moves",
                        }
                    )
            except ServiceError as exc:
                checks.append(
                    {"capability": "replay_frame", "ok": False, "detail": f"{source}/{strategy}: {exc}"}
                )
    replay_dir = layout.root / "replay"
    players = sorted(path.name for path in replay_dir.glob("*/player.html")) if replay_dir.exists() else []
    checks.append(
        {
            "capability": "static_site",
            "ok": (replay_dir / "index.html").exists() and bool(players),
            "detail": f"replay/index.html={'yes' if (replay_dir / 'index.html').exists() else 'no'}, "
            f"players={players or 'none'}",
        }
    )
    for entry in listing["sources"]:
        if not entry["available"]:
            continue
        source = entry["source"]
        try:
            timeline = replay_timeline(config, layout, source)
            checks.append(
                {
                    "capability": "replay_timeline",
                    "ok": timeline["n_frames"] > 0 and len(timeline["frames"]) == timeline["n_frames"],
                    "detail": f"{source}: {timeline['n_frames']} frames",
                }
            )
        except ServiceError as exc:
            checks.append({"capability": "replay_timeline", "ok": False, "detail": f"{source}: {exc}"})
    failures = [item for item in checks if not item["ok"]]
    return {"ok": not failures, "checks": checks}
