"""HTTP boundary and export behavior of the new workbench."""
from __future__ import annotations

import json
import threading
from urllib.error import HTTPError
from urllib.request import urlopen

import pytest

from bike_agent.serve.app import make_server
from bike_agent.presentation.console import publish_run
from test_llm_runtime import execute, llm_env


def test_console_api_exports_deep_links_and_boundaries(llm_env, monkeypatch):
    runtime, outcome = execute(llm_env)
    config, layout = llm_env
    monkeypatch.setenv("AMAP_KEY", "web-map-test-key")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "private-model-test-secret")
    publish_run(config, layout, outcome["run_id"], include_tests=True)
    server = make_server(config, layout, port=0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = "http://127.0.0.1:" + str(server.server_address[1])
    rid = outcome["run_id"]
    try:
        with urlopen(base + "/console/") as response:
            assert "城市调度决策台" in response.read().decode()
        with urlopen(base + "/console/map-config.js") as response:
            script = response.read().decode()
            assert "web-map-test-key" in script
            assert "private-model-test-secret" not in script
            assert response.headers["Cache-Control"] == "no-store"
        with urlopen(base + "/console/run/" + rid) as response:
            assert '<base href="/console/">' in response.read().decode()
        with urlopen(base + "/api/v2/runs/" + rid) as response:
            view = json.load(response)
            assert view["identity"]["run_id"] == rid
        with urlopen(base + "/api/v2/runs/" + rid + "/events?after_seq=10") as response:
            assert all(e["sequence"] > 10 for e in json.load(response)["events"])
        with urlopen(base + "/api/v2/runs/" + rid + "/report") as response:
            assert response.headers.get_content_type() == "text/html"
            assert "<table>" in response.read().decode()
        for path in ["/console/exports/" + rid + "/dispatch-moves.csv",
                     "/api/v2/runs/" + rid + "/exports/dispatch-moves.csv"]:
            with urlopen(base + path) as response:
                assert response.headers.get_content_type() == "text/csv"
                assert runtime.candidates[-1]["result"]["result_id"] in response.read().decode("utf-8-sig")
        for path in ["/api/v2/runs/" + rid + "/exports/model_calls.jsonl",
                     "/console/%2e%2e/pyproject.toml"]:
            with pytest.raises(HTTPError) as error:
                urlopen(base + path)
            assert error.value.code in {400,403,404}
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
