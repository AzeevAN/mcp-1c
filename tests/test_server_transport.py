"""Публичные варианты запуска MCP-сервера."""

from __future__ import annotations

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
