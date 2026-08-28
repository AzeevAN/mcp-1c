"""Публичные варианты запуска MCP-сервера."""

from __future__ import annotations

import errno
import sys
from types import SimpleNamespace

import pytest

from mcp1c import server as server_module


def test_проверка_каталога_создаёт_рабочие_подкаталоги(tmp_path):
    data_dir = tmp_path / "data"

    server_module.require_writable_data(data_dir)

    assert {
        path.name for path in data_dir.iterdir() if path.is_dir()
    } == {
        "bootstrap",
        "incoming",
        "sources",
        "index",
        "modules",
        "extensions",
        "logs",
    }
    assert not list(data_dir.rglob(".mcp1c-write-test-*"))


def test_проверка_каталога_объясняет_числового_владельца(
    tmp_path, monkeypatch
):
    data_dir = tmp_path / "data"
    original_open = server_module.os.open

    def denied(path, flags, mode=0o777, *, dir_fd=None):
        if str(path).startswith(".mcp1c-write-test-"):
            raise PermissionError(errno.EACCES, "Permission denied", str(path))
        return original_open(path, flags, mode, dir_fd=dir_fd)

    monkeypatch.setattr(server_module.os, "open", denied)
    monkeypatch.setattr(server_module.os, "geteuid", lambda: 10001)
    monkeypatch.setattr(server_module.os, "getegid", lambda: 10001)

    with pytest.raises(server_module.DataDirectoryError) as error:
        server_module.require_writable_data(data_dir)

    text = str(error.value)
    assert str(data_dir) in text
    assert "uid=10001" in text
    assert "gid=10001" in text
    assert "chown -R 10001:10001" in text
    assert "<MCP1C_DATA_DIR>" in text
    assert "chmod 777" in text


def test_проверка_каталога_не_пишет_через_символическую_ссылку(tmp_path):
    data_dir = tmp_path / "data"
    outside = tmp_path / "outside"
    data_dir.mkdir()
    outside.mkdir()
    (data_dir / "sources").symlink_to(outside, target_is_directory=True)

    with pytest.raises(server_module.DataDirectoryError) as error:
        server_module.require_writable_data(data_dir)

    assert str(data_dir / "sources") in str(error.value)
    assert not list(outside.iterdir())


def test_main_проверяет_запись_только_по_явному_флагу(tmp_path, monkeypatch):
    calls = []

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
    monkeypatch.setattr(
        server_module,
        "require_writable_data",
        lambda data: calls.append(data),
    )

    assert server_module.main(
        [
            "--data",
            str(tmp_path),
            "--transport",
            "stdio",
            "--require-writable-data",
        ]
    ) == 0
    assert calls == [str(tmp_path)]


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
