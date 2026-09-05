"""Внутренние метки таблиц не становятся пользовательским Markdown."""

import hashlib
import json
import sqlite3

import anyio
import pytest
from mcp import ClientSession
from mcp.shared.memory import create_client_server_memory_streams

from mcp1c.reference_provider import ReferenceService, calculate_logical_hash
from mcp1c.registry import Registry
from mcp1c.server import build_server
from reference_fixture import SyntheticReferenceSigner, build_reference_database


@pytest.fixture
def reference_with_tables(tmp_path):
    body = (
        "Вступление.\n" + "Синтетическое описание. " * 40
        + "\n\x00таблица-0\x00\nМежду таблицами.\n\x00таблица-1\x00\n"
        + "Завершение; буквальный текст таблица-0 сохранён."
    )
    database = build_reference_database(tmp_path / "synthetic.sqlite3", body=body)
    with sqlite3.connect(database) as connection:
        connection.row_factory = sqlite3.Row
        connection.execute("UPDATE sections SET body=?", (body,))
        for ordinal in range(2):
            connection.execute(
                "INSERT INTO item_tables VALUES (?, ?, ?, ?, ?)",
                ("bsl/Example", ordinal, json.dumps([f"Колонка {ordinal}"]),
                 json.dumps([[f"Ячейка {ordinal}"]]), "a" * 64),
            )
        connection.execute(
            "UPDATE meta SET value=? WHERE key='content_sha256'",
            (calculate_logical_hash(connection),),
        )
    connection.close()
    signer = SyntheticReferenceSigner.generate()
    artifact = signer.build(tmp_path / "reference" / "reference.mcp1cref", database)
    original_digest = hashlib.sha256(artifact.read_bytes()).hexdigest()
    service = ReferenceService.discover(tmp_path, verifier=signer.verifier())
    assert service.status.state == "ready"
    yield service, body
    assert hashlib.sha256(artifact.read_bytes()).hexdigest() == original_digest


@pytest.mark.parametrize("section_id", [None, "bsl/Example#usage"])
def test_маркеры_не_попадают_в_карточку_и_раздел(reference_with_tables, section_id):
    service, body = reference_with_tables
    content = service.provider.get(
        "bsl/Example", section_id=section_id, max_chars=10000,
    )["content"]
    assert "\x00" not in content
    assert body.replace("\x00таблица-0\x00", "").replace("\x00таблица-1\x00", "") in content
    if section_id is None:
        for ordinal in range(2):
            assert content.count(f"## Таблица {ordinal + 1}") == 1
            assert content.count(f"Колонка {ordinal}") == 1
            assert content.count(f"Ячейка {ordinal}") == 1


@pytest.mark.parametrize("section_id", [None, "bsl/Example#usage"])
def test_пагинация_чистого_текста_не_теряет_содержимое(reference_with_tables, section_id):
    service, _ = reference_with_tables
    provider = service.provider
    whole = provider.get("bsl/Example", section_id=section_id, max_chars=10000)["content"]
    pages = []
    cursor = None
    offset = 0
    for _ in range(20):
        page = provider.get("bsl/Example", section_id=section_id, max_chars=256, cursor=cursor)
        assert "\x00" not in page["content"]
        assert page["continuation"]["offset"] == offset
        pages.append(page["content"])
        offset += len(page["content"])
        assert page["continuation"]["next_offset"] == offset
        assert page["continuation"]["total_chars"] == len(whole)
        cursor = page["continuation"]["next_cursor"]
        if cursor is None:
            break
    assert cursor is None
    assert len(pages) > 1
    assert "".join(pages) == whole


def test_карточка_без_маркеров_не_меняется(tmp_path):
    database = build_reference_database(tmp_path / "synthetic.sqlite3", body="Текст таблица-0.")
    signer = SyntheticReferenceSigner.generate()
    signer.build(tmp_path / "reference" / "reference.mcp1cref", database)
    service = ReferenceService.discover(tmp_path, verifier=signer.verifier())
    content = service.provider.get("bsl/Example")["content"]
    assert "## Описание\n\nТекст таблица-0." in content


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.mark.anyio
@pytest.mark.parametrize("section_id", [None, "bsl/Example#usage"])
async def test_mcp_выдаёт_чистый_текст(reference_with_tables, tmp_path, section_id):
    service, _ = reference_with_tables
    server = build_server(Registry(tmp_path / "registry"), reference=service)
    async with create_client_server_memory_streams() as (client, remote):
        async with anyio.create_task_group() as tasks:
            tasks.start_soon(
                server._lowlevel_server.run, *remote,
                server._lowlevel_server.create_initialization_options(),
            )
            try:
                async with ClientSession(*client) as session:
                    await session.initialize()
                    arguments = {"item_id": "bsl/Example", "max_chars": 10000}
                    if section_id is not None:
                        arguments["section_id"] = section_id
                    result = await session.call_tool("get_reference", arguments)
            finally:
                tasks.cancel_scope.cancel()
    assert result.is_error is False
    payload = json.loads(result.content[0].text)
    assert "\x00" not in payload["content"]
    if section_id is None:
        assert "Ячейка 0" in payload["content"]
        assert "Ячейка 1" in payload["content"]
