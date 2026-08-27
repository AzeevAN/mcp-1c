"""Публичные варианты запуска MCP-сервера."""

from __future__ import annotations

import sys
from types import SimpleNamespace

import pytest

from mcp1c import server as server_module


def test_sse_транспорт_отклоняется_до_запуска_сервера(tmp_path, monkeypatch):
    """Устаревший SSE не должен оставаться скрытым незащищённым входом."""

    class FakeRegistry:
        configurations = [object()]

        def __init__(self, data):
            self.data = data

        def startup(self):
            return []

        def snapshot(self):
            return self

    class FakeServer:
        def run(self, **kwargs):
            return None

    monkeypatch.setattr(server_module, "Registry", FakeRegistry)
    monkeypatch.setattr(server_module, "build_server", lambda registry: FakeServer())

    with pytest.raises(SystemExit) as ошибка:
        server_module.main(
            ["--data", str(tmp_path), "--transport", "sse"]
        )

    assert ошибка.value.code == 2


def test_http_по_умолчанию_слушает_loopback_и_не_доверяет_proxy(
    tmp_path, monkeypatch
):
    class FakeRegistry:
        configurations = [object()]

        def __init__(self, data):
            self.data = data

        def startup(self):
            return []

        def snapshot(self):
            return self

    параметры = {}
    monkeypatch.setattr(server_module, "Registry", FakeRegistry)
    monkeypatch.setattr(server_module, "build_server", lambda registry: object())
    monkeypatch.setattr(
        server_module,
        "_run_streamable_http",
        lambda server, **kwargs: параметры.update(kwargs),
    )

    assert server_module.main(["--data", str(tmp_path)]) == 0
    assert параметры == {
        "host": "127.0.0.1",
        "port": 8000,
        "trust_proxy_headers": False,
    }


def test_uvicorn_доверяет_forwarded_headers_только_по_явному_флагу(monkeypatch):
    вызовы = []

    class FakeServer:
        def streamable_http_app(self, *, host):
            return ("app", host)

    monkeypatch.setitem(
        sys.modules,
        "uvicorn",
        SimpleNamespace(run=lambda app, **kwargs: вызовы.append((app, kwargs))),
    )
    monkeypatch.setattr(server_module, "mcp_guard", lambda app: ("guard", app))

    server_module._run_streamable_http(
        FakeServer(), host="127.0.0.1", port=8000, trust_proxy_headers=False
    )
    server_module._run_streamable_http(
        FakeServer(), host="0.0.0.0", port=8000, trust_proxy_headers=True
    )

    assert вызовы[0][1]["proxy_headers"] is False
    assert вызовы[0][1]["forwarded_allow_ips"] is None
    assert вызовы[1][1]["proxy_headers"] is True
    assert вызовы[1][1]["forwarded_allow_ips"] == "*"
