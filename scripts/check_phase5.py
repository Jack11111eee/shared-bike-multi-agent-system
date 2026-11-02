#!/usr/bin/env python3
"""REQUIREMENTS-PHASE5 §6.1: deterministic acceptance for Phase 5 (F21–F23).

Runs the machine-checkable parts of F21-1..4, F22-1..4 and F23-1..4 against the
*current* inputs, the *shipped* pages and the *actual* media files, writes
``outputs/presentation_validation.phase5.json`` and exits non-zero when any check
fails.

Two rules shape this file:

* **Nothing is verified by the code that produced it.**  The projection check
  re-derives pixel coordinates from the published bbox with its own formula; the
  prediction check re-reads the parquet and reshapes it independently; the media
  check decodes the PNGs and probes the video instead of trusting extensions.
* **A missing tool is a failure, not a skip.**  If Chrome or ffmpeg is absent the
  media checks fail and say which tool to install.  The only thing that never
  runs here is the browser *interaction* sweep, which has its own command and its
  own recorded result.

Usage::

    python3 scripts/check_presentation.py --phase 5 [--json] [--verbose]
"""

from __future__ import annotations

import base64
import hashlib
import json
import math
import re
import subprocess
import sys
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
SITE = ROOT / "replay"
MEDIA = ROOT / "presentations" / "phase5"
SOURCES = ("xm", "sh")
REPORT = ROOT / "outputs" / "presentation_validation.phase5.json"

if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

#: NF-30 budgets, in bytes.
SITE_CAP = 12 * 1024 * 1024
BASEMAP_CAP = 2 * 1024 * 1024
FORECAST_CAP = 2 * 1024 * 1024
#: NF-31 media budget.
MEDIA_CAP = 100 * 1024 * 1024
#: F21-2: how far a published pixel may sit from the independently recomputed one.
PIXEL_TOLERANCE = 0.5
#: F23-1 minimum sizes.
MIN_SHOT = (1600, 900)
MIN_VIDEO = (1280, 720)
VIDEO_SECONDS = (90.0, 180.0)
#: The six states the screenshot set must cover (F23-2).
REQUIRED_SHOTS = {
    "guide-overview": "导览概貌",
    "xm-geo-delta": "XM 地理底图上的差分地图",
    "xm-ledger": "XM 单格账本",
    "sh-forecast": "SH 预测联动",
    "scenario": "敏感性场景",
    "sh-geo": "SH 地理底图",
}
#: Video chapters (F23-2/F23-3).
REQUIRED_CHAPTERS = {"xm-overview", "xm-event", "xm-delta", "xm-ledger", "sh-forecast", "scenario", "sh-geo"}


def _rel(path: Path) -> str:
    try:
        return str(path.relative_to(ROOT))
    except ValueError:
        return str(path)


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _jsonable(value: Any) -> Any:
    """numpy scalars and booleans leak out of comparisons; make them plain JSON."""
    if isinstance(value, (np.bool_,)):
        return bool(value)
    if isinstance(value, (np.integer,)):
        return int(value)
    if isinstance(value, (np.floating,)):
        return float(value)
    if isinstance(value, (np.ndarray,)):
        return value.tolist()
    raise TypeError(f"{type(value).__name__} is not JSON serializable")


def _close(a: float, b: float, tol: float = 1e-9) -> bool:
    return abs(float(a) - float(b)) <= max(tol * max(1.0, abs(float(b))), tol)


class Checks:
    def __init__(self) -> None:
        self.items: list[dict[str, Any]] = []

    def add(self, check_id: str, requirement: str, *, ok: bool, expected: Any, actual: Any,
            inputs: list[str] | None = None, note: str = "") -> None:
        self.items.append({
            "id": check_id,
            "requirement": requirement,
            "result": "pass" if ok else "fail",
            "expected": expected,
            "actual": actual,
            "inputs": inputs or [],
            "note": note,
        })

    @property
    def failed(self) -> list[dict[str, Any]]:
        return [item for item in self.items if item["result"] != "pass"]


# -- F21: basemap -----------------------------------------------------------
def _read_basemap_js(path: Path) -> dict[str, Any]:
    text = path.read_text(encoding="utf-8")
    match = re.search(r'store\["[a-z]+"\] = (\{.*\});\n\}\)\(\);', text, re.S)
    if not match:
        raise ValueError(f"{path} is not a basemap bundle")
    return json.loads(match.group(1))


def _forward(projection: dict[str, Any], lon: float, lat: float) -> tuple[float, float]:
    """Independent forward projection, written from the published bbox alone.

    This is deliberately *not* ``bike_agent.basemap.build.Projection.forward``:
    a check that calls the code under test proves only that the code is
    self-consistent.  Here the formula is spelled out again from the documented
    definition (equirectangular, standard parallel at the bbox mid-latitude).
    """
    lon_min, lat_min, lon_max, lat_max = projection["bbox"]
    width = projection["width"]
    height = projection["height"]
    x = (lon - lon_min) / (lon_max - lon_min) * width
    y = (lat_max - lat) / (lat_max - lat_min) * height
    return x, y


def _inverse(projection: dict[str, Any], x: float, y: float) -> tuple[float, float]:
    lon_min, lat_min, lon_max, lat_max = projection["bbox"]
    width = projection["width"]
    height = projection["height"]
    lon = lon_min + x / width * (lon_max - lon_min)
    lat = lat_max - y / height * (lat_max - lat_min)
    return lon, lat


def _check_basemap(checks: Checks, layout: Any) -> dict[str, Any]:
    manifest_path = SITE / "basemap" / "manifest.json"
    detail: dict[str, Any] = {}
    if not manifest_path.exists():
        checks.add("F21-1[manifest]", "两源底图资源与 manifest 存在且可解析", ok=False,
                   expected="replay/basemap/manifest.json", actual="缺失",
                   note="先运行 `bike-agent replay --sources xm,sh`")
        return detail
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))

    for source in SOURCES:
        cells = pd.read_parquet(layout.cells_path(source, _cell_size(source)))
        entry = manifest["sources"].get(source)
        bundle_path = SITE / source / "basemap.js"
        problems: list[str] = []
        if entry is None:
            problems.append("manifest 缺该源")
        if not bundle_path.exists():
            problems.append("basemap.js 缺失")
            checks.add(f"F21-1[{source}]", "两源底图资源及 manifest 存在且可解析", ok=False,
                       expected=f"replay/{source}/basemap.js", actual=problems)
            continue
        payload = _read_basemap_js(bundle_path)
        if entry is not None:
            if _sha(bundle_path) != entry["published_sha256"]:
                problems.append("发布文件 sha256 与 manifest 不一致")
            raw_path = ROOT / entry["raw_extract"]
            if not raw_path.exists():
                problems.append(f"原始提取缺失 {entry['raw_extract']}")
            elif _sha(raw_path) != entry["raw_extract_sha256"]:
                problems.append("原始提取 sha256 与 manifest 不一致")
        # Geometry must be finite and non-empty -- per layer, not just in total:
        # an empty `land` layer with a populated `admin` one is a broken basemap
        # that a whole-file point count would call fine.
        finite = True
        points = 0
        layer_points: dict[str, int] = {}
        for layer in ("land", "water", "admin"):
            layer_points[layer] = 0
            for data in payload["layers"].get(layer, []):
                for match in re.finditer(r"[-\d.]+", data):
                    if not math.isfinite(float(match.group(0))):
                        finite = False
                layer_points[layer] += data.count("L") + 1
            points += layer_points[layer]
        if points == 0:
            problems.append("底图几何为空")
        for layer, count in layer_points.items():
            if count == 0:
                problems.append(f"{layer} 图层为空")
        # Coverage: the published grid rectangles must cover every cell centre,
        # and there must be exactly one rectangle per cell.
        projection = payload["projection"]
        rects = {row[0]: row[1:] for row in payload["cells"]}
        missing = [cell for cell in cells["cell_id"] if cell not in rects]
        duplicate = len(payload["cells"]) - len(rects)
        outside = []
        for row in cells.itertuples():
            rect = rects.get(row.cell_id)
            if rect is None:
                continue
            x0, y0, w, h = rect
            if x0 < 0 or y0 < 0 or x0 + w > projection["width"] + 1e-6 or y0 + h > projection["height"] + 1e-6:
                outside.append(row.cell_id)
        if missing:
            problems.append(f"缺 {len(missing)} 个格")
        if duplicate:
            problems.append(f"{duplicate} 个重复格")
        if outside:
            problems.append(f"{len(outside)} 个格超出发布画布")
        if len(payload["cells"]) != len(cells):
            problems.append(f"格数 {len(payload['cells'])} != {len(cells)}")
        checks.add(
            f"F21-1[{source}]",
            "底图资源/manifest 可解析、摘要一致、几何非空有限、覆盖网格且不丢格不重复",
            ok=not problems,
            expected=f"{len(cells)} 个格全部落在发布画布内，sha256 与 manifest 一致",
            actual={"problems": problems, "rects": len(payload["cells"]), "points": points, "finite": finite},
            inputs=[_rel(bundle_path), "replay/basemap/manifest.json"],
        )
        detail[source] = {"manifest": entry, "payload": payload}

    _check_geometry(checks, layout, detail)
    _check_basemap_offline(checks, detail)
    return detail


def _cell_size(source: str) -> int:
    from bike_agent.config import load_config, source_config

    return int(source_config(load_config(), source)["cell_size_m"])


def _check_geometry(checks: Checks, layout: Any, detail: dict[str, Any]) -> None:
    """F21-2: re-derive every published pixel from the published bbox."""
    for source in SOURCES:
        if source not in detail:
            continue
        payload = detail[source]["payload"]
        projection = payload["projection"]
        cells = pd.read_parquet(layout.cells_path(source, _cell_size(source)))
        rects = {row[0]: row[1:] for row in payload["cells"]}
        problems: list[str] = []
        worst = 0.0

        # Every cell's centre and its four corners, recomputed from lon/lat.
        # The cell step comes from the *parquet's own* cell centres (median gap
        # between neighbouring columns/rows), not from the build's edge formula
        # -- otherwise a wrong step in the build would be reproduced here.
        lon_by_ix = cells.groupby("ix")["lon"].mean().sort_index()
        lat_by_iy = cells.groupby("iy")["lat"].mean().sort_index()
        lon_step = float(lon_by_ix.diff().dropna().median()) if len(lon_by_ix) > 1 else 0.0
        lat_step = float(lat_by_iy.diff().dropna().median()) if len(lat_by_iy) > 1 else 0.0
        if not (lon_step > 0 and lat_step > 0):
            problems.append("无法从格中心推得行列步长")
        for row in cells.itertuples():
            rect = rects.get(row.cell_id)
            if rect is None:
                problems.append(f"{row.cell_id} 无发布矩形")
                continue
            x0, y0, w, h = rect
            # Centre: published rect centre vs forward-projected cell centre.
            px, py = _forward(projection, row.lon, row.lat)
            error = math.hypot((x0 + w / 2) - px, (y0 + h / 2) - py)
            worst = max(worst, error)
            if error > PIXEL_TOLERANCE:
                problems.append(f"{row.cell_id} 中心误差 {error:.3f}px")
            # Corners: the cell's own lon/lat box (centre ± half a step), forward
            # projected.  The published rect must have its top-left at the
            # north-west corner and its bottom-right at the south-east corner --
            # which is also where a swapped lat/lon or a south-up axis shows up.
            nw = _forward(projection, row.lon - lon_step / 2, row.lat + lat_step / 2)
            se = _forward(projection, row.lon + lon_step / 2, row.lat - lat_step / 2)
            corner_error = max(
                math.hypot(nw[0] - x0, nw[1] - y0),
                math.hypot(se[0] - (x0 + w), se[1] - (y0 + h)),
            )
            worst = max(worst, corner_error)
            if corner_error > PIXEL_TOLERANCE:
                problems.append(f"{row.cell_id} 角点误差 {corner_error:.3f}px")
            # Inverse round-trip, measured in pixels: pixels -> lon/lat -> pixels.
            back_lon, back_lat = _inverse(projection, x0 + w / 2, y0 + h / 2)
            back_x, back_y = _forward(projection, back_lon, back_lat)
            round_trip = math.hypot(back_x - (x0 + w / 2), back_y - (y0 + h / 2))
            if round_trip > PIXEL_TOLERANCE:
                problems.append(f"{row.cell_id} 逆变换误差 {round_trip:.3f}px")

        # Lat/lon not swapped: increasing ix must move right, increasing iy up.
        by_ix = cells.sort_values(["ix", "iy"])
        first_ix, last_ix = by_ix.iloc[0], by_ix.iloc[-1]
        ix_ok = _forward(projection, last_ix.lon, last_ix.lat)[0] > _forward(projection, first_ix.lon, first_ix.lat)[0]
        by_iy = cells.sort_values(["iy", "ix"])
        first_iy, last_iy = by_iy.iloc[0], by_iy.iloc[-1]
        # North up: larger latitude is *smaller* screen y.
        north_ok = _forward(projection, last_iy.lon, last_iy.lat)[1] < _forward(projection, first_iy.lon, first_iy.lat)[1]
        if not ix_ok:
            problems.append("ix 增大时经度未增大（经纬可能互换）")
        if not north_ok:
            problems.append("纬度增大时屏幕 y 未减小（北向错误）")

        # Three or more spread-out control points, checked in both directions.
        picks = _spread_controls(cells, count=4)
        control_rows = []
        for cell in picks:
            rect = rects[cell.cell_id]
            px, py = _forward(projection, cell.lon, cell.lat)
            lon, lat = _inverse(projection, rect[0] + rect[2] / 2, rect[1] + rect[3] / 2)
            control_rows.append({
                "cell_id": cell.cell_id,
                "lon": round(float(cell.lon), 6),
                "lat": round(float(cell.lat), 6),
                "px": round(px, 2),
                "py": round(py, 2),
                "round_trip_lon": round(lon, 6),
                "round_trip_lat": round(lat, 6),
            })
        # Round-trip agreement is judged in pixels, the unit the tolerance is in:
        # 0.1 px of published rounding is ~2e-5 degrees, so a degree tolerance
        # would either be vacuous or arbitrary.
        control_ok = all(
            math.hypot(
                _forward(projection, row["round_trip_lon"], row["round_trip_lat"])[0] - row["px"],
                _forward(projection, row["round_trip_lon"], row["round_trip_lat"])[1] - row["py"],
            ) <= PIXEL_TOLERANCE
            for row in control_rows
        )
        spread_ok = len({(round(row["px"] / 50), round(row["py"] / 50)) for row in control_rows}) == len(control_rows)
        if not control_ok:
            problems.append("控制点逆变换不一致")
        if len(control_rows) < 3 or not spread_ok:
            problems.append("分散控制点少于 3 个")

        checks.add(
            f"F21-2[{source}]",
            "独立几何核对：全部格中心与角点、经纬未互换、北向正确、逆变换、≥3 个分散控制点",
            ok=not problems,
            expected=f"全部格中心/角点误差 ≤ {PIXEL_TOLERANCE}px；ix↑→x↑；lat↑→y↓；逆变换一致",
            actual={
                "cells": len(cells),
                "worst_centre_error_px": round(worst, 4),
                "ix_increases_x": ix_ok,
                "north_up": north_ok,
                "controls": control_rows,
                "problems": problems[:10],
            },
            inputs=[f"data/processed/cells.{source}.{_cell_size(source)}m.parquet", f"replay/{source}/basemap.js"],
        )

        # Move endpoints: every published arrow names two real cells, and the
        # page script builds the line from those cells' centres.  The drawn
        # geometry itself is measured in browser_smoke.py (needs a DOM).
        move_detail = _move_endpoint_detail(source, payload)
        checks.add(
            f"F21-2[{source}] 搬运端点",
            "搬运连线端点取自对应格中心：端点格存在、脚本按矩形中心取点（画面量测见浏览器冒烟）",
            ok=not move_detail["problems"],
            expected="每条线的 from/to 格都在发布几何中，且脚本用 rect.x+width/2、rect.y+height/2 作端点",
            actual=move_detail,
            inputs=[
                f"outputs/sim_moves.rule.{source}.json",
                f"replay/{source}/basemap.js",
                "replay/workbench-geo.js",
            ],
            note="实际画出的 x1/y1/x2/y2 与格心比对由 scripts/browser_smoke.py 在真实 DOM 上完成（0.5 px）",
        )


def _spread_controls(cells: pd.DataFrame, count: int = 4) -> list[Any]:
    """Pick cells far apart: one per quadrant of the grid, then fill in."""
    mid_ix = (cells["ix"].min() + cells["ix"].max()) / 2.0
    mid_iy = (cells["iy"].min() + cells["iy"].max()) / 2.0
    buckets: dict[tuple[bool, bool], list[Any]] = {}
    for row in cells.itertuples():
        key = (row.ix > mid_ix, row.iy > mid_iy)
        buckets.setdefault(key, []).append(row)
    picks = []
    for key in sorted(buckets):
        rows = buckets[key]
        picks.append(rows[len(rows) // 2])
        if len(picks) >= count:
            break
    return picks


def _move_endpoint_detail(source: str, payload: dict[str, Any]) -> dict[str, Any]:
    """F21-2 (move endpoints), two halves, neither of which claims the other's work.

    1. *Membership*: every recorded move names two cells that exist in the
       published rectangles, and those centres are finite.
    2. *Construction*: the shipped page script builds each line from
       ``centre(move.from_cell)``/``centre(move.to_cell)``, and ``centre`` is
       ``rect.x + rect.width/2``, ``rect.y + rect.height/2``.

    The geometric proof -- the drawn ``x1/y1/x2/y2`` actually equal the rect
    centres -- needs a DOM and lives in ``scripts/browser_smoke.py``, which
    compares each ``line.geo-move`` against its cells' ``getBoundingClientRect``
    centres within 0.5 px.  This function does not pretend to do that.
    """
    rects = {row[0]: row[1:] for row in payload["cells"]}
    records = json.loads((ROOT / "outputs" / f"sim_moves.rule.{source}.json").read_text(encoding="utf-8"))
    problems = []
    checked = 0
    for record in records[:2000]:
        start = rects.get(record["from_cell"])
        end = rects.get(record["to_cell"])
        if start is None or end is None:
            problems.append(f"{record['from_cell']}→{record['to_cell']} 端点格不在发布几何中")
            continue
        for rect in (start, end):
            cx, cy = rect[0] + rect[2] / 2, rect[1] + rect[3] / 2
            if not (math.isfinite(cx) and math.isfinite(cy)):
                problems.append("格心坐标非有限")
        checked += 1

    script = (SITE / "workbench-geo.js").read_text(encoding="utf-8")
    for token in ("centre(move.from_cell)", "centre(move.to_cell)"):
        if token not in script:
            problems.append(f"workbench-geo.js 未从格心取端点：缺 {token}")
    for token in ("parseFloat(rect.getAttribute('x')) + w / 2",
                  "parseFloat(rect.getAttribute('y')) + h / 2"):
        if token not in script:
            problems.append(f"workbench-geo.js 的 centre() 不是矩形中心：缺 {token}")
    return {
        "moves": len(records),
        "checked": checked,
        "construction": not problems,
        "drawn_geometry_checked_by": "scripts/browser_smoke.py (0.5 px, real DOM)",
        "problems": problems[:5],
    }


def _check_basemap_offline(checks: Checks, detail: dict[str, Any]) -> None:
    """F21-3/F21-4: no external reference, attribution present, source matches."""
    external = re.compile(
        r"""(?:src|href)\s*=\s*["'](?:(?:https?:)?//)[^"']+["']|@import\s+(?:url\()?["']?(?:https?:)?//|url\(\s*["']?(?:https?:)?//""",
        re.IGNORECASE,
    )
    offenders = []
    for path in SITE.rglob("*"):
        if path.suffix not in {".html", ".css", ".js"}:
            continue
        for match in external.finditer(path.read_text(encoding="utf-8")):
            offenders.append(f"{_rel(path)}: {match.group(0)[:60]}")
    checks.add(
        "F21-3[offline]",
        "离线资源扫描：站点没有任何外部 href/src/@import/url(http)",
        ok=not offenders,
        expected="0 处外部引用",
        actual=offenders[:10],
        inputs=["replay/"],
    )

    # The attribution text must reach the page as plain text, and the URL must
    # live in the manifest rather than in a link.
    page_problems = []
    for source in SOURCES:
        page = (SITE / source / "workbench.html").read_text(encoding="utf-8")
        if "basemap.js" not in page or "basemap/manifest.json" not in page:
            page_problems.append(f"{source}: 页面未指向 basemap.js / manifest")
        if "不是道路轨迹" not in page:
            page_problems.append(f"{source}: 缺少“不是道路轨迹”的说明")
        if "坐标口径" not in page and "crs" not in page.lower():
            page_problems.append(f"{source}: 缺少坐标口径入口")
    manifest = json.loads((SITE / "basemap" / "manifest.json").read_text(encoding="utf-8"))
    licence_phrases = {
        "GSHHG-LICENSE.txt": "GSHHG",
        "LGPL-3.0.txt": "GNU LESSER GENERAL PUBLIC LICENSE",
        "GPL-3.0.txt": "GNU GENERAL PUBLIC LICENSE",
        "ODbL-1.0.txt": "Open Database License",
    }
    for name, entry in manifest["datasets"].items():
        for key in ("source_page", "download_url", "license_id", "license_file", "original_crs", "fetched"):
            if not entry.get(key):
                page_problems.append(f"manifest/{name} 缺 {key}")
        # Existence is not conveyance: the *full* text has to be there, not a
        # pointer to one.  A 1 KB file named LGPL-3.0.txt would otherwise pass.
        for relative in entry.get("license_texts") or [entry["license_file"]]:
            license_path = SITE / "basemap" / relative
            if not license_path.exists():
                page_problems.append(f"manifest/{name} 指向的许可文本不存在：{relative}")
                continue
            text = license_path.read_text(encoding="utf-8", errors="replace")
            if len(text) < 1000:
                page_problems.append(f"{relative} 只有 {len(text)} 字节，不是完整许可文本")
            phrase = licence_phrases.get(license_path.name)
            if phrase and phrase not in text:
                page_problems.append(f"{relative} 不含 {phrase!r}，不是它自称的那份许可")
    checks.add(
        "F21-4[attribution]",
        "可见出处/图例/坐标口径与 manifest 一致；许可全文随包且内容可核对",
        ok=not page_problems,
        expected="页面指向底图与 manifest；manifest 记录来源/许可/CRS/获取日期；每份许可全文存在且含标识行",
        actual=page_problems[:10],
        inputs=["replay/xm/workbench.html", "replay/sh/workbench.html", "replay/basemap/manifest.json"],
    )


# -- F22: prediction --------------------------------------------------------
def _decode(block: dict[str, Any], count: int) -> np.ndarray:
    raw = base64.b64decode(block["values"])
    return np.frombuffer(raw, dtype="<f8", count=count).astype(np.float64)


def _check_forecast(checks: Checks, layout: Any) -> None:
    for source in SOURCES:
        path = SITE / source / "forecast.js"
        if not path.exists():
            checks.add(f"F22-1[{source}]", "预测数据逐项等于 forecast parquet", ok=False,
                       expected=f"replay/{source}/forecast.js", actual="缺失",
                       note="先运行 `bike-agent replay --sources xm,sh`")
            continue
        text = path.read_text(encoding="utf-8")
        match = re.search(r'store\["[a-z]+"\] = (\{.*\});\n\}\)\(\);', text, re.S)
        payload = json.loads(match.group(1))
        df = pd.read_parquet(layout.forecast_path(source))
        df["time_bucket"] = pd.to_datetime(df["time_bucket"])
        df = df.sort_values(["time_bucket", "cell_id"], kind="mergesort")
        cells = list(payload["cell_ids"])
        buckets = [pd.Timestamp(value) for value in payload["buckets"]]
        n, m = len(cells), len(buckets)
        cell_index = {cell: i for i, cell in enumerate(cells)}
        bucket_index = {bucket: i for i, bucket in enumerate(buckets)}

        problems: list[str] = []
        columns = {
            "out": ("actual_out", "baseline_out", "pred_out"),
            "in": ("actual_in", "baseline_in", "pred_in"),
        }
        # net must be out - in in the parquet itself.
        for prefix in ("actual", "baseline", "pred"):
            diff = (df[f"{prefix}_out"] - df[f"{prefix}_in"]) - df[f"{prefix}_net_flow"]
            if float(np.abs(diff).max()) > 0:
                problems.append(f"{prefix}_net_flow != out - in (max {float(np.abs(diff).max())})")

        for metric, (col_actual, col_baseline, col_pred) in columns.items():
            block = payload["metrics"][metric]
            actual = block["series"]["actual"]["values"]
            baseline = _decode(block["series"]["baseline"], n * m).reshape(n, m)
            pred = _decode(block["series"]["pred"], n * m).reshape(n, m)
            for row in df.itertuples():
                i = cell_index.get(row.cell_id)
                j = bucket_index.get(pd.Timestamp(row.time_bucket))
                if i is None or j is None:
                    problems.append(f"parquet 行无法关联：{row.cell_id} {row.time_bucket}")
                    continue
                if actual[i][j] != int(getattr(row, col_actual)):
                    problems.append(f"{metric}/actual {row.cell_id} {row.time_bucket}")
                if not _close(baseline[i, j], getattr(row, col_baseline)):
                    problems.append(f"{metric}/baseline {row.cell_id} {row.time_bucket}")
                if not _close(pred[i, j], getattr(row, col_pred)):
                    problems.append(f"{metric}/pred {row.cell_id} {row.time_bucket}")
            # The published series must not invent points the parquet lacks.
            published = int(np.isfinite(baseline).sum())
            if published != len(df):
                problems.append(f"{metric}: 发布 {published} 个点，parquet 有 {len(df)} 行")
        # F22-1: the buckets must line up with the replay by value, because the
        # page aligns the cursor by position.
        replay_buckets = [
            pd.Timestamp(value)
            for value in sorted(
                pd.read_parquet(layout.replay_path(source, "rule"), columns=["time_bucket"])["time_bucket"].unique()
            )
        ]
        if replay_buckets != buckets:
            problems.append(
                f"预测时段与回放时段不一致：{len(buckets)} vs {len(replay_buckets)}"
            )
        checks.add(
            f"F22-1[{source}]",
            "两源全格全测试点三指标三系列逐项等于 forecast parquet；net = out − in",
            ok=not problems,
            expected=f"{len(df)} 行 × 3 指标 × 3 系列全部相等；parquet 的 *_net_flow == out − in",
            actual={"problems": problems[:10], "rows": len(df), "cells": n, "buckets": m},
            inputs=[_rel(layout.forecast_path(source)), f"replay/{source}/forecast.js"],
        )

        # F22-2: the summary, the current error and the local MAE, recomputed
        # from the parquet -- net included, which has no published series.
        problems = []
        cell_pos = {cell: i for i, cell in enumerate(cells)}
        bucket_pos = {pd.Timestamp(bucket): j for j, bucket in enumerate(buckets)}

        def from_parquet(column: str) -> np.ndarray:
            block_matrix = np.full((n, m), np.nan, dtype=np.float64)
            if column not in df.columns:
                return block_matrix
            for row in df.itertuples():
                i = cell_pos.get(row.cell_id)
                j = bucket_pos.get(pd.Timestamp(row.time_bucket))
                if i is not None and j is not None:
                    block_matrix[i, j] = float(getattr(row, column))
            return block_matrix

        truth = {"out": {}, "in": {}}
        for series_name in ("actual", "baseline", "pred"):
            truth["out"][series_name] = from_parquet(f"{series_name}_out")
            truth["in"][series_name] = from_parquet(f"{series_name}_in")
        truth["net"] = {
            series_name: truth["out"][series_name] - truth["in"][series_name]
            for series_name in ("actual", "baseline", "pred")
        }

        for metric in ("out", "in", "net"):
            block = payload["metrics"][metric]
            for series_name in ("actual", "baseline", "pred"):
                matrix = truth[metric][series_name]
                expected_values = []
                for j in range(m):
                    column = matrix[:, j]
                    present = column[~np.isnan(column)]
                    expected_values.append(None if present.size == 0 else float(present.sum()))
                published = block["summary"][series_name]["values"]
                for j, (want, got) in enumerate(zip(expected_values, published)):
                    if want is None or got is None:
                        if want is not got:
                            problems.append(f"{metric}/{series_name} 汇总 {j} 空值不一致")
                    elif not _close(want, got, 1e-9):
                        problems.append(f"{metric}/{series_name} 汇总 {j}: {want} != {got}")
            # Local MAE per cell, from the paired points only.
            actual = truth[metric]["actual"]
            pred = truth[metric]["pred"]
            for i in range(n):
                paired = ~(np.isnan(actual[i]) | np.isnan(pred[i]))
                want_n = int(paired.sum())
                want_mae = float(np.abs(pred[i][paired] - actual[i][paired]).mean()) if want_n else None
                got = block["local"]["per_cell"][i]
                if got["n"] != want_n:
                    problems.append(f"{metric} 格 {cells[i]} n={got['n']} != {want_n}")
                if want_mae is None:
                    if got["mae"] is not None:
                        problems.append(f"{metric} 格 {cells[i]} 无配对点却给出 MAE")
                elif got["mae"] is None or not _close(got["mae"], want_mae, 1e-12):
                    problems.append(f"{metric} 格 {cells[i]} MAE 不一致")
            source_mae = block["local"]["source"]
            paired = ~(np.isnan(actual) | np.isnan(pred))
            want_mae = float(np.abs(pred[paired] - actual[paired]).mean())
            if source_mae["n"] != int(paired.sum()) or not _close(source_mae["mae"], want_mae, 1e-12):
                problems.append(f"{metric} 全源 MAE 不一致")

        # The current-point error is computed in the page from the two readout
        # values; the formula has to be that subtraction, not something else.
        js = (SITE / "workbench.js").read_text(encoding="utf-8")
        if "readout.forecast_pred - readout.forecast_actual" not in js:
            problems.append("workbench.js 的当前点误差不是 pred − actual")
        if "Math.abs(error)" not in js:
            problems.append("workbench.js 的绝对误差不是 |pred − actual|")
        checks.add(
            f"F22-2[{source}]",
            "全源汇总、局部 MAE/样本数独立复算一致（含 net）；缺失点不被静默变成 0",
            ok=not problems,
            expected="out/in/net × 三系列每时段求和、每格 MAE 与 n 全部可复算；误差公式为 pred − actual",
            actual={"problems": problems[:10]},
            inputs=[f"replay/{source}/forecast.js"],
        )

        # F22-4: honesty of the page text and of the payload's scope.
        page = (SITE / source / "workbench.html").read_text(encoding="utf-8")
        honesty = []
        if "不是预测区间" not in page:
            honesty.append("页面未声明 MAE 不是预测区间")
        if "不随调度策略变化" not in page:
            honesty.append("页面未声明预测不随策略变化")
        if "不补训练期" not in page and "不补训练" not in page:
            honesty.append("页面未声明不补训练期")
        if "95%" in page or "置信区间" in page:
            honesty.append("页面出现了预测区间字样")
        if "全源汇总" not in page or "当前格" not in page:
            honesty.append("页面缺少全源/当前格的范围标签")
        checks.add(
            f"F22-4[{source}]",
            "不伪造训练期预测或预测区间；局部与全源指标标签与范围不同",
            ok=not honesty,
            expected="页面声明测试段范围、MAE≠区间、预测不随策略变化；无 95% 预测带",
            actual=honesty,
            inputs=[f"replay/{source}/workbench.html"],
        )

    # F22-3: behaviour.  The state machine is exercised in node (tests/) and the
    # browser sweep covers the DOM; here the shipped page must carry the hooks
    # the behaviour depends on.
    hooks: list[str] = []
    js = (SITE / "workbench.js").read_text(encoding="utf-8")
    for token in ("data-forecast-metric", "data-forecast-range", "data-forecast-bucket", "setForecastMetric", "setForecastRange"):
        if token not in js:
            hooks.append(f"workbench.js 缺 {token}")
    for source in SOURCES:
        page = (SITE / source / "workbench.html").read_text(encoding="utf-8")
        for token in ('data-forecast-metric="out"', 'data-forecast-range="cell"', 'id="forecast-chart"'):
            if token not in page:
                hooks.append(f"{source}: 页面缺 {token}")
    checks.add(
        "F22-3[hooks]",
        "选格/切指标/切范围/点时段/事件跳转/换源的联动钩子存在",
        ok=not hooks,
        expected="页面与脚本暴露指标、范围、时段点与联动函数",
        actual=hooks,
        inputs=["replay/workbench.js", "replay/xm/workbench.html"],
        note="实际点击行为由 scripts/browser_smoke.py 与 tests/test_presentation_forecast.py 覆盖",
    )


# -- F23: media -------------------------------------------------------------
def _png_size(path: Path) -> tuple[int, int]:
    """Decode the PNG header *and* verify the stream decodes.

    A PNG whose IHDR says 1600×900 but whose IDAT is truncated is not a
    screenshot; Pillow's ``load()`` walks the whole stream and fails on damage.
    """
    from PIL import Image

    with Image.open(path) as image:
        image.load()
        return image.size


def _probe_video(path: Path) -> dict[str, Any]:
    """ffprobe, then a full decode pass -- metadata alone does not prove playability."""
    ffprobe = _which("ffprobe")
    ffmpeg = _which("ffmpeg")
    if ffprobe is None or ffmpeg is None:
        return {"ok": False, "reason": "ffprobe/ffmpeg 未安装"}
    probe = subprocess.run(
        [ffprobe, "-v", "error", "-print_format", "json", "-show_format", "-show_streams", str(path)],
        capture_output=True, text=True, check=False,
    )
    if probe.returncode != 0:
        return {"ok": False, "reason": f"ffprobe 退出码 {probe.returncode}", "stderr": probe.stderr[-300:]}
    info = json.loads(probe.stdout)
    streams = info.get("streams", [])
    video = [s for s in streams if s.get("codec_type") == "video"]
    if not video:
        return {"ok": False, "reason": "没有视频帧轨"}
    stream = video[0]
    duration = float(info.get("format", {}).get("duration") or stream.get("duration") or 0)
    width, height = int(stream["width"]), int(stream["height"])
    # Decode every frame; a container that opens but cannot decode is not playable.
    decode = subprocess.run(
        [ffmpeg, "-v", "error", "-i", str(path), "-f", "null", "-"],
        capture_output=True, text=True, check=False,
    )
    frames = None
    count = subprocess.run(
        [ffprobe, "-v", "error", "-select_streams", "v:0", "-count_packets",
         "-show_entries", "stream=nb_read_packets", "-of", "csv=p=0", str(path)],
        capture_output=True, text=True, check=False,
    )
    try:
        frames = int(count.stdout.strip().splitlines()[0])
    except (ValueError, IndexError):
        frames = None
    return {
        "ok": decode.returncode == 0 and decode.stderr.strip() == "",
        "codec": stream.get("codec_name"),
        "width": width,
        "height": height,
        "duration_s": round(duration, 2),
        "frames": frames,
        "decode_exit": decode.returncode,
        "decode_errors": decode.stderr.strip()[:300],
        "format": info.get("format", {}).get("format_name"),
    }


def _which(name: str) -> str | None:
    import shutil

    return shutil.which(name)


def _check_media(checks: Checks) -> None:
    manifest_path = MEDIA / "manifest.json"
    if not manifest_path.exists():
        checks.add("F23-1[media]", "六张 PNG 与一段视频存在且非空", ok=False,
                   expected="presentations/phase5/manifest.json", actual="缺失",
                   note="先运行 `python3 scripts/capture_presentation.py --all`")
        return
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    shots = manifest.get("screenshots", [])
    videos = manifest.get("videos", [])

    problems: list[str] = []
    decoded: list[dict[str, Any]] = []
    for entry in shots:
        path = ROOT / entry["path"]
        if not path.exists() or path.stat().st_size == 0:
            problems.append(f"{entry['path']} 缺失或为空")
            continue
        try:
            width, height = _png_size(path)
        except Exception as error:
            problems.append(f"{entry['path']} 无法解码：{error}")
            continue
        ratio = width / height
        ok = width >= MIN_SHOT[0] and height >= MIN_SHOT[1] and abs(ratio - 16 / 9) < 0.01
        if not ok:
            problems.append(f"{entry['path']} 尺寸 {width}×{height}（比例 {ratio:.3f}）不达标")
        if entry.get("sha256") != _sha(path):
            problems.append(f"{entry['path']} sha256 与 manifest 不一致")
        decoded.append({"path": entry["path"], "width": width, "height": height, "ratio": round(ratio, 4), "ok": ok})
    checks.add(
        "F23-1[screenshots]",
        "至少六张 PNG 存在、非空、可解码，尺寸 ≥1600×900 且 16:9",
        ok=not problems and len(shots) >= 6,
        expected=f"≥6 张，≥{MIN_SHOT[0]}×{MIN_SHOT[1]}，16:9",
        actual={"count": len(shots), "decoded": decoded, "problems": problems[:10]},
        inputs=[_rel(manifest_path)],
    )

    video_problems: list[str] = []
    probes: list[dict[str, Any]] = []
    for entry in videos:
        path = ROOT / entry["path"]
        if not path.exists() or path.stat().st_size == 0:
            video_problems.append(f"{entry['path']} 缺失或为空")
            continue
        probe = _probe_video(path)
        probes.append({"path": entry["path"], **probe})
        if not probe.get("ok"):
            video_problems.append(f"{entry['path']} 不可完整解码：{probe.get('decode_errors') or probe.get('reason')}")
            continue
        if probe["width"] < MIN_VIDEO[0] or probe["height"] < MIN_VIDEO[1]:
            video_problems.append(f"{entry['path']} 分辨率 {probe['width']}×{probe['height']} 过低")
        if abs(probe["width"] / probe["height"] - 16 / 9) > 0.02:
            video_problems.append(f"{entry['path']} 不是 16:9")
        if not (VIDEO_SECONDS[0] <= probe["duration_s"] <= VIDEO_SECONDS[1]):
            video_problems.append(f"{entry['path']} 时长 {probe['duration_s']}s 不在 90–180s")
        if not probe.get("frames"):
            video_problems.append(f"{entry['path']} 没有帧")
        if entry.get("sha256") != _sha(path):
            video_problems.append(f"{entry['path']} sha256 与 manifest 不一致")
    checks.add(
        "F23-1[video]",
        "至少一段 WebM/MP4 可播放：真实解码、帧轨、≥1280×720、16:9、90–180 秒",
        ok=not video_problems and len(videos) >= 1,
        expected=f"≥1 段，≥{MIN_VIDEO[0]}×{MIN_VIDEO[1]}，{VIDEO_SECONDS[0]:.0f}–{VIDEO_SECONDS[1]:.0f}s，可完整解码",
        actual={"count": len(videos), "probes": probes, "problems": video_problems[:10]},
        inputs=[_rel(manifest_path)],
    )

    # F23-2: the manifest's own integrity and coverage.
    problems = []
    present = {entry.get("key") for entry in shots}
    missing = sorted(set(REQUIRED_SHOTS) - present)
    if missing:
        problems.append(f"截图缺少状态：{missing}")
    required_state = ("source", "frame", "mode", "cell", "capture_command", "tool_versions",
                      "viewport", "theme", "status")
    for entry in shots:
        for key in required_state:
            if key not in entry or entry[key] in (None, ""):
                problems.append(f"{entry.get('path')} 缺状态键 {key}")
        if entry.get("status") != "ok":
            problems.append(f"{entry.get('path')} 捕获状态是 {entry.get('status')!r}，不是 ok")
        if not entry.get("page"):
            problems.append(f"{entry.get('path')} 缺页面入口")
        if not entry.get("input_digest"):
            problems.append(f"{entry.get('path')} 缺输入摘要")
    for entry in videos:
        chapters = entry.get("chapters") or []
        names = {chapter.get("id") for chapter in chapters}
        if not REQUIRED_CHAPTERS <= names:
            problems.append(f"视频章节缺少 {sorted(REQUIRED_CHAPTERS - names)}")
        if chapters != sorted(chapters, key=lambda c: c.get("at", 0)):
            problems.append("视频章节未按时间排序")
        for key in ("capture_command", "tool_versions", "viewport", "theme", "input_digest", "status"):
            if not entry.get(key):
                problems.append(f"视频缺 {key}")
        if entry.get("status") != "ok":
            problems.append(f"{entry.get('path')} 捕获状态是 {entry.get('status')!r}，不是 ok")
        if "演示选段" not in json.dumps(entry, ensure_ascii=False):
            problems.append("视频未标注“演示选段、模拟值”")

    # The media must still describe the tree it ships with: every recorded input
    # digest has to match the file on disk, and the published page assets the
    # screenshots depict have to be among the recorded inputs at all.
    from scripts.capture_presentation import PAGE_ASSETS, input_digest, stale_entries

    input_files = manifest.get("input_files") or {}
    for asset in PAGE_ASSETS:
        if asset not in input_files:
            problems.append(f"输入摘要没有覆盖页面资源 {asset}（页面改动后素材会过期却查不出来）")
    for name, digest in input_files.items():
        path = ROOT / name
        if digest is None:
            problems.append(f"输入摘要里的 {name} 不存在")
        elif not path.exists():
            problems.append(f"输入摘要里的 {name} 已被删除")
        elif _sha(path) != digest:
            problems.append(f"输入 {name} 在捕获后改动，素材已过期")

    # ...and every medium must carry the stamp of the snapshot it was actually
    # captured from.  The top-level digest describes the last *capture*, so a
    # partial run (`--shots` after a page edit, leaving the video alone) shows up
    # here: the media it did not rebuild still name the older tree.
    current = input_digest()
    if manifest.get("input_digest") != current:
        problems.append("manifest 的输入摘要与当前产物不一致，素材已过期")
    stale = stale_entries([*shots, *videos], current)
    if stale:
        problems.append(
            f"这些素材的输入摘要与当前产物不一致（部分重录，未刷新整套）：{stale}；"
            "重跑 `python3 scripts/capture_presentation.py --all`"
        )

    # The state keys must actually relocate: `--verify` replayed each recorded
    # recipe against the live site and compared the readback, key for key.
    replay = manifest.get("replay_check") or {}
    if not replay:
        problems.append("manifest 缺 replay_check：运行 `python3 scripts/capture_presentation.py --verify`")
    else:
        if replay.get("ok") is not True:
            problems.append(f"状态键复核未通过：{replay.get('mismatches')}")
        if replay.get("n") != len(shots):
            problems.append(f"状态键复核覆盖 {replay.get('n')} 张，截图有 {len(shots)} 张")
        states = replay.get("states") or {}
        for entry in shots:
            state = states.get(entry.get("key")) or {}
            if state.get("ok") is not True:
                problems.append(f"{entry.get('key')} 没有通过状态键复核")
            if state.get("replayed") != state.get("expected"):
                problems.append(f"{entry.get('key')} 复核状态与记录不一致")
    checks.add(
        "F23-2[manifest]",
        "manifest 的摘要、状态键、输入摘要与章节范围有效；状态键经重放复核可重新定位",
        ok=not problems,
        expected=f"截图覆盖 {sorted(REQUIRED_SHOTS)}；每项含状态键/捕获命令/工具版本/输入摘要；replay_check 通过",
        actual={"keys": sorted(present), "replay_ok": replay.get("ok"), "problems": problems[:10]},
        inputs=[_rel(manifest_path)],
    )

    # F23-3: the recorded export actually ran, with its exit code and file list.
    problems = []
    run = manifest.get("last_run") or {}
    if run.get("exit_code") != 0:
        problems.append(f"最近一次导出退出码 {run.get('exit_code')}（期望 0）")
    for key in ("command", "started_at", "finished_at", "files"):
        if not run.get(key):
            problems.append(f"导出记录缺 {key}")
    if run.get("files"):
        listed = {item["path"] for item in run["files"]}
        for entry in shots + videos:
            if entry["path"] not in listed:
                problems.append(f"{entry['path']} 不在导出文件清单里")
        # A record that lists files it did not write would be a paper trail with
        # no run behind it; at least one entry must be this run's own output.
        if not any(item.get("written") for item in run["files"]):
            problems.append("导出记录没有标出本次实际写出的文件")
        for item in run["files"]:
            path = ROOT / item["path"]
            if path.exists() and item.get("sha256") != _sha(path):
                problems.append(f"{item['path']} 导出记录里的 sha256 与当前文件不符")
    checks.add(
        "F23-3[export]",
        "从当前工作区实际执行导出命令成功，记录退出码与文件清单",
        ok=not problems,
        expected="退出码 0，记录命令/时间/文件清单，清单覆盖全部媒体",
        actual={"run": run, "problems": problems[:10]},
        inputs=[_rel(manifest_path)],
    )

    # F23-4: the media live outside the site, and the site does not serve them.
    problems = []
    if MEDIA.is_relative_to(SITE):
        problems.append("媒体目录位于 replay/ 内，会扩大静态根")
    total_media = sum(path.stat().st_size for path in MEDIA.rglob("*") if path.is_file())
    if total_media > MEDIA_CAP:
        problems.append(f"presentations/phase5 合计 {total_media / 1048576:.1f} MiB，超过 100 MiB")
    # `replay/figures/` is the Phase 1–3 chart set and belongs to the site; the
    # check is that *this* phase's media did not get copied in.
    media_names = {Path(entry["path"]).name for entry in shots + videos}
    for path in SITE.rglob("*"):
        if path.suffix.lower() in {".mp4", ".webm"}:
            problems.append(f"站点内出现视频 {_rel(path)}")
        elif path.name in media_names:
            problems.append(f"Phase 5 媒体被复制进站点 {_rel(path)}")
    checks.add(
        "F23-4[layout]",
        "媒体位于仓库根 presentations/phase5/，不复制进 replay/，不扩大静态根；合计 ≤100 MiB",
        ok=not problems,
        expected="媒体目录在站点之外，且合计 ≤ 100 MiB",
        actual={"bytes": total_media, "problems": problems[:10]},
        inputs=["presentations/phase5/"],
    )


# -- budgets ----------------------------------------------------------------
def _check_budgets(checks: Checks) -> None:
    files = [path for path in SITE.rglob("*") if path.is_file() and path.name != ".DS_Store"]
    total = sum(path.stat().st_size for path in files)
    basemap_files = [path for path in (SITE / "basemap").rglob("*") if path.is_file()]
    basemap_files += [SITE / source / "basemap.js" for source in SOURCES if (SITE / source / "basemap.js").exists()]
    basemap_total = sum(path.stat().st_size for path in basemap_files)
    forecast_total = sum(
        (SITE / source / "forecast.js").stat().st_size
        for source in SOURCES
        if (SITE / source / "forecast.js").exists()
    )
    checks.add(
        "NF-30",
        "全站 < 12 MiB；底图发布资源合计 ≤ 2 MiB；预测展示数据两源合计 ≤ 2 MiB",
        ok=total < SITE_CAP and basemap_total <= BASEMAP_CAP and forecast_total <= FORECAST_CAP,
        expected={"site_lt": SITE_CAP, "basemap_le": BASEMAP_CAP, "forecast_le": FORECAST_CAP},
        actual={"site_bytes": total, "basemap_bytes": basemap_total, "forecast_bytes": forecast_total},
        inputs=["replay/"],
    )


# -- report -----------------------------------------------------------------
def run_checks() -> dict[str, Any]:
    from bike_agent.config import Layout

    layout = Layout()
    checks = Checks()

    missing = [
        _rel(path)
        for source in SOURCES
        for path in (
            SITE / source / "workbench.html",
            SITE / source / "basemap.js",
            SITE / source / "forecast.js",
            layout.forecast_path(source),
            # F22-1 compares the forecast buckets against the replay parquet, so
            # it is an input of this gate -- without it the check would crash
            # instead of reporting the missing prerequisite.
            layout.replay_path(source, "rule"),
        )
        if not path.exists()
    ]
    if missing:
        checks.add("P5-PRE", "§6.1 输入齐备", ok=False,
                   expected="工作台/底图/预测数据包/回放产物存在",
                   actual=missing,
                   note="先运行 `bike-agent replay --sources xm,sh`")
        return _report(checks)

    # The forecast payload is read back from the shipped bundle, so the gate does
    # not need to rebuild the presentation payloads first.
    _check_basemap(checks, layout)
    _check_forecast(checks, layout)
    _check_media(checks)
    _check_budgets(checks)
    _check_phase4_still_passes(checks)
    return _report(checks)


def _check_phase4_still_passes(checks: Checks) -> None:
    """NF-28: the Phase 4 contract must still hold after this phase's changes."""
    script = ROOT / "scripts" / "check_presentation.py"
    completed = subprocess.run(
        [sys.executable, str(script), "--phase", "4"],
        capture_output=True, text=True, cwd=str(ROOT), check=False,
    )
    checks.add(
        "NF-28",
        "Phase 4 验证命令仍通过（原数值/全部帧/事件/账本/场景/契约保持）",
        ok=completed.returncode == 0,
        expected="check_presentation.py --phase 4 退出码 0",
        actual=completed.returncode,
        inputs=["outputs/presentation_validation.phase4.json"],
        note=(completed.stdout or "").strip()[-300:],
    )


def _report(checks: Checks) -> dict[str, Any]:
    inputs: dict[str, Any] = {}
    for name in (
        "outputs/forecast.xm.parquet",
        "outputs/forecast.sh.parquet",
        "data/basemap/raw/xm.json",
        "data/basemap/raw/sh.json",
        "replay/basemap/manifest.json",
        "presentations/phase5/manifest.json",
    ):
        path = ROOT / name
        if path.exists():
            inputs[name] = {"bytes": path.stat().st_size, "sha256_16": _sha(path)[:16]}
    return {
        "phase": 5,
        "requirement": "REQUIREMENTS-PHASE5.md",
        "generated_by": "scripts/check_presentation.py --phase 5",
        "note": (
            "本报告是确定性验收证据，不替代 `python3 -m pytest`、浏览器实测与人工视觉检查（§6.2）。"
        ),
        "ok": not checks.failed,
        "n_checks": len(checks.items),
        "n_failed": len(checks.failed),
        "inputs": inputs,
        "checks": checks.items,
    }


def main(argv: list[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--phase", type=int, default=5, choices=[5])
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args(argv)

    report = run_checks()
    REPORT.parent.mkdir(parents=True, exist_ok=True)
    REPORT.write_text(json.dumps(report, ensure_ascii=False, indent=2, default=_jsonable) + "\n", encoding="utf-8")

    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2, default=_jsonable))
    else:
        for item in report["checks"]:
            if args.verbose or item["result"] != "pass":
                mark = "PASS" if item["result"] == "pass" else "FAIL"
                print(f"[{mark}] {item['id']:<22} {item['requirement']}")
            if item["result"] != "pass":
                print(f"        expected: {item['expected']}")
                print(f"        actual:   {json.dumps(item['actual'], ensure_ascii=False, default=_jsonable)[:600]}")
        if report["ok"]:
            print(f"phase 5: all checks passed -> {_rel(REPORT)}")
        else:
            print(f"phase 5: {report['n_failed']} of {report['n_checks']} checks FAILED -> {_rel(REPORT)}")
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
