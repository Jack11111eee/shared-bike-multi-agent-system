"""A minimal Chrome DevTools Protocol client, used only by the export tooling.

``scripts/capture_presentation.py`` needs three things a one-shot
``--dump-dom`` run cannot give it: click something, read the resulting state,
and photograph what the browser actually painted.  That is what CDP is for, and
talking to it directly keeps the dependency list honest -- the site and the
pipeline stay zero-dependency, while the *export* path needs Chrome plus
``websockets`` and nothing else.

The client is deliberately small: connect to one page target, send commands,
await replies, and collect the events a caller subscribed to.  Anything more
belongs in a real automation library, which is exactly what this project does
not want to depend on at runtime.
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import socket
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Any, Callable

CANDIDATES = (
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    "/Applications/Chromium.app/Contents/MacOS/Chromium",
    "/usr/bin/google-chrome",
    "/usr/bin/chromium",
    "/usr/bin/chromium-browser",
    "google-chrome",
    "chromium",
)


class ToolMissing(RuntimeError):
    """Raised when the export path cannot run; the message says how to fix it."""


def find_chrome() -> str | None:
    for candidate in CANDIDATES:
        found = shutil.which(candidate)
        if found:
            return found
    return None


def find_ffmpeg() -> str | None:
    return shutil.which("ffmpeg")


def require_chrome() -> str:
    chrome = find_chrome()
    if chrome is None:
        raise ToolMissing(
            "找不到 Chrome/Chromium。准备步骤：\n"
            "  1. 安装 Google Chrome 或 Chromium（本机已验证：/Applications/Google Chrome.app）\n"
            "  2. 重新运行 `python3 scripts/capture_presentation.py --check` 确认版本\n"
            "主站与 `bike-agent replay` 不需要浏览器，只有截图/录屏导出需要。"
        )
    return chrome


def require_ffmpeg() -> str:
    ffmpeg = find_ffmpeg()
    if ffmpeg is None:
        raise ToolMissing(
            "找不到 ffmpeg。准备步骤：\n"
            "  1. macOS: `brew install ffmpeg`；Linux: 用发行版包管理器安装 ffmpeg\n"
            "  2. 重新运行 `python3 scripts/capture_presentation.py --check`\n"
            "录屏只在导出路径使用 ffmpeg，主站与主流水线不依赖它。"
        )
    return ffmpeg


def tool_versions() -> dict[str, Any]:
    """Versions of everything the export path uses, for the media manifest."""
    info: dict[str, Any] = {"python": f"{os.sys.version_info.major}.{os.sys.version_info.minor}.{os.sys.version_info.micro}"}
    chrome = find_chrome()
    if chrome:
        try:
            out = subprocess.run([chrome, "--version"], capture_output=True, text=True, timeout=30, check=False)
            info["chrome"] = {"path": chrome, "version": (out.stdout or out.stderr).strip()}
        except Exception as error:  # pragma: no cover - environment dependent
            info["chrome"] = {"path": chrome, "version": f"unknown ({error})"}
    else:
        info["chrome"] = None
    ffmpeg = find_ffmpeg()
    if ffmpeg:
        try:
            out = subprocess.run([ffmpeg, "-version"], capture_output=True, text=True, timeout=30, check=False)
            info["ffmpeg"] = {"path": ffmpeg, "version": (out.stdout or "").splitlines()[0] if out.stdout else "unknown"}
        except Exception as error:  # pragma: no cover
            info["ffmpeg"] = {"path": ffmpeg, "version": f"unknown ({error})"}
    else:
        info["ffmpeg"] = None
    try:
        import websockets

        info["websockets"] = websockets.__version__
    except Exception:  # pragma: no cover
        info["websockets"] = None
    return info


class Browser:
    """One headless Chrome with one page target."""

    def __init__(self, width: int = 1600, height: int = 900, chrome: str | None = None, profile: Path | None = None):
        self.width = width
        self.height = height
        self.chrome = chrome or require_chrome()
        self._profile = profile
        self._process: subprocess.Popen | None = None
        self._tmp: tempfile.TemporaryDirectory | None = None
        self._ws = None
        self._next_id = 0
        self._pending: dict[int, asyncio.Future] = {}
        self._events: dict[str, list[Callable[[dict], None]]] = {}
        self._reader: asyncio.Task | None = None

    # -- lifecycle ---------------------------------------------------------
    async def __aenter__(self) -> "Browser":
        if self._profile is None:
            # Chrome keeps writing into its profile for a moment after SIGTERM;
            # ignore_cleanup_errors leaves a stray temp dir rather than failing
            # an otherwise successful capture.
            self._tmp = tempfile.TemporaryDirectory(prefix="bike-capture-", ignore_cleanup_errors=True)
            self._profile = Path(self._tmp.name)
        # A fixed free port rather than `--remote-debugging-port=0`: on macOS
        # Chrome 156 does not write DevToolsActivePort for headless=new, so the
        # port has to be chosen up front and probed over HTTP.
        self._port = free_port()
        self._process = subprocess.Popen(
            [
                self.chrome,
                "--headless=new",
                "--disable-gpu",
                "--no-first-run",
                "--no-default-browser-check",
                "--disable-extensions",
                "--hide-scrollbars",
                "--force-device-scale-factor=1",
                f"--window-size={self.width},{self.height}",
                f"--user-data-dir={self._profile}",
                f"--remote-debugging-port={self._port}",
                "about:blank",
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        target = await self._wait_for_page(self._port)
        import websockets

        self._ws = await websockets.connect(target, max_size=64 * 1024 * 1024)
        self._reader = asyncio.create_task(self._read_loop())
        await self.call("Page.enable")
        await self.call("Runtime.enable")
        await self.call("Emulation.setDeviceMetricsOverride", {
            "width": self.width, "height": self.height, "deviceScaleFactor": 1, "mobile": False,
        })
        return self

    async def __aexit__(self, *exc) -> None:
        try:
            if self._ws is not None:
                await self._ws.close()
        except Exception:
            pass
        if self._reader is not None:
            self._reader.cancel()
        if self._process is not None:
            self._process.terminate()
            try:
                self._process.wait(timeout=15)
            except subprocess.TimeoutExpired:  # pragma: no cover
                self._process.kill()
                try:
                    self._process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    pass
            # Let Chrome finish flushing its profile before the temp dir goes.
            await asyncio.sleep(0.5)
        if self._tmp is not None:
            self._tmp.cleanup()

    async def _wait_for_port(self, timeout: float = 45.0) -> int:
        port_file = self._profile / "DevToolsActivePort"
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if port_file.exists():
                lines = port_file.read_text(encoding="utf-8").splitlines()
                if lines and lines[0].strip().isdigit():
                    return int(lines[0].strip())
            await asyncio.sleep(0.05)
        raise ToolMissing(f"Chrome did not open a debugging port within {timeout}s (profile {self._profile})")

    async def _wait_for_page(self, port: int, timeout: float = 60.0) -> str:
        """Poll the DevTools endpoint until it answers.

        On a loaded machine Chrome can take ten seconds or more to open the port,
        and a connection refused during that window is normal, not fatal -- so the
        wait is on the *endpoint*, with the process checked so a dead browser
        fails fast instead of burning the whole timeout.
        """
        import urllib.request

        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                with urllib.request.urlopen(f"http://127.0.0.1:{port}/json/list", timeout=2) as response:
                    targets = json.loads(response.read().decode("utf-8"))
                for target in targets:
                    if target.get("type") == "page" and target.get("webSocketDebuggerUrl"):
                        return target["webSocketDebuggerUrl"]
            except Exception:
                pass
            if self._process is not None and self._process.poll() is not None:
                raise ToolMissing(
                    f"Chrome exited with code {self._process.returncode} before opening a page target"
                )
            await asyncio.sleep(0.2)
        raise ToolMissing(f"Chrome exposed no page target on port {port} within {timeout}s")

    # -- protocol ----------------------------------------------------------
    async def _read_loop(self) -> None:
        assert self._ws is not None
        try:
            async for message in self._ws:
                payload = json.loads(message)
                if "id" in payload and payload["id"] in self._pending:
                    future = self._pending.pop(payload["id"])
                    if not future.done():
                        if "error" in payload:
                            future.set_exception(RuntimeError(payload["error"]))
                        else:
                            future.set_result(payload.get("result", {}))
                elif "method" in payload:
                    for handler in self._events.get(payload["method"], []):
                        handler(payload.get("params", {}))
        except Exception:  # pragma: no cover - connection teardown
            for future in self._pending.values():
                if not future.done():
                    future.set_exception(RuntimeError("devtools connection closed"))
            self._pending.clear()

    def on(self, method: str, handler: Callable[[dict], None]) -> None:
        self._events.setdefault(method, []).append(handler)

    async def call(self, method: str, params: dict | None = None, timeout: float = 60.0) -> dict:
        assert self._ws is not None
        self._next_id += 1
        message_id = self._next_id
        future: asyncio.Future = asyncio.get_running_loop().create_future()
        self._pending[message_id] = future
        await self._ws.send(json.dumps({"id": message_id, "method": method, "params": params or {}}))
        return await asyncio.wait_for(future, timeout=timeout)

    async def goto(self, url: str, *, settle: float = 0.6) -> None:
        await self.call("Page.navigate", {"url": url})
        await self.wait_ready()
        await asyncio.sleep(settle)

    async def wait_ready(self, timeout: float = 30.0) -> None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            state = await self.evaluate("document.readyState")
            if state == "complete":
                return
            await asyncio.sleep(0.05)
        raise TimeoutError("page did not reach readyState=complete")

    async def evaluate(self, expression: str, timeout: float = 30.0) -> Any:
        result = await self.call(
            "Runtime.evaluate",
            {"expression": expression, "returnByValue": True, "awaitPromise": True},
            timeout=timeout,
        )
        if "exceptionDetails" in result:
            raise RuntimeError(json.dumps(result["exceptionDetails"], ensure_ascii=False)[:800])
        return result.get("result", {}).get("value")

    async def click(self, selector: str) -> bool:
        """Click the first match, or report that there was none.

        SVG elements have no ``click()`` method, so the fallback dispatches the
        same bubbling event a pointer would produce -- which is exactly what the
        pages' delegated handlers listen for.
        """
        script = (
            "(function(){var el=document.querySelector(%s);"
            "if(!el)return false;"
            "if(typeof el.click==='function'){el.click();return true;}"
            "el.dispatchEvent(new MouseEvent('click',{bubbles:true,cancelable:true}));return true;})()"
            % json.dumps(selector)
        )
        return bool(await self.evaluate(script))

    async def screenshot(self, path: Path, *, clip: dict | None = None) -> None:
        params: dict[str, Any] = {"format": "png", "captureBeyondViewport": False}
        if clip:
            params["clip"] = clip
        result = await self.call("Page.captureScreenshot", params)
        import base64

        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(base64.b64decode(result["data"]))


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]
