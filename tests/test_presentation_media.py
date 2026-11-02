"""F23: the media deliverables -- decoded, not trusted.

The point of these tests is that a file with the right extension proves nothing.
Each screenshot is decoded with Pillow (which walks the whole PNG stream), and the
video is probed *and* fully decoded by ffmpeg.  A manifest entry whose sha256 no
longer matches its file means the input changed and the media was not rebuilt;
that is a failure here, not a warning.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
MEDIA = ROOT / "presentations" / "phase5"
SITE = ROOT / "replay"

REQUIRED_SHOTS = {
    "guide-overview",
    "xm-geo-delta",
    "xm-ledger",
    "sh-forecast",
    "scenario",
    "sh-geo",
}
REQUIRED_CHAPTERS = {"xm-overview", "xm-event", "xm-delta", "xm-ledger", "sh-forecast", "scenario", "sh-geo"}


@pytest.fixture(scope="module")
def manifest():
    path = MEDIA / "manifest.json"
    if not path.exists():
        pytest.fail(
            "presentations/phase5/manifest.json 不存在 —— F23 尚未交付。"
            "运行 `python3 scripts/capture_presentation.py --all`。"
        )
    return json.loads(path.read_text(encoding="utf-8"))


def _sha(path: Path) -> str:
    import hashlib

    return hashlib.sha256(path.read_bytes()).hexdigest()


# -- screenshots ------------------------------------------------------------
def test_six_screenshots_are_present_and_decodable(manifest):
    from PIL import Image

    shots = manifest["screenshots"]
    assert len(shots) >= 6
    assert {shot["key"] for shot in shots} >= REQUIRED_SHOTS
    for shot in shots:
        path = ROOT / shot["path"]
        assert path.exists(), shot["path"]
        assert path.stat().st_size > 10_000, f"{shot['path']} 太小，可能是空图"
        with Image.open(path) as image:
            image.load()  # walks the whole stream: truncated files fail here
            width, height = image.size
        assert width >= 1600 and height >= 900, f"{shot['path']} 是 {width}×{height}"
        assert abs(width / height - 16 / 9) < 0.01, f"{shot['path']} 不是 16:9"


def test_screenshot_digests_match_the_manifest(manifest):
    for shot in manifest["screenshots"]:
        assert _sha(ROOT / shot["path"]) == shot["sha256"], f"{shot['path']} 已被修改或过期"


def test_each_screenshot_records_the_state_it_shows(manifest):
    for shot in manifest["screenshots"]:
        for key in ("source", "frame", "time_bucket", "mode", "projection", "cell",
                    "scenario", "forecast_metric", "forecast_range", "page", "theme",
                    "viewport", "recipe", "captured_at", "status"):
            assert key in shot and shot[key] not in (None, ""), f"{shot['key']} 缺 {key}"
        assert shot["status"] == "ok"
        assert shot["source"] in {"xm", "sh"}
        assert shot["mode"] in {"side", "delta"}
        assert shot["projection"] in {"grid", "geo"}
        assert (ROOT / shot["page"]).exists(), shot["page"]


def test_the_recorded_inputs_still_match_the_shipped_site(manifest):
    """A page edited after capture makes the screenshots stale; prove it is not.

    ``input_digest`` covers the page assets as well as the data files, and
    ``input_files`` carries each one's digest, so this compares the manifest
    against the tree rather than trusting a timestamp.
    """
    input_files = manifest["input_files"]
    from scripts.capture_presentation import PAGE_ASSETS

    for asset in PAGE_ASSETS:
        assert asset in input_files, f"输入摘要没有覆盖 {asset}"
    for name, digest in input_files.items():
        assert digest is not None, f"{name} 在捕获时就不存在"
        assert _sha(ROOT / name) == digest, f"{name} 在捕获后改动，素材已过期"


def test_every_medium_carries_the_digest_of_its_own_capture(manifest):
    """The manifest's top-level digest is not enough -- each file needs its stamp.

    ``--verify`` writes no media and must not restamp the tree it happens to
    see, and a partial ``--shots``/``--video`` run rebuilds only part of the set.
    Either way the leftover files keep the digest of the tree they came from, so
    comparing each entry (not just the manifest header) is what makes a stale
    screenshot detectable.
    """
    from scripts.capture_presentation import input_digest, stale_entries

    entries = manifest["screenshots"] + manifest["videos"]
    for entry in entries:
        assert entry.get("input_digest"), f"{entry['path']} 没有记录捕获时的输入摘要"
        assert entry["input_digest"] == manifest["input_digest"], (
            f"{entry['path']} 的输入摘要与 manifest 顶部不一致：这份素材来自另一次捕获"
        )
    current = input_digest()
    assert manifest["input_digest"] == current, (
        "输入已变化而媒体未重建；重新运行 `python3 scripts/capture_presentation.py --all`"
    )
    assert stale_entries(entries, current) == []


def test_a_partial_recapture_is_reported_as_stale():
    """The helper the gate relies on must actually flag mixed snapshots.

    Building the failure here keeps it a property of the code rather than of a
    manual sequence of documented commands.
    """
    from scripts.capture_presentation import stale_entries

    entries = [
        {"path": "a.png", "input_digest": "new"},
        {"path": "b.webm", "input_digest": "old"},
        {"path": "c.png"},
    ]
    assert stale_entries(entries, "new") == ["b.webm", "c.png"]
    assert stale_entries(entries[:1], "new") == []


def test_a_verification_pass_keeps_the_capture_time_snapshot():
    """`--verify` photographs nothing, so it cannot restamp the inputs.

    Otherwise editing a page and running only `--verify` would refresh the
    manifest's digest while the PNGs still show the old page -- the exact
    "input changed, media not rebuilt" state NF-33 forbids recording as a pass.
    """
    from scripts.capture_presentation import SnapshotMissing, capture_snapshot

    existing = {"input_digest": "captured-at-T0", "input_files": {"replay/workbench.js": "abc"}}
    assert capture_snapshot("verify", existing) == ("captured-at-T0", existing["input_files"])
    digest, files = capture_snapshot("all", existing)
    assert digest != "captured-at-T0" and files
    # A verification pass with no snapshot to inherit must refuse, not describe a
    # snapshot the media never had.  `--verify` already exits 2 without
    # screenshots, so this only fires on a manifest written by an older tool.
    with pytest.raises(SnapshotMissing):
        capture_snapshot("verify", {})
    with pytest.raises(SnapshotMissing):
        capture_snapshot("verify", {"screenshots": [{"path": "a.png"}]})


def test_the_screenshot_set_covers_both_geographic_views(manifest):
    by_key = {shot["key"]: shot for shot in manifest["screenshots"]}
    assert by_key["xm-geo-delta"]["projection"] == "geo"
    assert by_key["xm-geo-delta"]["mode"] == "delta"
    assert by_key["xm-geo-delta"]["source"] == "xm"
    assert by_key["sh-geo"]["projection"] == "geo"
    assert by_key["sh-geo"]["source"] == "sh"
    assert by_key["sh-forecast"]["source"] == "sh"
    assert by_key["xm-ledger"]["cell"], "账本截图没有选中格"
    assert by_key["scenario"]["scenario"] != "baseline"


def test_the_recorded_state_keys_relocate_to_the_live_page(manifest):
    """The manifest claims its state keys point back at the page.  Prove it.

    ``capture_presentation.py --verify`` reopens every page, replays the recorded
    ``recipe`` and compares the readback against the recorded state, key for key.
    Without that record the state keys are labels; with it they are evidence.
    """
    replay = manifest.get("replay_check")
    assert replay, (
        "manifest 缺 replay_check —— 运行 "
        "`python3 scripts/capture_presentation.py --verify` 并把结果写回 manifest"
    )
    assert replay["ok"] is True, replay.get("mismatches")
    assert replay["n"] == len(manifest["screenshots"])
    for shot in manifest["screenshots"]:
        state = replay["states"][shot["key"]]
        assert state["ok"] is True, f"{shot['key']} 重放后状态不一致"
        assert state["replayed"] == state["expected"]
        # Every identity key must actually have been compared, not skipped.
        for key in replay["keys"]:
            assert key in state["expected"]
        assert state["expected"]["source"] == shot["source"]
        assert state["expected"]["frame"] == shot["frame"]
        assert state["expected"]["mode"] == shot["mode"]
        assert state["expected"]["projection"] == shot["projection"]


# -- video ------------------------------------------------------------------
def test_video_decodes_and_meets_the_size_and_duration_contract(manifest):
    ffprobe = shutil.which("ffprobe")
    ffmpeg = shutil.which("ffmpeg")
    if not ffprobe or not ffmpeg:
        pytest.fail("ffprobe/ffmpeg 未安装：F23 的视频无法验证。安装后再运行 `--check`。")
    videos = manifest["videos"]
    assert videos, "manifest 没有视频条目"
    for video in videos:
        path = ROOT / video["path"]
        assert path.exists() and path.stat().st_size > 100_000
        probe = subprocess.run(
            [ffprobe, "-v", "error", "-print_format", "json", "-show_format", "-show_streams", str(path)],
            capture_output=True, text=True, check=False,
        )
        assert probe.returncode == 0, probe.stderr[-400:]
        info = json.loads(probe.stdout)
        streams = [s for s in info["streams"] if s["codec_type"] == "video"]
        assert streams, "没有视频帧轨"
        stream = streams[0]
        width, height = int(stream["width"]), int(stream["height"])
        duration = float(info["format"]["duration"])
        assert width >= 1280 and height >= 720, f"{width}×{height} 太小"
        assert abs(width / height - 16 / 9) < 0.02, "不是 16:9"
        assert 90 <= duration <= 180, f"时长 {duration:.1f}s 不在 90–180s"
        decode = subprocess.run(
            [ffmpeg, "-v", "error", "-i", str(path), "-f", "null", "-"],
            capture_output=True, text=True, check=False,
        )
        assert decode.returncode == 0 and not decode.stderr.strip(), decode.stderr[-400:]
        assert _sha(path) == video["sha256"]


def test_video_chapters_cover_every_required_state(manifest):
    video = manifest["videos"][0]
    chapters = video["chapters"]
    assert {chapter["id"] for chapter in chapters} >= REQUIRED_CHAPTERS
    times = [chapter["at"] for chapter in chapters]
    assert times == sorted(times)
    for chapter in chapters:
        assert chapter["until"] > chapter["at"]
        assert chapter["data_clock"]["time_bucket"]
        assert chapter["data_clock"]["source"] in {"xm", "sh"}
    # The last chapter must end at the declared duration.
    assert abs(chapters[-1]["until"] - video["declared_duration_s"]) < 0.5


def test_video_is_labelled_as_a_segment_with_a_separate_data_clock(manifest):
    video = manifest["videos"][0]
    assert "演示选段" in video["note"]
    assert "数据时钟" in video["note"]
    for chapter in video["chapters"]:
        assert chapter["data_clock"]["note"]


# -- export record ----------------------------------------------------------
def test_the_export_command_really_ran(manifest):
    run = manifest["last_run"]
    assert run["exit_code"] == 0
    assert run["command"].startswith("python3 scripts/capture_presentation.py")
    assert run["started_at"] and run["finished_at"]
    listed = {item["path"] for item in run["files"]}
    for entry in manifest["screenshots"] + manifest["videos"]:
        assert entry["path"] in listed, f"{entry['path']} 不在导出清单里"
    # The list must distinguish "this run wrote it" from "the manifest still
    # carries it", and the recorded digests must still describe the files.
    assert any(item["written"] for item in run["files"])
    for item in run["files"]:
        assert _sha(ROOT / item["path"]) == item["sha256"]


def test_media_stays_outside_the_site_and_inside_its_budget(manifest):
    assert MEDIA.is_dir()
    assert not MEDIA.is_relative_to(SITE), "媒体不能放进 replay/，否则会扩大静态根"
    total = sum(path.stat().st_size for path in MEDIA.rglob("*") if path.is_file())
    assert total <= 100 * 1024 * 1024, f"presentations/phase5 合计 {total / 1048576:.1f} MiB"
    # `replay/figures/` holds the Phase 1–3 report charts and is part of the site
    # by design; what must not appear there is a copy of *this* phase's media.
    names = {Path(entry["path"]).name for entry in manifest["screenshots"] + manifest["videos"]}
    for path in SITE.rglob("*"):
        if path.suffix.lower() in {".mp4", ".webm"}:
            pytest.fail(f"站点内出现视频 {path.relative_to(ROOT)}")
        if path.name in names:
            pytest.fail(f"Phase 5 媒体被复制进站点 {path.relative_to(ROOT)}")


def test_tool_versions_and_input_digest_are_recorded(manifest):
    versions = manifest["tool_versions"]
    assert versions.get("chrome") and versions["chrome"].get("version")
    assert versions.get("ffmpeg") and versions["ffmpeg"].get("version")
    assert len(manifest["input_digest"]) == 64
    for name, digest in manifest["input_files"].items():
        assert digest, f"{name} 缺失或未记录摘要"


# -- missing tools ----------------------------------------------------------
def test_export_without_tools_fails_loudly(monkeypatch, tmp_path):
    """F23-4: no browser means a clear failure, never a skip or an empty file."""
    import scripts.capture_presentation as capture
    import scripts.cdp as cdp

    monkeypatch.setattr(cdp, "find_chrome", lambda: None)
    monkeypatch.setattr(cdp, "find_ffmpeg", lambda: None)
    assert capture.check() != 0
    with pytest.raises(cdp.ToolMissing):
        cdp.require_chrome()
    with pytest.raises(cdp.ToolMissing):
        cdp.require_ffmpeg()


def test_the_pipeline_does_not_import_the_capture_tooling():
    """The site and the pipeline must never depend on the export path."""
    import ast

    offenders = []
    for path in (ROOT / "bike_agent").rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            names = []
            if isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module:
                names = [node.module]
            for name in names:
                if name.startswith("scripts") or "capture_presentation" in name or name == "websockets":
                    offenders.append(f"{path.relative_to(ROOT)}: {name}")
    assert not offenders, offenders
