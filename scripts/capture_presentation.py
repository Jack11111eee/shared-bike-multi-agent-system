#!/usr/bin/env python3
"""F23: capture the presentation screenshots and the demo video from the real site.

Everything this script writes comes from the pages ``bike-agent replay``
generated: it loads them in headless Chrome, drives the real controls, reads the
state back out of the DOM, and only then photographs the result.  Nothing is
composited, retouched or staged: if a control does not exist, the run fails
instead of drawing a box where it should have been.

The output lives in ``presentations/phase5/`` -- outside ``replay/``, so the
static HTTP root does not grow and the media never counts against the site
budget.

Commands::

    python3 scripts/capture_presentation.py --check     # tools + versions, no capture
    python3 scripts/capture_presentation.py --shots     # the six PNGs
    python3 scripts/capture_presentation.py --video     # the demo video
    python3 scripts/capture_presentation.py --all       # both, then refresh the manifest
    python3 scripts/capture_presentation.py --verify    # replay the recorded state keys

Each media entry records the input digest of the tree it was captured from.  A
partial run (``--shots`` / ``--video``) rebuilds only its own media and reports
the rest as stale, exiting non-zero: the delivered set must describe one
snapshot, and ``--all`` is the command that produces it.

Optional dependencies, used only here: Chrome/Chromium, ``websockets`` (for the
DevTools protocol) and ``ffmpeg``/``ffprobe`` (for encoding and verification).
The site, the pipeline and the test suite never import this module.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
SITE = ROOT / "replay"
MEDIA = ROOT / "presentations" / "phase5"
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.cdp import Browser, ToolMissing, require_chrome, require_ffmpeg, tool_versions  # noqa: E402

#: Screenshot viewport: 16:9 at 1600×900, the smallest size F23 accepts.
SHOT_VIEWPORT = (1600, 900)
#: Video viewport: 16:9 at 1280×720, the smallest size F23 accepts.
VIDEO_VIEWPORT = (1280, 720)
#: Frames per second of the captured video.  Chosen so the capture finishes in a
#: couple of minutes while every chapter still holds long enough to read.
VIDEO_FPS = 4

#: Inputs whose digests go into the manifest: change one and the media is stale.
#: The published page assets are included because the screenshots *are* those
#: pages: changing the markup or script after a capture would otherwise leave a
#: manifest that still claims to describe the current site.
INPUT_FILES = (
    "outputs/forecast.xm.parquet",
    "outputs/forecast.sh.parquet",
    "outputs/replay.rule.xm.parquet",
    "outputs/replay.optimized.xm.parquet",
    "outputs/replay.rule.sh.parquet",
    "outputs/replay.optimized.sh.parquet",
    "outputs/sim_rule.xm.csv",
    "outputs/sim_optimized.xm.csv",
    "outputs/sim_rule.sh.csv",
    "outputs/sim_optimized.sh.csv",
    "outputs/sim_sensitivity.xm.json",
    "outputs/sim_sensitivity.sh.json",
    "data/basemap/raw/xm.json",
    "data/basemap/raw/sh.json",
    "replay/workbench.css",
    "replay/workbench.js",
    "replay/workbench-core.js",
    "replay/workbench-geo.js",
    "replay/xm/workbench.html",
    "replay/sh/workbench.html",
    "replay/xm/replay-data.js",
    "replay/sh/replay-data.js",
    "replay/xm/presentation.js",
    "replay/sh/presentation.js",
    "replay/xm/basemap.js",
    "replay/sh/basemap.js",
    "replay/xm/forecast.js",
    "replay/sh/forecast.js",
)

#: The published page assets among those inputs.  Derived, not restated: the gate
#: and the tests ask for "the page assets the screenshots depict", and a second
#: hand-written list would go stale the first time a file is added here.
PAGE_ASSETS = tuple(name for name in INPUT_FILES if name.startswith("replay/"))


# -- helpers ----------------------------------------------------------------
def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def input_files_digests() -> dict[str, str | None]:
    """The per-file digests the manifest records alongside the single digest."""
    return {name: (_sha(ROOT / name) if (ROOT / name).exists() else None) for name in INPUT_FILES}


class SnapshotMissing(RuntimeError):
    """A verify pass was asked to preserve a snapshot the manifest does not have."""


def input_digest() -> str:
    """One digest over every input the media depends on."""
    parts = [f"{name}:{digest or 'missing'}" for name, digest in input_files_digests().items()]
    return hashlib.sha256("\n".join(parts).encode("utf-8")).hexdigest()


def stale_entries(entries: list[dict[str, Any]], digest: str) -> list[str]:
    """Media captured against a different input snapshot than ``digest``.

    Every entry carries the digest of the tree it was captured from.  Comparing
    those to the current tree is what makes a partial re-capture visible: the
    files a run did not rebuild keep their old stamp, so they cannot inherit the
    freshness of the ones it did.
    """
    return [entry["path"] for entry in entries if entry.get("input_digest") != digest]


def capture_snapshot(mode: str, existing: dict[str, Any]) -> tuple[str, dict[str, str | None]]:
    """The input snapshot a run should record in the manifest.

    ``--verify`` photographs nothing, so it must not restamp the tree it happens
    to see: doing so would let a stale screenshot inherit a fresh digest from a
    run that never looked at it.  Only a capture records a new snapshot, so a
    verify pass with nothing to inherit is an error rather than a licence to
    describe a snapshot the media never had.
    """
    if mode == "verify":
        digest = existing.get("input_digest")
        files = existing.get("input_files")
        if not digest or not files:
            raise SnapshotMissing(
                "--verify 只能沿用已有清单里的捕获快照，但这份清单没有记录。"
                "先运行 `python3 scripts/capture_presentation.py --all` 重新捕获。"
            )
        return digest, files
    return input_digest(), input_files_digests()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _page(source: str) -> str:
    return (SITE / source / "workbench.html").as_uri()


# -- state driving ----------------------------------------------------------
#: Each step is (kind, argument).  ``js`` runs an expression, ``click`` clicks a
#: selector and fails when it matches nothing, ``scroll`` brings a selector into
#: view.  Keeping the recipe declarative is what makes "the same semantic state"
#: reproducible: the manifest records the recipe, not just the pixels.
def _steps(*items: tuple[str, str]) -> list[tuple[str, str]]:
    return list(items)


SHOTS: list[dict[str, Any]] = [
    {
        "key": "guide-overview",
        "title": "导览概貌 · XM",
        "caption": "XM 复盘工作台的演示导览首章：4 个时间单元、169 个格子，S-RULE 与 S-OPT 的未满足请求与搬运量。",
        "source": "xm",
        "theme": "light",
        "steps": _steps(
            ("click", '[data-guide-action="start"]'),
            ("scroll", "#guide"),
        ),
        "mode": "side",
        "projection": "grid",
    },
    {
        "key": "xm-geo-delta",
        "title": "XM 地理底图上的差分地图",
        "caption": "XM 第 2 个时间单元、差分模式下切到地理底图：两个退步格（S-OPT 未满足更多）落在厦门岛内，色域固定 ±102 次。",
        "source": "xm",
        "theme": "light",
        "steps": _steps(
            ("js", "var s=document.querySelector('[data-control=\"scrub\"]');s.value='1';s.dispatchEvent(new Event('input',{bubbles:true}));"),
            ("click", '[data-mode="delta"]'),
            ("click", '[data-projection="geo"]'),
            ("scroll", "[data-maps]"),
        ),
        "mode": "delta",
        "projection": "geo",
    },
    {
        "key": "xm-ledger",
        "title": "XM 单格库存账本",
        "caption": "XM 默认格 C0007_0010（累计 S-RULE 未满足最高）在两策略下的期初/调入/调出/归还/期末与全回放历史，四个派生值都能由自身操作数复算。",
        "source": "xm",
        "theme": "light",
        "steps": _steps(
            ("click", '[data-mode="side"]'),
            ("scroll", "#ledger"),
        ),
        "mode": "side",
        "projection": "grid",
    },
    {
        "key": "sh-forecast",
        "title": "SH 预测联动",
        "caption": "SH 当前格的实际、季节均值基线与模型预测三条序列（149 个测试时段），当前点误差与局部 MAE（含样本数）同屏显示。",
        "source": "sh",
        "theme": "light",
        "steps": _steps(
            ("scroll", "#forecast"),
            ("click", "#forecast-chart circle:nth-of-type(40)"),
        ),
        "mode": "side",
        "projection": "grid",
    },
    {
        "key": "scenario",
        "title": "敏感性场景切换",
        "caption": "XM 切换到 initial_fill=0.3 场景：两策略四项汇总指标相对同源同策略 baseline 的绝对与百分比变化；地图仍是 baseline 回放。",
        "source": "xm",
        "theme": "light",
        "steps": _steps(
            ("click", '[data-scenario="initial_fill=0.3"]'),
            ("scroll", "#scenarios"),
        ),
        "mode": "side",
        "projection": "grid",
    },
    {
        "key": "sh-geo",
        "title": "SH 地理底图",
        "caption": "SH 工作台切到地理底图：黄浦江与区界为定位语境，255 个格子的期末库存叠加其上，比例尺与出处可复算。",
        "source": "sh",
        "theme": "light",
        "steps": _steps(
            ("click", '[data-mode="side"]'),
            ("click", '[data-projection="geo"]'),
            ("scroll", "[data-maps]"),
        ),
        "mode": "side",
        "projection": "geo",
    },
]


#: The video's chapters.  ``hold`` is playback seconds; ``data_clock`` records the
#: *data* time each chapter shows, which is deliberately separate from the video
#: timeline (a 15-second chapter can show one hourly bucket, and the video never
#: claims a finer observation rate than the data has).
CHAPTERS: list[dict[str, Any]] = [
    {
        "id": "xm-overview",
        "title": "XM · 导览概貌",
        "hold": 16,
        "source": "xm",
        "steps": _steps(("click", '[data-guide-action="start"]'), ("scroll", "#guide")),
    },
    {
        "id": "xm-event",
        "title": "XM · 关键事件跳转",
        "hold": 16,
        "source": "xm",
        "steps": _steps(
            ("js", "document.querySelector('[data-guide-action=\"exit\"]').click();"),
            ("click", "[data-event]:not([disabled])"),
            ("scroll", "[data-maps]"),
        ),
    },
    {
        "id": "xm-delta",
        "title": "XM · 地理底图上的策略差分",
        "hold": 18,
        "source": "xm",
        # Frame 2 is the one with a real difference (net +125, two regressed
        # cells).  Frame 1 is all zero, and a delta chapter that shows only zeros
        # would leave the viewer unable to see what the metric does.
        "steps": _steps(
            ("js", "var s=document.querySelector('[data-control=\"scrub\"]');s.value='1';s.dispatchEvent(new Event('input',{bubbles:true}));"),
            ("click", '[data-mode="delta"]'),
            ("click", '[data-projection="geo"]'),
            ("scroll", "[data-maps]"),
        ),
    },
    {
        "id": "xm-ledger",
        "title": "XM · 单格账本",
        "hold": 16,
        "source": "xm",
        "steps": _steps(
            ("click", '[data-mode="side"]'),
            ("click", '[data-projection="grid"]'),
            ("scroll", "#ledger"),
        ),
    },
    {
        "id": "sh-forecast",
        "title": "SH · 预测联动",
        "hold": 18,
        "source": "sh",
        "steps": _steps(("scroll", "#forecast"), ("click", "#forecast-chart circle:nth-of-type(40)")),
    },
    {
        "id": "scenario",
        "title": "XM · 敏感性场景",
        "hold": 16,
        "source": "xm",
        "steps": _steps(("click", '[data-scenario="initial_fill=0.3"]'), ("scroll", "#scenarios")),
    },
    {
        "id": "sh-geo",
        "title": "SH · 地理底图",
        "hold": 16,
        "source": "sh",
        "steps": _steps(
            ("click", '[data-mode="side"]'),
            ("click", '[data-projection="geo"]'),
            ("scroll", "[data-maps]"),
        ),
    },
]


async def _apply(browser: Browser, steps: list[tuple[str, str]]) -> None:
    for kind, argument in steps:
        if kind == "js":
            await browser.evaluate(argument)
        elif kind == "click":
            if not await browser.click(argument):
                raise RuntimeError(f"页面缺少控件 {argument}；截图会造假，因此中止")
        elif kind == "scroll":
            ok = await browser.evaluate(
                "(function(){var el=document.querySelector(%s);if(!el)return false;"
                "el.scrollIntoView({block:'start'});return true;})()" % json.dumps(argument)
            )
            if not ok:
                raise RuntimeError(f"页面缺少区域 {argument}")
        else:  # pragma: no cover - programming error
            raise ValueError(f"unknown step kind {kind}")
        await asyncio.sleep(0.15)


READ_STATE = r"""
(function(){
  function val(id) {
    var node = document.getElementById(id);
    return node ? node.getAttribute('data-value') : null;
  }
  function text(id) {
    var node = document.getElementById(id);
    return node ? node.textContent.trim() : null;
  }
  function metric(name) {
    var node = document.querySelector('[data-metric="' + name + '"]');
    return node ? node.getAttribute('data-value') : null;
  }
  var playState = document.getElementById('play-state');
  return {
    source: document.body.getAttribute('data-source'),
    frame: parseInt(val('frame-label'), 10) - 1,
    time_bucket: val('bucket-label'),
    mode: val('state-mode'),
    projection: (document.querySelector('[data-projection].active') || {}).getAttribute
      ? document.querySelector('[data-projection].active').getAttribute('data-projection') : null,
    cell: val('state-cell') || val('ledger-cell'),
    scenario: val('state-scenario'),
    playing: playState ? playState.getAttribute('data-playing') : null,
    forecast_metric: (document.querySelector('[data-forecast-metric].active') || {}).getAttribute
      ? document.querySelector('[data-forecast-metric].active').getAttribute('data-forecast-metric') : null,
    forecast_range: (document.querySelector('[data-forecast-range].active') || {}).getAttribute
      ? document.querySelector('[data-forecast-range].active').getAttribute('data-forecast-range') : null,
    forecast_bucket: val('forecast-bucket'),
    forecast_mae: val('forecast-mae'),
    forecast_n: val('forecast-n'),
    delta_net: metric('delta_net'),
    scroll_y: Math.round(window.scrollY)
  };
})()
"""


async def _state(browser: Browser) -> dict[str, Any]:
    return await browser.evaluate(READ_STATE)


#: The state keys that identify a screenshot's *analysis position*.  They are
#: compared exactly when a recorded recipe is replayed, so "the state keys can
#: relocate to this page" is measured rather than asserted.  ``playing`` and
#: ``scroll_y`` are excluded: one is playback transport, the other a viewport
#: side effect of the scroll step.
IDENTITY_KEYS = (
    "source",
    "frame",
    "time_bucket",
    "mode",
    "projection",
    "cell",
    "scenario",
    "forecast_metric",
    "forecast_range",
    "forecast_bucket",
    "forecast_mae",
    "forecast_n",
    "delta_net",
)


async def verify_shots(browser: Browser, screenshots: list[dict[str, Any]]) -> dict[str, Any]:
    """Reopen each page, replay its recorded recipe, and compare the state keys.

    This is what makes the manifest's state keys evidence rather than labels: the
    recipe is executed against the live site and the resulting state must equal
    the state that was recorded at capture time, key for key.
    """
    states: dict[str, Any] = {}
    mismatches: list[dict[str, Any]] = []
    for shot in screenshots:
        await browser.call("Emulation.setEmulatedMedia", {
            "features": [{"name": "prefers-color-scheme", "value": shot["theme"]}]
        })
        await browser.goto(_page(shot["source"]), settle=0.8)
        await _apply(browser, [tuple(step) for step in shot["recipe"]])
        await asyncio.sleep(0.35)
        replayed = await _state(browser)
        expected = {key: shot.get(key) for key in IDENTITY_KEYS}
        actual = {key: replayed.get(key) for key in IDENTITY_KEYS}
        ok = expected == actual
        states[shot["key"]] = {"ok": ok, "expected": expected, "replayed": actual}
        if not ok:
            mismatches.append({"key": shot["key"], "expected": expected, "replayed": actual})
        print(f"[verify] {shot['key']}: {'ok' if ok else 'MISMATCH'}")
    return {
        "ok": not mismatches,
        "checked_at": _now(),
        "n": len(screenshots),
        "keys": list(IDENTITY_KEYS),
        "states": states,
        "mismatches": mismatches,
    }


# -- capture ----------------------------------------------------------------
async def capture_shots(browser: Browser, out_dir: Path) -> list[dict[str, Any]]:
    versions = tool_versions()
    digest = input_digest()
    results = []
    for shot in SHOTS:
        await browser.call("Emulation.setEmulatedMedia", {
            "features": [{"name": "prefers-color-scheme", "value": shot["theme"]}]
        })
        await browser.goto(_page(shot["source"]), settle=0.8)
        await _apply(browser, shot["steps"])
        await asyncio.sleep(0.35)
        state = await _state(browser)
        if state["source"] != shot["source"]:
            raise RuntimeError(f"{shot['key']}: 页面数据源是 {state['source']}，与声明的 {shot['source']} 不符")
        if state["mode"] != shot["mode"] or state["projection"] != shot["projection"]:
            raise RuntimeError(
                f"{shot['key']}: 实际模式 {state['mode']}/{state['projection']}，"
                f"与声明的 {shot['mode']}/{shot['projection']} 不符"
            )
        path = out_dir / f"{shot['key']}.png"
        await browser.screenshot(path)
        results.append({
            "key": shot["key"],
            "title": shot["title"],
            "caption": shot["caption"],
            "path": str(path.relative_to(ROOT)),
            "type": "image/png",
            "page": f"replay/{shot['source']}/workbench.html",
            "capture_command": f"python3 scripts/capture_presentation.py --shots  # key={shot['key']}",
            "tool_versions": versions,
            "input_digest": digest,
            "theme": shot["theme"],
            "viewport": f"{SHOT_VIEWPORT[0]}x{SHOT_VIEWPORT[1]}",
            "source": state["source"],
            "frame": state["frame"],
            "time_bucket": state["time_bucket"],
            "mode": state["mode"],
            "projection": state["projection"],
            "cell": state["cell"],
            "scenario": state["scenario"],
            "forecast_metric": state["forecast_metric"],
            "forecast_range": state["forecast_range"],
            "forecast_bucket": state["forecast_bucket"],
            "forecast_mae": state["forecast_mae"],
            "forecast_n": state["forecast_n"],
            "delta_net": state["delta_net"],
            "recipe": [[kind, argument] for kind, argument in shot["steps"]],
            "sha256": _sha(path),
            "bytes": path.stat().st_size,
            "captured_at": _now(),
            "status": "ok",
        })
        print(f"[shots] {shot['key']}: {path.stat().st_size:,} B  "
              f"({state['source']} frame {state['frame']} {state['mode']}/{state['projection']})")
    return results


async def capture_video(browser: Browser, out_dir: Path, workdir: Path) -> dict[str, Any]:
    frames_dir = workdir / "frames"
    frames_dir.mkdir(parents=True, exist_ok=True)
    await browser.call("Emulation.setEmulatedMedia", {
        "features": [{"name": "prefers-color-scheme", "value": "light"}]
    })

    index = 0
    chapter_log: list[dict[str, Any]] = []
    elapsed = 0.0
    for chapter in CHAPTERS:
        await browser.goto(_page(chapter["source"]), settle=0.8)
        await _apply(browser, chapter["steps"])
        state = await _state(browser)
        if state["source"] != chapter["source"]:
            raise RuntimeError(f"{chapter['id']}: 页面数据源 {state['source']} 与声明不符")
        n_frames = int(chapter["hold"] * VIDEO_FPS)
        started = elapsed
        for _ in range(n_frames):
            path = frames_dir / f"frame-{index:05d}.png"
            await browser.screenshot(path)
            index += 1
            elapsed += 1.0 / VIDEO_FPS
        chapter_log.append({
            "id": chapter["id"],
            "title": chapter["title"],
            "at": round(started, 2),
            "until": round(elapsed, 2),
            "data_clock": {
                "source": state["source"],
                "frame": state["frame"],
                "time_bucket": state["time_bucket"],
                "note": "本章展示的数据时段；视频时长与数据时钟无关",
            },
            "state": {
                "mode": state["mode"],
                "projection": state["projection"],
                "cell": state["cell"],
                "scenario": state["scenario"],
                "forecast_metric": state["forecast_metric"],
                "forecast_range": state["forecast_range"],
            },
            "recipe": [[kind, argument] for kind, argument in chapter["steps"]],
        })
        print(f"[video] {chapter['id']}: {n_frames} frames, {started:.1f}s → {elapsed:.1f}s")

    target = out_dir / "demo.webm"
    ffmpeg = require_ffmpeg()
    command = [
        ffmpeg, "-y", "-v", "error",
        "-framerate", str(VIDEO_FPS),
        "-i", str(frames_dir / "frame-%05d.png"),
        "-c:v", "libvpx-vp9", "-b:v", "0", "-crf", "38",
        "-row-mt", "1", "-pix_fmt", "yuv420p",
        str(target),
    ]
    completed = subprocess.run(command, capture_output=True, text=True, check=False)
    if completed.returncode != 0:
        raise RuntimeError(f"ffmpeg 编码失败（退出码 {completed.returncode}）：{completed.stderr[-800:]}")
    duration = elapsed
    return {
        "key": "demo",
        "title": "演示录屏 · 两源复盘",
        "path": str(target.relative_to(ROOT)),
        "type": "video/webm",
        "page": "replay/xm/workbench.html ; replay/sh/workbench.html",
        "capture_command": "python3 scripts/capture_presentation.py --video",
        "tool_versions": tool_versions(),
        "input_digest": input_digest(),
        "theme": "light",
        "viewport": f"{VIDEO_VIEWPORT[0]}x{VIDEO_VIEWPORT[1]}",
        "fps": VIDEO_FPS,
        "duration_s": round(duration, 2),
        "declared_duration_s": round(duration, 2),
        "chapters": chapter_log,
        "frames_captured": index,
        "sha256": _sha(target),
        "bytes": target.stat().st_size,
        "captured_at": _now(),
        "status": "ok",
        "note": (
            "演示选段、模拟值：按既定章节停留并显式切章，画面全部来自真实已生成页面；"
            "视频播放时长与数据时钟分开，数据只有小时级时间单元，视频不暗示更细的观测频率。"
        ),
    }


# -- manifest ---------------------------------------------------------------
def _existing_manifest() -> dict[str, Any]:
    path = MEDIA / "manifest.json"
    if path.exists():
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except ValueError:
            return {}
    return {}


def _write_manifest(
    *,
    screenshots: list[dict[str, Any]],
    videos: list[dict[str, Any]],
    run: dict[str, Any],
    digest: str,
    files: dict[str, str | None],
    replay_check: dict[str, Any] | None = None,
) -> Path:
    """Write the manifest around a snapshot the caller chose.

    ``digest``/``files`` are passed in rather than recomputed here: a
    verification pass must keep the capture-time snapshot, and a partial
    re-capture must be able to show that it left the other media behind.
    """
    MEDIA.mkdir(parents=True, exist_ok=True)
    manifest = {
        "phase": 5,
        "requirement": "REQUIREMENTS-PHASE5.md F23",
        "generated_by": "scripts/capture_presentation.py",
        "note": (
            "截图与视频都是真实浏览器对已生成页面的捕获；不使用图片编辑或合成补出未实现的控件、数字或底图。"
            "媒体放在仓库根 presentations/phase5/，不进入 replay/，也不扩大静态 HTTP 根。"
        ),
        "tool_versions": tool_versions(),
        "input_digest": digest,
        "input_files": files,
        "capture_command": (
            "python3 scripts/capture_presentation.py --all"
            if run.get("mode") == "all"
            else f"python3 scripts/capture_presentation.py --{run.get('mode')}"
        ),
        "screenshots": screenshots,
        "videos": videos,
        "replay_check": replay_check,
        "last_run": run,
    }
    path = MEDIA / "manifest.json"
    path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return path


def _human_manifest(manifest: dict[str, Any]) -> str:
    lines = [
        "# Phase 5 演示素材清单",
        "",
        "本目录的截图与视频由 `scripts/capture_presentation.py` 从真实已生成页面捕获。",
        "机器可读清单见 `manifest.json`；下面的状态键可用于重新定位到同一页面状态。",
        "",
        f"- 捕获命令：`{manifest['capture_command']}`",
        f"- 输入摘要：`{manifest['input_digest'][:16]}…`（任一输入变化即视为素材过期）",
        f"- 工具：{manifest['tool_versions'].get('chrome', {}).get('version', 'n/a')}；"
        f"{manifest['tool_versions'].get('ffmpeg', {}).get('version', 'n/a')}",
        "",
        "## 截图",
        "",
    ]
    for shot in manifest.get("screenshots", []):
        lines += [
            f"### {shot['title']}",
            "",
            f"- 文件：`{shot['path']}`（{shot['bytes']:,} B，sha256 `{shot['sha256'][:16]}…`）",
            f"- 页面：`{shot['page']}`；视口 {shot['viewport']}；主题 {shot['theme']}",
            f"- 状态：源 {shot['source']}，帧 {shot['frame']}（{shot['time_bucket']}），"
            f"模式 {shot['mode']}，投影 {shot['projection']}，格子 {shot['cell']}，场景 {shot['scenario']}",
            f"- 预测：指标 {shot['forecast_metric']}，范围 {shot['forecast_range']}，"
            f"时段 {shot['forecast_bucket']}，局部 MAE {shot['forecast_mae']}（n={shot['forecast_n']}）",
            f"- 图注：{shot['caption']}",
            "",
        ]
    lines += ["## 视频", ""]
    for video in manifest.get("videos", []):
        lines += [
            f"### {video['title']}",
            "",
            f"- 文件：`{video['path']}`（{video['bytes']:,} B，sha256 `{video['sha256'][:16]}…`）",
            f"- 视口 {video['viewport']}，{video['fps']} fps，时长 {video['declared_duration_s']} 秒",
            f"- {video['note']}",
            "",
            "| 章节 | 起 | 止 | 数据源 | 数据时段 |",
            "|---|---|---|---|---|",
        ]
        for chapter in video.get("chapters", []):
            clock = chapter["data_clock"]
            lines.append(
                f"| {chapter['title']} | {chapter['at']}s | {chapter['until']}s | "
                f"{clock['source'].upper()} | {clock['time_bucket']} |"
            )
        lines.append("")
    check = manifest.get("replay_check") or {}
    lines += [
        "## 状态键复核",
        "",
        "`--verify` 会重新打开每张截图对应的页面、重放 `recipe` 里的步骤，再把读回的状态键与捕获时记录的逐项比较。"
        "这是「状态键能重新定位到当前页面」的实测证据，不是说明文字。",
        "",
    ]
    if check:
        lines.append(
            f"- 最近一次复核：{'通过' if check.get('ok') else '失败'}"
            f"（{check.get('n')} 张，{check.get('checked_at')}）"
        )
        for key in check.get("keys", []):
            lines.append(f"  - `{key}`")
        for mismatch in check.get("mismatches", []):
            lines.append(f"- 不一致：`{mismatch['key']}`")
    else:
        lines.append("- 尚未复核：运行 `python3 scripts/capture_presentation.py --verify`。")
    lines.append("")
    return "\n".join(lines)


# -- entry point ------------------------------------------------------------
def check() -> int:
    versions = tool_versions()
    problems = []
    for name, label, fix in (
        ("chrome", "Chrome/Chromium", "安装 Google Chrome 或 Chromium"),
        ("ffmpeg", "ffmpeg", "macOS: brew install ffmpeg；Linux: 用发行版包管理器安装"),
        ("websockets", "python 包 websockets", "python3 -m pip install websockets"),
    ):
        if not versions.get(name):
            problems.append(f"{label} 缺失 —— {fix}")
    print(json.dumps(versions, ensure_ascii=False, indent=2))
    if problems:
        print("\n导出工具不完整：")
        for problem in problems:
            print(f"  - {problem}")
        print("\n主站、`bike-agent replay` 与测试不需要这些工具；只有截图/录屏导出需要。")
        return 2
    print("\n导出工具齐备。")
    return 0


async def _run(mode: str) -> int:
    require_chrome()
    MEDIA.mkdir(parents=True, exist_ok=True)
    existing = _existing_manifest()
    screenshots = list(existing.get("screenshots", []))
    videos = list(existing.get("videos", []))
    replay_check = existing.get("replay_check")
    if mode == "verify" and not screenshots:
        print("没有可复核的截图：先运行 `--shots` 或 `--all`。", file=sys.stderr)
        return 2
    started_at = _now()
    written: list[Path] = []
    exit_code = 0
    with tempfile.TemporaryDirectory(prefix="bike-capture-work-", ignore_cleanup_errors=True) as work:
        workdir = Path(work)
        if mode in {"shots", "all"}:
            async with Browser(*SHOT_VIEWPORT) as browser:
                screenshots = await capture_shots(browser, MEDIA)
                # Same browser, same build: replay every recipe right after the
                # capture so the manifest's state keys are proven to relocate.
                replay_check = await verify_shots(browser, screenshots)
            written += [ROOT / item["path"] for item in screenshots]
            if not replay_check["ok"]:
                exit_code = 1
        elif mode == "verify":
            async with Browser(*SHOT_VIEWPORT) as browser:
                replay_check = await verify_shots(browser, screenshots)
            if not replay_check["ok"]:
                exit_code = 1
        if mode in {"video", "all"}:
            async with Browser(*VIDEO_VIEWPORT) as browser:
                video = await capture_video(browser, MEDIA, workdir)
            videos = [video]
            written.append(ROOT / video["path"])
    finished_at = _now()
    digest, files = capture_snapshot(mode, existing)
    if mode == "verify":
        # A verification pass writes no media, so it must not claim to be the run
        # that produced the files.  Keep the recorded export run intact.
        run = existing.get("last_run") or {"mode": "verify", "command": "", "exit_code": 0,
                                           "started_at": started_at, "finished_at": finished_at,
                                           "files": []}
        # The state keys can relocate onto a page that has since changed, so say
        # so here rather than letting the operator discover it at the next gate.
        if input_digest() != digest:
            print(
                "[verify] 注意：输入已改动，这份素材已过期（状态键仍能定位，但画面不是当前页面的）。"
                "重新捕获用 `--all`。",
                file=sys.stderr,
            )
    else:
        # ``files`` lists the whole media set the manifest now describes, with a
        # flag for the ones this run wrote.  A ``--shots`` run refreshes only the
        # PNGs but the video it leaves alone is still part of the delivered set,
        # so the list must not imply the video is missing.
        written_paths = {str(path.relative_to(ROOT)) for path in written if path.exists()}
        run = {
            "mode": mode,
            "command": f"python3 scripts/capture_presentation.py --{mode}",
            "exit_code": exit_code,
            "started_at": started_at,
            "finished_at": finished_at,
            "files": [
                {
                    "path": entry["path"],
                    "bytes": entry["bytes"],
                    "sha256": entry["sha256"],
                    "written": entry["path"] in written_paths,
                }
                for entry in [*screenshots, *videos]
            ],
        }
    # A partial run rebuilds only its own media.  The rest keep the stamp of the
    # tree they were captured from, so say so here and fail: a delivered set that
    # mixes snapshots is not the set the manifest describes.
    stale = stale_entries([*screenshots, *videos], digest)
    if stale:
        exit_code = exit_code or 1
        print(
            f"[stale] {len(stale)} 个素材仍是旧输入：{', '.join(stale)}。"
            "重跑 `python3 scripts/capture_presentation.py --all` 刷新整套。",
            file=sys.stderr,
        )
        if mode != "verify":
            run["stale_media"] = stale
            run["exit_code"] = exit_code
    manifest = _write_manifest(
        screenshots=screenshots, videos=videos, run=run, digest=digest, files=files,
        replay_check=replay_check,
    )
    (MEDIA / "README.md").write_text(_human_manifest(_existing_manifest()), encoding="utf-8")
    print(f"[manifest] {manifest.relative_to(ROOT)}")
    if mode == "verify":
        print(f"[verify] {'all state keys relocate' if exit_code == 0 else 'state keys did NOT relocate'}")
    return exit_code


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--check", action="store_true", help="report tool availability and versions")
    group.add_argument("--shots", action="store_true", help="capture the six screenshots")
    group.add_argument("--video", action="store_true", help="capture the demo video")
    group.add_argument("--all", action="store_true", help="capture both, then write the manifest")
    group.add_argument("--verify", action="store_true", help="replay the recorded state keys, no capture")
    args = parser.parse_args(argv)

    if args.check:
        return check()
    mode = "shots" if args.shots else ("video" if args.video else ("all" if args.all else "verify"))
    try:
        return asyncio.run(_run(mode))
    except ToolMissing as error:
        print(f"导出失败：{error}", file=sys.stderr)
        return 2
    except SnapshotMissing as error:
        print(f"复核失败：{error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
