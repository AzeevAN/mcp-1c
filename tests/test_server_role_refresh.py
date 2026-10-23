"""Поток синхронизации и уведомления каталога MCP."""

import asyncio
from threading import get_ident
from types import SimpleNamespace

import pytest
from mcp.server.subscriptions import ToolsListChanged

from mcp1c.registry import Registry
from test_role_access_stage9 import _server


@pytest.mark.parametrize('changed', [False, True], ids=['unchanged', 'changed'])
def test_refresh_role_tools_синхронизирует_в_worker_и_публикует_из_event_loop(tmp_path, monkeypatch, changed):
    # Настоящий server/configure выполняется до установки измерительных швов:
    # начальная синхронная настройка каталога здесь не является async-refresh.
    server = _server(Registry(tmp_path / 'data'), tmp_path)
    sync_threads = []
    published = []

    def sync_role_tools():
        sync_threads.append(get_ident())
        return changed

    async def publish(event):
        published.append((get_ident(), event))

    monkeypatch.setattr(server, '_sync_role_tools', sync_role_tools)
    monkeypatch.setattr(server, '_role_subscriptions', SimpleNamespace(publish=publish))

    async def refresh():
        loop_thread = get_ident()
        result = await server.refresh_role_tools()
        assert result is changed
        assert len(sync_threads) == 1
        assert sync_threads[0] != loop_thread
        if changed:
            assert len(published) == 1
            publish_thread, event = published[0]
            assert publish_thread == loop_thread
            assert isinstance(event, ToolsListChanged)
        else:
            assert published == []

    asyncio.run(refresh())
