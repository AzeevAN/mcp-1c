"""Async-обработчики не ждут дисковых операций и замка Registry в event loop.

Один процесс обслуживает и MCP, и дашборд, и healthcheck контейнера. Если
обработчик синхронно пишет файл или ждёт `_lock`, который публикация держит
на fsync, замирают все запросы сразу, а контейнер получает провал health.
"""

from __future__ import annotations

import asyncio
import threading
import time
from pathlib import Path

from starlette.applications import Starlette

from conftest import build_configuration, write_export, живой_клиент
from mcp1c import dashboard_backend as dashboard
from mcp1c.dashboard_runtime import DASHBOARD_ON, routes
from mcp1c.registry import Registry
from mcp1c.server import build_server


async def _get(app, path: str) -> list[dict]:
    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "GET",
        "scheme": "http",
        "path": path,
        "raw_path": path.encode(),
        "query_string": b"",
        "root_path": "",
        "headers": [(b"host", b"testserver")],
        "client": ("127.0.0.1", 1),
        "server": ("testserver", 80),
    }
    sent: list[dict] = []

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message):
        sent.append(message)

    await app(scope, receive, send)
    return sent


def _loop_delay_while_lock_held(app, registry: Registry, path: str) -> tuple[float, int]:
    """Насколько задерживается event loop, пока другой поток держит `_lock`."""
    held = threading.Event()

    def holder():
        with registry._lock:
            held.set()
            time.sleep(1.0)

    async def scenario():
        thread = threading.Thread(target=holder)
        thread.start()
        held.wait(5)
        request = asyncio.create_task(_get(app, path))
        started = time.monotonic()
        await asyncio.sleep(0.05)
        delay = time.monotonic() - started
        sent = await asyncio.wait_for(request, 5)
        thread.join()
        status = next(m["status"] for m in sent if m["type"] == "http.response.start")
        return delay, status

    return asyncio.run(scenario())


def test_health_не_блокирует_loop_замком_registry(tmp_path, monkeypatch):
    monkeypatch.delenv("API_TOKEN", raising=False)
    registry = Registry(tmp_path / "data")
    app = Starlette(routes=build_server(registry)._custom_starlette_routes)

    delay, status = _loop_delay_while_lock_held(app, registry, "/health")

    assert status == 200
    assert delay < 0.5


def test_bootstrap_дашборда_не_блокирует_loop_замком_registry(tmp_path, monkeypatch):
    monkeypatch.delenv("API_TOKEN", raising=False)
    registry = Registry(tmp_path / "data")
    app = Starlette(routes=routes(registry, mode=DASHBOARD_ON))

    delay, status = _loop_delay_while_lock_held(
        app, registry, "/api/v1/dashboard/bootstrap"
    )

    assert status == 200
    assert delay < 0.5


def test_запись_загруженного_файла_идёт_не_в_event_loop(tmp_path, monkeypatch):
    monkeypatch.delenv("API_TOKEN", raising=False)
    monkeypatch.setenv("ADMIN_TOKEN", "admin-token")
    monkeypatch.setattr(dashboard, "JOBS", [])
    exports = tmp_path / "exports"
    exports.mkdir()
    payload = write_export(exports, build_configuration()).read_bytes()
    registry = Registry(tmp_path / "data")
    client = живой_клиент(Starlette(routes=routes(registry, mode=DASHBOARD_ON)))

    записи_в_loop: list[bool] = []
    настоящий_open = Path.open

    class Recorder:
        def __init__(self, handle):
            self._handle = handle

        def write(self, data):
            try:
                asyncio.get_running_loop()
                записи_в_loop.append(True)
            except RuntimeError:
                записи_в_loop.append(False)
            return self._handle.write(data)

        def __getattr__(self, name):
            return getattr(self._handle, name)

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return self._handle.__exit__(*exc)

    def open_recording(path, mode="r", *args, **kwargs):
        handle = настоящий_open(path, mode, *args, **kwargs)
        if path.name == "загрузка.zip" and "w" in mode:
            return Recorder(handle)
        return handle

    monkeypatch.setattr(Path, "open", open_recording)

    response = client.post(
        "/api/v1/sources/upload",
        files={"file": ("загрузка.zip", payload)},
        headers={"x-api-token": "admin-token"},
    )

    assert response.status_code == 202
    assert записи_в_loop
    assert not any(записи_в_loop)


def test_журнал_покрытия_сверяет_активацию_и_снимок_не_в_event_loop(
    корень_кода, реестр_из_кода, monkeypatch
):
    monkeypatch.delenv("API_TOKEN", raising=False)
    registry = реестр_из_кода(корень_кода, name="Пример")
    client = живой_клиент(Starlette(routes=routes(registry, mode=DASHBOARD_ON)))
    вызовы: list[tuple[str, bool]] = []

    def record(name, original):
        def wrapper(*args, **kwargs):
            try:
                asyncio.get_running_loop()
                вызовы.append((name, True))
            except RuntimeError:
                вызовы.append((name, False))
            return original(*args, **kwargs)
        return wrapper

    monkeypatch.setattr(
        registry, "active_activation", record("activation", registry.active_activation)
    )
    monkeypatch.setattr(
        registry, "snapshot_is_current", record("current", registry.snapshot_is_current)
    )

    client.get("/api/v1/sources/coverage?source_id=Пример:modules")

    assert {name for name, _ in вызовы} >= {"activation", "current"}
    assert not any(in_loop for _, in_loop in вызовы)
