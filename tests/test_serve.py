"""F9: the HTTP service must be thin, consistent, offline and LLM-free.

The tests start a real server on an ephemeral port (F9-6: no fixed port, no
external network) and compare every response against the artefact or handler it
is supposed to mirror -- never against a hand-copied expectation.
"""

from __future__ import annotations

import json
import subprocess
import sys
import threading
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

import pandas as pd
import pytest

from bike_agent.config import Layout, load_config, source_config
from bike_agent.serve import ServiceError, list_sources, make_server, replay_index
from bike_agent.tools import handlers

ROOT = Path(__file__).resolve().parents[1]


def _pairs() -> list[tuple[str, str]]:
    layout, config = Layout(), load_config()
    pairs = []
    for source in config.get("sources", {}):
        for strategy in ("rule", "optimized"):
            if layout.sim_path(source, strategy, "csv").exists():
                pairs.append((source, strategy))
    return pairs


_PAIRS = _pairs()


def _get(url: str) -> dict:
    with urllib.request.urlopen(url, timeout=30) as response:
        return json.loads(response.read().decode("utf-8"))


def _post(url: str, payload: dict) -> dict:
    request = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        return json.loads(response.read().decode("utf-8"))


@pytest.fixture(scope="module")
def live_server():
    if not _PAIRS:
        pytest.skip("no simulation artefacts on disk; run `bike-agent repro` first")
    config, layout = load_config(), Layout()
    server = make_server(config, layout, port=0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}", config, layout
    finally:
        server.shutdown()
        server.server_close()


# -- F9-1 -------------------------------------------------------------------
def test_serve_check_mode_exits_zero_without_binding_a_port():
    result = subprocess.run(
        [sys.executable, "-m", "bike_agent.cli", "serve", "--check"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=300,
    )
    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    assert payload["ok"] is True
    assert payload["checks"]


# -- F9-6 -------------------------------------------------------------------
def test_server_binds_an_ephemeral_port(live_server):
    base, _, _ = live_server
    port = int(base.rsplit(":", 1)[1])
    assert port != 0 and port != 8000
    assert _get(f"{base}/api/health")["status"] == "ok"


# -- F9-2 -------------------------------------------------------------------
@pytest.mark.parametrize(("source", "strategy"), _PAIRS or [pytest.param("none", "none")])
def test_every_index_frame_summary_equals_the_csv_row(live_server, source, strategy):
    """Every bucket, exact equality -- not a sample, not a tolerance."""
    if source == "none":
        pytest.skip("no simulation artefacts on disk")
    base, _, layout = live_server
    index = _get(f"{base}/api/replay/index?source={source}&strategy={strategy}")
    steps = pd.read_csv(layout.sim_path(source, strategy, "csv"))
    steps["time_bucket"] = pd.to_datetime(steps["time_bucket"])

    assert index["n_frames"] == len(steps)
    assert [frame["time_bucket"] for frame in index["frames"]] == [
        ts.strftime("%Y-%m-%d %H:%M:%S") for ts in steps["time_bucket"]
    ]

    for frame in index["frames"]:
        payload = _get(
            f"{base}/api/replay/frame?source={source}&strategy={strategy}"
            f"&time_bucket={urllib.parse.quote(frame['time_bucket'])}"
        )
        row = steps.loc[steps["time_bucket"] == pd.Timestamp(frame["time_bucket"])].iloc[0]
        for field in (
            "borrow_requests",
            "borrow_met",
            "unmet_requests",
            "returns",
            "returns_applied",
            "returns_overflow",
            "bikes_moved",
            "distance_bike_km",
            "inventory_start",
            "inventory_end",
        ):
            assert payload["summary"][field] == row[field], f"{source}/{strategy} {frame['time_bucket']} {field}"
        assert len(payload["cells"]) == int(
            len(pd.read_parquet(layout.replay_path(source, strategy))["cell_id"].unique())
        )


# -- F9-3 -------------------------------------------------------------------
@pytest.mark.parametrize(("source", "strategy"), _PAIRS or [pytest.param("none", "none")])
def test_replay_frame_moves_come_from_the_moves_artifact(live_server, source, strategy):
    if source == "none":
        pytest.skip("no simulation artefacts on disk")
    base, _, layout = live_server
    records = json.loads((layout.outputs / f"sim_moves.{strategy}.{source}.json").read_text(encoding="utf-8"))
    index = _get(f"{base}/api/replay/index?source={source}&strategy={strategy}")
    for frame in index["frames"]:
        payload = _get(
            f"{base}/api/replay/frame?source={source}&strategy={strategy}"
            f"&time_bucket={urllib.parse.quote(frame['time_bucket'])}"
        )
        expected = [r for r in records if pd.Timestamp(r["time_bucket"]) == pd.Timestamp(frame["time_bucket"])]
        assert payload["moves"] == expected
        assert payload["n_moves"] == len(expected)


def test_tool_endpoints_are_field_equal_to_the_handlers(live_server):
    """F9-3: the facade must not invent a second contract."""
    base, config, layout = live_server
    source = next(iter(config.get("sources", {})), "xm")
    bucket = _get(f"{base}/api/replay/index?source={source}&strategy=rule")["frames"][0]["time_bucket"]

    served = _get(f"{base}/api/tools/forecast?source={source}&time_bucket={urllib.parse.quote(bucket)}")
    direct = handlers.get_forecast(config, layout, source=source, time_bucket=bucket)
    assert served == direct

    served = _get(f"{base}/api/tools/risk?source={source}&direction=replenish&top_k=5")
    direct = handlers.get_risk_ranking(config, layout, direction="replenish", top_k=5, source=source)
    assert served == direct

    served = _post(f"{base}/api/tools/simulate", {"source": source, "strategy": "rule"})
    direct = handlers.run_simulation(config, layout, strategy="rule", source=source)
    assert served == direct


# -- F9-5 -------------------------------------------------------------------
def _partial_layout(tmp_path: Path) -> Layout:
    """Only the `rule` strategy ran; `optimized` must not be faked."""
    layout = Layout(tmp_path)
    layout.ensure()
    config = load_config()
    cell_size = int(source_config(config, "xm")["cell_size_m"])
    pd.DataFrame(
        {"cell_id": ["A", "B"], "ix": [0, 1], "iy": [0, 0], "lon": [118.1, 118.2], "lat": [24.5, 24.5]}
    ).to_parquet(layout.cells_path("xm", cell_size), index=False)
    bucket = pd.Timestamp("2020-12-21 06:00")
    pd.DataFrame(
        [
            {
                "time_bucket": bucket,
                "borrow_requests": 3,
                "borrow_met": 3,
                "unmet_requests": 0,
                "returns": 1,
                "returns_applied": 1,
                "returns_overflow": 0,
                "bikes_moved": 1,
                "distance_bike_km": 0.5,
                "inventory_start": 4,
                "inventory_end": 2,
            }
        ]
    ).to_csv(layout.sim_path("xm", "rule", "csv"), index=False)
    pd.DataFrame(
        [
            {
                "time_bucket": bucket,
                "cell_id": "A",
                "inventory_start": 2,
                "borrow_requests": 3,
                "borrow_met": 2,
                "unmet_requests": 1,
                "returns": 0,
                "returns_applied": 0,
                "returns_overflow": 0,
                "bikes_moved": 1,
                "inventory_end": 0,
            },
            {
                "time_bucket": bucket,
                "cell_id": "B",
                "inventory_start": 2,
                "borrow_requests": 0,
                "borrow_met": 0,
                "unmet_requests": 0,
                "returns": 1,
                "returns_applied": 1,
                "returns_overflow": 0,
                "bikes_moved": 0,
                "inventory_end": 2,
            },
        ]
    ).to_parquet(layout.replay_path("xm", "rule"), index=False)
    (layout.outputs / "sim_moves.rule.xm.json").write_text("[]", encoding="utf-8")
    return layout


def test_malformed_inputs_are_client_errors(live_server):
    """F9-5: bad input is a clear 400, not a crash or an empty success."""
    base, _, _ = live_server
    for path in (
        "/api/replay/frame?source=xm&strategy=rule&time_bucket=not-a-date",
        "/api/replay/frame?source=xm&strategy=rule",
        "/api/replay/index?source=xm&strategy=nope",
        "/api/replay/index?source=nope&strategy=rule",
        "/api/tools/risk?source=xm&direction=sideways",
        "/api/tools/risk?source=xm&top_k=abc",
        "/api/does-not-exist",
    ):
        with pytest.raises(urllib.error.HTTPError) as excinfo:
            urllib.request.urlopen(f"{base}{path}", timeout=30)
        assert excinfo.value.code == 400 or excinfo.value.code == 404, path
        body = json.loads(excinfo.value.read().decode("utf-8"))
        assert body["error"], path
        assert "error" in body and "kind" in body


def test_missing_artifact_is_an_explicit_error_not_an_empty_object(tmp_path):
    layout = _partial_layout(tmp_path)
    config = load_config()

    # The index still lists what exists, and says which strategies are missing.
    listing = list_sources(config, layout)
    xm = next(entry for entry in listing["sources"] if entry["source"] == "xm")
    assert xm["strategies"] == ["rule"]
    assert xm["available"] is True
    sh = next(entry for entry in listing["sources"] if entry["source"] == "sh")
    assert sh["available"] is False and sh["missing"]

    with pytest.raises(ServiceError) as excinfo:
        replay_index(config, layout, "xm", "optimized")
    assert "optimized" in str(excinfo.value)

    server = make_server(config, layout, port=0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        url = f"http://127.0.0.1:{server.server_address[1]}/api/replay/index?source=xm&strategy=optimized"
        with pytest.raises(urllib.error.HTTPError) as http_error:
            urllib.request.urlopen(url, timeout=30)
        assert http_error.value.code == 404
        body = json.loads(http_error.value.read().decode("utf-8"))
        assert body["error"] and body["kind"] == "ServiceError"
    finally:
        server.shutdown()
        server.server_close()


# -- F9-4 -------------------------------------------------------------------
_LLM_BLOCKER = r"""
import sys, importlib.abc

BLOCKED = {
    "openai", "anthropic", "langchain", "langchain_core", "llama_index",
    "transformers", "cohere", "mistralai", "vertexai", "google.generativeai",
}

class Blocker(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname in BLOCKED or fullname.split(".")[0] in BLOCKED:
            raise ImportError(f"LLM SDK {fullname!r} is unavailable in this environment")
        return None

sys.meta_path.insert(0, Blocker())

from bike_agent.config import Layout, load_config
from bike_agent.serve import check, replay_frame, replay_index, tool_risk
from bike_agent.tools import handlers

config, layout = load_config(), Layout()
source = next(iter(config["sources"]))
index = replay_index(config, layout, source, "rule")
frame = replay_frame(config, layout, source, "rule", index["frames"][0]["time_bucket"])
assert frame["summary"]["time_bucket"] == index["frames"][0]["time_bucket"]
assert tool_risk(config, layout, "replenish", 3, source) == handlers.get_risk_ranking(
    config, layout, direction="replenish", top_k=3, source=source
)
assert check(config, layout)["ok"]
leaked = sorted(name for name in sys.modules if name.split(".")[0] in BLOCKED)
assert not leaked, leaked
print("OK")
"""


def test_service_works_without_any_llm_sdk():
    """F9-4: import-blocking every LLM SDK must not disturb the service."""
    if not _PAIRS:
        pytest.skip("no simulation artefacts on disk")
    result = subprocess.run(
        [sys.executable, "-c", _LLM_BLOCKER],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=300,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip().endswith("OK")


def test_bad_arguments_are_400_not_404(live_server):
    """A bad parameter is the caller's fault; a missing artefact is not."""
    base, _, _ = live_server
    for path in (
        "/api/tools/risk?source=xm&direction=sideways",
        "/api/tools/risk?source=xm&top_k=0",
        "/api/tools/risk?source=xm&top_k=-3",
        "/api/replay/index?source=xm&strategy=sideways",
    ):
        with pytest.raises(urllib.error.HTTPError) as excinfo:
            urllib.request.urlopen(f"{base}{path}", timeout=30)
        assert excinfo.value.code == 400, f"{path} -> {excinfo.value.code}"


def test_malformed_body_is_a_client_error(live_server):
    base, _, _ = live_server
    for payload in (b"null", b"[]", b'"text"', b"{not json"):
        request = urllib.request.Request(
            f"{base}/api/tools/simulate",
            data=payload,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with pytest.raises(urllib.error.HTTPError) as excinfo:
            urllib.request.urlopen(request, timeout=30)
        assert excinfo.value.code == 400, f"{payload!r} -> {excinfo.value.code}"


def test_availability_requires_the_replay_file(tmp_path):
    """Step totals alone cannot serve a frame; the source must not claim to be available."""
    layout = _partial_layout(tmp_path)
    config = load_config()
    layout.replay_path("xm", "rule").unlink()

    entry = next(e for e in list_sources(config, layout)["sources"] if e["source"] == "xm")
    assert entry["strategies"] == ["rule"]
    assert entry["frames_available"] == []
    assert entry["available"] is False
    assert any("replay" in missing for missing in entry["missing"])
