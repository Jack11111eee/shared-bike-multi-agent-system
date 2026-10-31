"""F15: static hosting and the timeline endpoint.

The server is started on an ephemeral port (F15-6) and every response is
compared against the shipped file or ``outputs/`` -- never against a hand-copied
expectation.  ``tests/test_serve.py`` is deliberately left untouched: this file
only adds the Phase 3 surface.
"""

from __future__ import annotations

import json
import socket
import threading
import urllib.error
import urllib.request
from pathlib import Path

import pandas as pd
import pytest

from bike_agent.config import Layout, load_config
from bike_agent.serve import make_server

ROOT = Path(__file__).resolve().parents[1]
SITE = ROOT / "replay"


def _ready() -> bool:
    needed = [SITE / "index.html", SITE / "xm" / "player.html", SITE / "xm" / "replay-data.js"]
    return all(path.exists() for path in needed)


@pytest.fixture(scope="module")
def live():
    if not _ready():
        pytest.skip("player site not built; run `bike-agent repro` first")
    config, layout = load_config(), Layout()
    server = make_server(config, layout, port=0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    host, port = server.server_address[:2]
    try:
        yield f"http://{host}:{port}", host, port, layout
    finally:
        server.shutdown()
        server.server_close()


def _fetch(url: str):
    with urllib.request.urlopen(url, timeout=30) as response:
        return response.status, response.headers.get("Content-Type", ""), response.read()


def _raw(host: str, port: int, path: str) -> tuple[int, bytes]:
    with socket.create_connection((host, port), timeout=10) as sock:
        request = f"GET {path} HTTP/1.1\r\nHost: test\r\nConnection: close\r\n\r\n"
        sock.sendall(request.encode("ascii"))
        chunks = []
        while True:
            chunk = sock.recv(65536)
            if not chunk:
                break
            chunks.append(chunk)
    raw = b"".join(chunks)
    head, _, body = raw.partition(b"\r\n\r\n")
    status = int(head.split(b" ", 2)[1])
    return status, body


# -- F15-1 ------------------------------------------------------------------
def test_root_serves_the_site_byte_for_byte(live):
    base, _, _, _ = live
    status, content_type, body = _fetch(f"{base}/")
    assert status == 200
    assert content_type.split(";")[0] == "text/html"
    assert body == (SITE / "index.html").read_bytes()


def test_player_and_bundle_have_the_right_types(live):
    base, _, _, _ = live
    status, content_type, body = _fetch(f"{base}/xm/player.html")
    assert status == 200
    assert content_type.split(";")[0] == "text/html"
    assert body == (SITE / "xm" / "player.html").read_bytes()

    status, content_type, body = _fetch(f"{base}/sh/replay-data.js")
    assert status == 200
    assert content_type.split(";")[0] in ("application/javascript", "text/javascript")
    assert body == (SITE / "sh" / "replay-data.js").read_bytes()

    status, content_type, body = _fetch(f"{base}/dashboard.html")
    assert status == 200
    assert content_type.split(";")[0] == "text/html"
    assert body == (SITE / "dashboard.html").read_bytes()


# -- F15-2 ------------------------------------------------------------------
def test_path_traversal_is_refused_without_leaking_content(live):
    _, host, port, _ = live
    for path in (
        "/../REQUIREMENTS.md",
        "/%2e%2e/REQUIREMENTS.md",
        "/%2e%2e%2fREQUIREMENTS.md",
        "/etc/passwd",
    ):
        status, body = _raw(host, port, path)
        assert 400 <= status < 500, f"{path} -> {status}"
        assert b"REQUIREMENTS-PHASE" not in body
        assert b"root:" not in body


# -- F15-3 ------------------------------------------------------------------
@pytest.mark.parametrize("source", ["xm", "sh"])
def test_timeline_is_the_same_bundle_as_the_static_site(live, source):
    base, _, _, layout = live
    status, _, body = _fetch(f"{base}/api/replay/timeline?source={source}")
    assert status == 200
    served = json.loads(body)
    static = json.loads(
        (SITE / source / "replay-data.js")
        .read_text(encoding="utf-8")
        .removeprefix("window.REPLAY_DATA = ")[:-1]
    )
    assert served == static

    steps = pd.read_csv(layout.sim_path(source, "rule", "csv"))
    steps["time_bucket"] = pd.to_datetime(steps["time_bucket"])
    steps = steps.sort_values("time_bucket").reset_index(drop=True)
    assert served["n_frames"] == len(served["frames"]) == len(steps)
    assert [frame["time_bucket"] for frame in served["frames"]] == [
        ts.strftime("%Y-%m-%d %H:%M:%S") for ts in steps["time_bucket"]
    ]

    records = json.loads(
        (layout.outputs / f"sim_moves.rule.{source}.json").read_text(encoding="utf-8")
    )
    for frame in served["frames"]:
        expected = sorted(
            (str(item["from_cell"]), str(item["to_cell"]), int(item["bikes"]))
            for item in records
            if pd.Timestamp(item["time_bucket"]) == pd.Timestamp(frame["time_bucket"])
        )
        served_moves = sorted(
            (str(item["from_cell"]), str(item["to_cell"]), int(item["bikes"]))
            for item in frame["moves"]["rule"]
        )
        assert served_moves == expected


# -- F15-4 ------------------------------------------------------------------
def test_timeline_reports_missing_artifacts(tmp_path):
    layout = Layout(tmp_path)
    layout.ensure()
    config = load_config()
    server = make_server(config, layout, port=0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    host, port = server.server_address[:2]
    try:
        with pytest.raises(urllib.error.HTTPError) as excinfo:
            urllib.request.urlopen(f"http://{host}:{port}/api/replay/timeline?source=xm", timeout=30)
        assert 400 <= excinfo.value.code < 500
        payload = json.loads(excinfo.value.read().decode("utf-8"))
        assert payload["error"]
    finally:
        server.shutdown()
        server.server_close()


# -- F15-6 ------------------------------------------------------------------
def test_server_uses_an_ephemeral_port(live):
    base, _, port, _ = live
    assert port != 0 and port != 8000
    assert base.startswith("http://127.0.0.1:")


def test_legacy_api_aliases_are_not_swallowed_by_the_static_handler():
    """A path `_route_get` answers must never fall through to the file server."""
    from bike_agent.serve.app import LEGACY_API_PATHS, _Handler

    for path in LEGACY_API_PATHS:
        assert _Handler._is_api(path), f"{path} would be served as a static file"
    assert not _Handler._is_api("/xm/player.html")
    assert not _Handler._is_api("/")
