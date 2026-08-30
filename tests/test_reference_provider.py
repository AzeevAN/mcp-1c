"""Опциональная общая справка не влияет на основной Registry и MCP."""

from __future__ import annotations

import asyncio
import json
import sqlite3

import anyio
import pytest
from mcp import ClientSession
from mcp.shared.memory import create_client_server_memory_streams

from mcp1c.reference_provider import ReferenceService
from mcp1c.registry import Registry
from mcp1c.server import build_server

from reference_fixture import build_reference_database


@pytest.fixture
def anyio_backend():
    return "asyncio"


def test_нет_базы_оставляет_основной_mcp_рабочим(tmp_path):
    service = ReferenceService.discover(tmp_path, allow_unsigned=True)

    assert service.status.state == "missing"
    assert service.provider is None
    server = build_server(Registry(tmp_path), reference=service)
    names = [tool.name for tool in asyncio.run(server.list_tools())]
    assert len(names) == 11
    assert "search_reference" not in names
    assert "get_reference" not in names


def test_явное_off_отключает_адаптер_без_поиска_файла(tmp_path):
    service = ReferenceService.discover(tmp_path, database_path="off")

    assert service.status.state == "disabled"
    assert service.provider is None
    assert service.status.message == "Локальная общая справка выключена."


def test_неподписанная_база_по_умолчанию_не_подключается(tmp_path):
    database = tmp_path / "reference" / "reference.sqlite3"
    database.parent.mkdir()
    build_reference_database(database)

    service = ReferenceService.discover(tmp_path)

    assert service.status.state == "untrusted"
    assert service.provider is None


def test_ошибка_будущего_верификатора_остаётся_fail_soft(tmp_path):
    class BrokenVerifier:
        def verify(self, database, file_sha256):
            del database, file_sha256
            raise RuntimeError("секретная внутренняя причина")

    database = tmp_path / "reference" / "reference.sqlite3"
    database.parent.mkdir()
    build_reference_database(database)

    service = ReferenceService.discover(tmp_path, verifier=BrokenVerifier())

    assert service.status.state == "untrusted"
    assert service.status.signature == "verification-error"
    assert service.status.message == "Проверку подписи не удалось выполнить."
    assert service.provider is None


def test_валидная_экспериментальная_база_добавляет_ровно_две_ручки(tmp_path):
    database = tmp_path / "reference" / "reference.sqlite3"
    database.parent.mkdir()
    build_reference_database(database)

    service = ReferenceService.discover(tmp_path, allow_unsigned=True)
    server = build_server(Registry(tmp_path), reference=service)

    assert service.status.state == "ready"
    assert service.status.signature == "unsigned-experimental"
    tools = asyncio.run(server.list_tools())
    names = [tool.name for tool in tools]
    assert names[-2:] == ["search_reference", "get_reference"]
    assert len(names) == 13
    search = next(tool for tool in tools if tool.name == "search_reference")
    assert set(search.input_schema["properties"]) == {
        "query", "domain", "kind", "platform", "include_explicit",
        "include_hidden", "limit",
    }
    limit = search.input_schema["properties"]["limit"]
    assert limit["default"] == 10
    assert limit["minimum"] == 1
    assert limit["maximum"] == 50


def test_поиск_и_чтение_работают_по_schema_v1(tmp_path):
    database = tmp_path / "reference" / "reference.sqlite3"
    database.parent.mkdir()
    build_reference_database(database)
    service = ReferenceService.discover(tmp_path, allow_unsigned=True)
    provider = service.provider
    assert provider is not None

    found = provider.search("показать образец", platform="8.3.20")
    assert found["results"][0]["id"] == "bsl/Example"
    assert found["results"][0]["availability"]["status"] == "available"

    card = provider.get("bsl/Example", max_chars=256)
    assert card["card"]["id"] == "bsl/Example"
    assert "Синтетическое описание" in card["content"]


def test_повреждённая_и_несовместимая_базы_не_роняют_mcp(tmp_path):
    database = tmp_path / "reference" / "reference.sqlite3"
    database.parent.mkdir()
    database.write_bytes(b"not sqlite")
    corrupt = ReferenceService.discover(tmp_path, allow_unsigned=True)
    assert corrupt.status.state == "corrupt"
    assert corrupt.provider is None

    database.unlink()
    connection = sqlite3.connect(database)
    connection.execute("CREATE TABLE meta(key TEXT PRIMARY KEY, value TEXT NOT NULL)")
    connection.execute("INSERT INTO meta VALUES ('schema_version', '2')")
    connection.commit()
    connection.close()
    incompatible = ReferenceService.discover(tmp_path, allow_unsigned=True)
    assert incompatible.status.state == "incompatible"
    assert incompatible.provider is None


def test_индекс_восстанавливается_из_кэша_и_перестраивается_после_замены(tmp_path):
    database = tmp_path / "reference" / "reference.sqlite3"
    database.parent.mkdir()
    build_reference_database(database)

    first = ReferenceService.discover(tmp_path, allow_unsigned=True)
    assert first.status.index_cache == "rebuilt"
    assert (tmp_path / "index" / "reference" / "reference.search").is_file()
    first.close()

    second = ReferenceService.discover(tmp_path, allow_unsigned=True)
    assert second.status.index_cache == "hit"
    second.close()

    database.unlink()
    build_reference_database(database, body="Изменённое синтетическое описание.")
    replaced = ReferenceService.discover(tmp_path, allow_unsigned=True)
    assert replaced.status.index_cache == "rebuilt"
    assert "Изменённое" in replaced.provider.get("bsl/Example")["content"]


def test_обычный_startup_registry_не_удаляет_кэш_общей_справки(tmp_path):
    database = tmp_path / "reference" / "reference.sqlite3"
    database.parent.mkdir()
    build_reference_database(database)
    first = ReferenceService.discover(tmp_path, allow_unsigned=True)
    cache = tmp_path / "index" / "reference" / "reference.search"
    assert cache.is_file()
    first.close()

    Registry(tmp_path).startup()
    restarted = ReferenceService.discover(tmp_path, allow_unsigned=True)

    assert cache.is_file()
    assert restarted.status.index_cache == "hit"


def test_битое_json_поле_отклоняется_при_старте_до_tools_call(tmp_path):
    from mcp1c.reference_provider import calculate_logical_hash

    database = tmp_path / "reference" / "reference.sqlite3"
    database.parent.mkdir()
    build_reference_database(database)
    connection = sqlite3.connect(database)
    connection.row_factory = sqlite3.Row
    connection.execute("UPDATE parameters SET types_json='['")
    digest = calculate_logical_hash(connection)
    connection.execute(
        "UPDATE meta SET value=? WHERE key='content_sha256'", (digest,)
    )
    connection.commit()
    connection.close()

    service = ReferenceService.discover(tmp_path, allow_unsigned=True)

    assert service.status.state == "corrupt"
    assert service.status.message == "JSON-поля канонической базы повреждены."
    assert service.provider is None


def test_битый_кэш_расходный_и_не_мешает_старту(tmp_path):
    database = tmp_path / "reference" / "reference.sqlite3"
    database.parent.mkdir()
    build_reference_database(database)
    first = ReferenceService.discover(tmp_path, allow_unsigned=True)
    first.close()
    cache = tmp_path / "index" / "reference" / "reference.search"
    cache.write_bytes(b"broken cache")

    recovered = ReferenceService.discover(tmp_path, allow_unsigned=True)

    assert recovered.status.state == "ready"
    assert recovered.status.index_cache == "rebuilt"
    assert recovered.provider.search("образец")["results"][0]["id"] == "bsl/Example"


@pytest.mark.anyio
async def test_reference_tools_работают_через_полную_mcp_сессию(tmp_path):
    database = tmp_path / "reference" / "reference.sqlite3"
    database.parent.mkdir()
    build_reference_database(database)
    service = ReferenceService.discover(tmp_path, allow_unsigned=True)
    server = build_server(Registry(tmp_path), reference=service)

    async with create_client_server_memory_streams() as (client_streams, server_streams):
        async with anyio.create_task_group() as tasks:
            tasks.start_soon(
                server._lowlevel_server.run,
                *server_streams,
                server._lowlevel_server.create_initialization_options(),
            )
            try:
                async with ClientSession(*client_streams) as session:
                    await session.initialize()
                    found = await session.call_tool(
                        "search_reference",
                        {"query": "показать образец", "platform": "8.3.20"},
                    )
                    card = await session.call_tool(
                        "get_reference", {"item_id": "bsl/Example"}
                    )
                    missing = await session.call_tool(
                        "get_reference", {"item_id": "missing"}
                    )
            finally:
                tasks.cancel_scope.cancel()

    assert found.is_error is False
    assert json.loads(found.content[0].text)["results"][0]["id"] == "bsl/Example"
    assert card.is_error is False
    assert json.loads(card.content[0].text)["card"]["id"] == "bsl/Example"
    assert missing.is_error is True
    assert missing.structured_content == {"result": "Элемент 'missing' не найден."}
