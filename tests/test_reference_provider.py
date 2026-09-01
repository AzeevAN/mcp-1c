"""Опциональная общая справка не влияет на основной Registry и MCP."""

from __future__ import annotations

import asyncio
import json
import sqlite3

import anyio
import pytest
from mcp import ClientSession
from mcp.shared.memory import create_client_server_memory_streams

from mcp1c.reference_provider import ReferenceQueryError, ReferenceService
from mcp1c.registry import Registry
from mcp1c.server import INSTRUCTIONS, build_server

from reference_fixture import SyntheticReferenceSigner, build_reference_database


@pytest.fixture
def anyio_backend():
    return "asyncio"


def _signed_service(tmp_path, database=None, signer=None):
    signer = signer or SyntheticReferenceSigner.generate()
    if database is not None:
        signer.build(
            tmp_path / "reference" / "reference.mcp1cref",
            database,
        )
    return ReferenceService.discover(tmp_path, verifier=signer.verifier()), signer


def test_нет_базы_оставляет_основной_mcp_рабочим(tmp_path):
    service, _ = _signed_service(tmp_path)

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
    database = tmp_path / "reference" / "reference.mcp1cref"
    database.parent.mkdir()
    build_reference_database(database)

    service = ReferenceService.discover(tmp_path)

    assert service.status.state == "untrusted"
    assert service.provider is None


def test_ошибка_будущего_верификатора_остаётся_fail_soft(tmp_path):
    class BrokenVerifier:
        def verify(self, artifact, extraction_dir):
            del artifact, extraction_dir
            raise RuntimeError("секретная внутренняя причина")

    artifact = tmp_path / "reference" / "reference.mcp1cref"
    artifact.parent.mkdir()
    artifact.write_bytes(b"synthetic candidate")

    service = ReferenceService.discover(tmp_path, verifier=BrokenVerifier())

    assert service.status.state == "untrusted"
    assert service.status.signature == "verification-error"
    assert service.status.message == "Проверку подписи не удалось выполнить."
    assert service.provider is None


def test_валидная_экспериментальная_база_добавляет_ровно_две_ручки(tmp_path):
    database = build_reference_database(tmp_path / "source.sqlite3")

    service, _ = _signed_service(tmp_path, database)
    server = build_server(Registry(tmp_path), reference=service)

    assert service.status.state == "ready"
    assert service.status.signature == "ed25519"
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
    assert "не привязанной к Registry" in search.description
    assert "`query` — язык запросов" in search.input_schema["properties"]["domain"]["description"]
    assert "не влияет на текстовое совпадение" in search.input_schema["properties"]["platform"]["description"]
    assert "даже без целевой версии" in search.description
    assert "не угадывайте `id`" in search.input_schema["properties"]["query"]["description"]
    assert "общая справка, не зависящая" in INSTRUCTIONS
    assert "Известная версия появления" in INSTRUCTIONS
    assert "целевая версия только добавляет проверку применимости" in INSTRUCTIONS


def test_поиск_и_чтение_работают_по_schema_v1(tmp_path):
    database = build_reference_database(tmp_path / "source.sqlite3")
    service, _ = _signed_service(tmp_path, database)
    provider = service.provider
    assert provider is not None

    found = provider.search("показать образец", platform="8.3.20")
    assert found["results"][0]["id"] == "bsl/Example"
    assert found["results"][0]["availability"]["status"] == "available"

    card = provider.get("bsl/Example", max_chars=256)
    assert card["card"]["id"] == "bsl/Example"
    assert "Синтетическое описание" in card["content"]


def test_версия_появления_возвращается_и_без_целевой_платформы(tmp_path):
    database = build_reference_database(tmp_path / "source.sqlite3")
    service, _ = _signed_service(tmp_path, database)
    provider = service.provider
    assert provider is not None

    without_platform = provider.get("bsl/Example")["availability"]
    before = provider.get("bsl/Example", platform="8.3.9")["availability"]
    since = provider.get("bsl/Example", platform="8.3.10")["availability"]

    assert without_platform["status"] == "unknown"
    assert without_platform["platform"] is None
    assert without_platform["introduced"] == "8.3.10"
    assert without_platform["removed"] is None
    assert without_platform["known_present_in"] is None
    assert without_platform["reason"] == (
        "Подтверждена версия появления 8.3.10. "
        "Целевая версия платформы не указана."
    )
    assert without_platform["evidence"] == [{
        "kind": "curated",
        "fact": "introduced",
        "version": "8.3.10",
        "ref": "synthetic",
    }]
    assert before["status"] == "unavailable"
    assert before["introduced"] == "8.3.10"
    assert since["status"] == "available"
    assert since["introduced"] == "8.3.10"


def test_известное_присутствие_не_выдаётся_за_точную_версию_появления(tmp_path):
    database = build_reference_database(
        tmp_path / "source.sqlite3", observed_present=True
    )
    service, _ = _signed_service(tmp_path, database)
    provider = service.provider
    assert provider is not None

    availability = provider.get("dcs/Sum")["availability"]

    assert availability["introduced"] is None
    assert availability["known_present_in"] == "8.3.5"
    assert availability["reason"] == (
        "Элемент присутствует в полном снимке 8.3.5. "
        "Точная версия появления не установлена. "
        "Целевая версия платформы не указана."
    )
    assert availability["evidence"] == [{
        "kind": "observation",
        "presence": "present",
        "version": "8.3.5.1570",
        "ref": "synthetic:8.3.5",
    }]


def test_каталог_объясняет_доступные_фильтры_по_содержимому_базы(tmp_path):
    database = build_reference_database(tmp_path / "source.sqlite3")
    service, _ = _signed_service(tmp_path, database)
    provider = service.provider
    assert provider is not None

    catalog = provider.catalog()

    assert catalog["domains"] == [
        {
            "id": "bsl",
            "title": "Встроенный язык (BSL)",
            "description": "Конструкции языка, правила и практические шаблоны BSL.",
            "access_scope": "default",
            "items": 1,
        },
        {
            "id": "dcs",
            "title": "Выражения СКД",
            "description": "Функции и выражения системы компоновки данных.",
            "access_scope": "explicit",
            "items": 1,
        },
    ]
    assert {row["id"] for row in catalog["kinds"]} == {"article", "function"}
    assert catalog["platform_versions"] == ["8.3.20", "8.3.10"]
    assert service.payload()["catalog"] == catalog


def test_поиск_проверяет_фильтры_и_явный_домен_открывает_свои_карточки(tmp_path):
    database = build_reference_database(tmp_path / "source.sqlite3")
    service, _ = _signed_service(tmp_path, database)
    provider = service.provider
    assert provider is not None

    found = provider.search("сумма", domain="dcs")

    assert found["results"][0]["id"] == "dcs/Sum"
    assert provider.search("сумма")["results"] == []
    with pytest.raises(ReferenceQueryError, match="Неизвестный domain 'unknown'"):
        provider.search("сумма", domain="unknown")
    with pytest.raises(ReferenceQueryError, match="Неизвестный kind 'function' в domain 'bsl'"):
        provider.search("сумма", domain="bsl", kind="function")


def test_повреждённая_и_несовместимая_базы_не_роняют_mcp(tmp_path):
    artifact = tmp_path / "reference" / "reference.mcp1cref"
    artifact.parent.mkdir()
    artifact.write_bytes(b"not signed bundle")
    corrupt = ReferenceService.discover(tmp_path)
    assert corrupt.status.state == "corrupt"
    assert corrupt.provider is None

    database = tmp_path / "incompatible.sqlite3"
    connection = sqlite3.connect(database)
    connection.execute("CREATE TABLE meta(key TEXT PRIMARY KEY, value TEXT NOT NULL)")
    connection.execute("INSERT INTO meta VALUES ('schema_version', '2')")
    connection.commit()
    connection.close()
    signer = SyntheticReferenceSigner.generate()
    signer.build(artifact, database)
    incompatible = ReferenceService.discover(tmp_path, verifier=signer.verifier())
    assert incompatible.status.state == "incompatible"
    assert incompatible.provider is None


def test_индекс_восстанавливается_из_кэша_и_перестраивается_после_замены(tmp_path):
    database = build_reference_database(tmp_path / "source.sqlite3")
    first, signer = _signed_service(tmp_path, database)

    assert first.status.index_cache == "rebuilt"
    assert (tmp_path / "index" / "reference" / "reference.search").is_file()
    first.close()

    second, _ = _signed_service(tmp_path, signer=signer)
    assert second.status.index_cache == "hit"
    second.close()

    database.unlink()
    build_reference_database(database, body="Изменённое синтетическое описание.")
    signer.build(tmp_path / "reference" / "reference.mcp1cref", database)
    replaced, _ = _signed_service(tmp_path, signer=signer)
    assert replaced.status.index_cache == "rebuilt"
    assert "Изменённое" in replaced.provider.get("bsl/Example")["content"]


def test_обычный_startup_registry_не_удаляет_кэш_общей_справки(tmp_path):
    database = build_reference_database(tmp_path / "source.sqlite3")
    first, signer = _signed_service(tmp_path, database)
    cache = tmp_path / "index" / "reference" / "reference.search"
    assert cache.is_file()
    first.close()

    Registry(tmp_path).startup()
    restarted, _ = _signed_service(tmp_path, signer=signer)

    assert cache.is_file()
    assert restarted.status.index_cache == "hit"


def test_удаление_управляемой_базы_оставляет_живой_снимок_до_restart(tmp_path):
    database = build_reference_database(tmp_path / "source.sqlite3")
    service, signer = _signed_service(tmp_path, database)
    cache = tmp_path / "index" / "reference" / "reference.search"
    assert service.provider is not None
    assert cache.is_file()

    pending = service.remove_managed()

    assert not service.managed_path.exists()
    assert not cache.exists()
    assert pending is not None
    assert pending.state == "pending_restart"
    assert pending.action == "remove"
    assert service.provider.search("образец")["results"][0]["id"] == "bsl/Example"

    restarted, _ = _signed_service(tmp_path, signer=signer)
    assert restarted.status.state == "missing"
    assert restarted.provider is None
    names = [
        tool.name
        for tool in asyncio.run(
            build_server(Registry(tmp_path), reference=restarted).list_tools()
        )
    ]
    assert len(names) == 11
    assert "search_reference" not in names
    assert "get_reference" not in names


def test_удаление_неактивированной_загрузки_отменяет_pending(tmp_path):
    source = build_reference_database(tmp_path / "source.sqlite3")
    signer = SyntheticReferenceSigner.generate()
    service, _ = _signed_service(tmp_path, signer=signer)
    candidate = signer.build(tmp_path / "candidate.mcp1cref", source)
    installed = service.install_candidate(candidate)
    assert installed.action == "activate"
    assert service.managed_path.is_file()

    pending = service.remove_managed()

    assert pending is None
    assert service.pending_status is None
    assert not service.managed_path.exists()
    assert service.status.state == "missing"


def test_битое_json_поле_отклоняется_при_старте_до_tools_call(tmp_path):
    from mcp1c.reference_provider import calculate_logical_hash

    database = build_reference_database(tmp_path / "source.sqlite3")
    connection = sqlite3.connect(database)
    connection.row_factory = sqlite3.Row
    connection.execute("UPDATE parameters SET types_json='['")
    digest = calculate_logical_hash(connection)
    connection.execute(
        "UPDATE meta SET value=? WHERE key='content_sha256'", (digest,)
    )
    connection.commit()
    connection.close()
    signer = SyntheticReferenceSigner.generate()
    signer.build(tmp_path / "reference" / "reference.mcp1cref", database)

    service = ReferenceService.discover(tmp_path, verifier=signer.verifier())

    assert service.status.state == "corrupt"
    assert service.status.message == "JSON-поля канонической базы повреждены."
    assert service.provider is None


def test_битый_кэш_расходный_и_не_мешает_старту(tmp_path):
    database = build_reference_database(tmp_path / "source.sqlite3")
    first, signer = _signed_service(tmp_path, database)
    first.close()
    cache = tmp_path / "index" / "reference" / "reference.search"
    cache.write_bytes(b"broken cache")

    recovered, _ = _signed_service(tmp_path, signer=signer)

    assert recovered.status.state == "ready"
    assert recovered.status.index_cache == "rebuilt"
    assert recovered.provider.search("образец")["results"][0]["id"] == "bsl/Example"


@pytest.mark.anyio
async def test_reference_tools_работают_через_полную_mcp_сессию(tmp_path):
    database = build_reference_database(tmp_path / "source.sqlite3")
    service, _ = _signed_service(tmp_path, database)
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
    card_payload = json.loads(card.content[0].text)
    assert card_payload["card"]["id"] == "bsl/Example"
    assert card_payload["availability"]["introduced"] == "8.3.10"
    assert "Подтверждена версия появления 8.3.10" in card_payload["availability"]["reason"]
    assert missing.is_error is True
    assert missing.structured_content == {"result": "Элемент 'missing' не найден."}
