"""Сравнение явной пары без скрытой третьей конфигурации и потери различий."""

import json
import re
from types import SimpleNamespace

import anyio
import pytest
from mcp import ClientSession
from mcp.shared.memory import create_client_server_memory_streams

from mcp1c.model import Configuration, Field, MetadataObject
from mcp1c.registry import Registry, RegistryError
from mcp1c.server import build_server
from mcp1c.tools import compare_configurations
from conftest import write_export


OBJECT = "Справочник.Пример"


def configuration(name, fields):
    objects = {}
    if fields is not None:
        objects[OBJECT] = MetadataObject(
            OBJECT, "Справочник", "Пример", attributes=[Field(value) for value in fields],
        )
    return Configuration(name=name, objects=objects)


def registry_for(fields):
    configs = {name: SimpleNamespace(config=configuration(name, values))
               for name, values in fields.items()}
    return SimpleNamespace(
        snapshot=lambda: SimpleNamespace(configurations=configs, configuration_names=tuple(configs)),
        resolve=lambda name: SimpleNamespace(configuration=configs[name]),
    )


def continuation(answer):
    match = re.search(r"compare_configurations\((\{[^\n]+\})\)", answer)
    return json.loads(match.group(1)) if match else None


@pytest.mark.parametrize("count", [0, 1, 3, 6])
def test_без_пары_нельзя_молча_выбирать_первые_две(count):
    registry = registry_for({f"Корпус{i}": ["Общее"] for i in range(count)})
    with pytest.raises(RegistryError, match="две"):
        compare_configurations(registry, OBJECT)


@pytest.mark.parametrize("names", [[], ["А"], ["А", "А"], ["А", "Б", "В"]])
def test_явный_список_должен_быть_парой_разных_конфигураций(names):
    with pytest.raises(RegistryError, match="две"):
        compare_configurations(registry_for({"А": [], "Б": [], "В": []}), OBJECT, names)


def test_ровно_две_выбираются_автоматически():
    result = compare_configurations(registry_for({"А": ["Общее"], "Б": ["Общее"]}), OBJECT)
    assert "Имена реквизитов совпадают" in result
    assert "типы" in result.lower()
    assert "А" in result and "Б" in result


def test_пара_первая_и_шестая_сравнивается_без_остальных():
    fields = {f"Корпус{i}": ["Общее"] for i in range(1, 7)}
    fields["Корпус6"].append("ОсобоеПоле")
    result = compare_configurations(registry_for(fields), OBJECT, ["Корпус1", "Корпус6"])
    assert "`ОсобоеПоле`" in result
    assert "Корпус2" not in result


def test_неизвестная_конфигурация_не_подменяется():
    with pytest.raises(RegistryError, match="Неизвестная конфигурация"):
        compare_configurations(registry_for({"А": [], "Б": []}), OBJECT, ["А", "Нет"])


def test_отсутствующий_объект_не_означает_совпадение():
    result = compare_configurations(registry_for({"А": ["Поле"], "Б": None}), OBJECT)
    assert "объекта нет" in result
    assert "совпадают" not in result
    assert continuation(result) is None


def test_пагинация_возвращает_все_различия_обеих_сторон():
    fields = {"А": [f"Левое{i:03}" for i in range(62)],
              "Б": [f"Правое{i:03}" for i in range(43)]}
    registry = registry_for(fields)
    args = {"full_name": OBJECT, "configs": ["А", "Б"], "limit": 17}
    returned = []
    for _ in range(10):
        result = compare_configurations(registry, **args)
        page = re.findall(r"^- `(Левое\d+|Правое\d+)`$", result, re.M)
        assert 0 < len(page) <= 17
        returned.extend(page)
        args = continuation(result)
        if args is None:
            break
    assert args is None
    assert returned == fields["А"] + fields["Б"]


@pytest.mark.parametrize("limit", [0, 101, True, 1.5])
def test_некорректный_размер_страницы_отклоняется(limit):
    with pytest.raises(RegistryError, match="limit"):
        compare_configurations(registry_for({"А": [], "Б": []}), OBJECT, limit=limit)


@pytest.mark.parametrize("cursor", ["!", "e30", "a" * 2049])
def test_битый_курсор_отклоняется(cursor):
    with pytest.raises(RegistryError, match="[Кк]урсор"):
        compare_configurations(registry_for({"А": ["Поле"], "Б": []}), OBJECT, cursor=cursor)


@pytest.mark.parametrize("change", ["order", "object", "fields"])
def test_курсор_не_переносится_между_сравнениями(change):
    registry = registry_for({"А": ["Поле1", "Поле2"], "Б": []})
    args = continuation(compare_configurations(registry, OBJECT, ["А", "Б"], limit=1))
    assert args is not None
    if change == "order":
        args["configs"] = ["Б", "А"]
    elif change == "object":
        args["full_name"] = "Справочник.Другой"
    else:
        registry = registry_for({"А": ["Поле1", "Поле2", "Поле3"], "Б": []})
    with pytest.raises(RegistryError, match="[Кк]урсор"):
        compare_configurations(registry, **args)


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.mark.anyio
async def test_mcp_отклоняет_тройку_и_позволяет_дочитать_пару(tmp_path):
    registry = Registry(tmp_path / "data")
    for name in ["А", "Б", "В"]:
        folder = tmp_path / name
        folder.mkdir()
        registry.add_configuration(write_export(folder, configuration(name, [f"{name}{i:02}" for i in range(42)])))
    server = build_server(registry)
    async with create_client_server_memory_streams() as (client, remote):
        async with anyio.create_task_group() as tasks:
            tasks.start_soon(server._lowlevel_server.run, *remote,
                             server._lowlevel_server.create_initialization_options())
            try:
                async with ClientSession(*client) as session:
                    await session.initialize()
                    rejected = await session.call_tool("compare_configurations", {"full_name": OBJECT})
                    page = await session.call_tool("compare_configurations", {
                        "full_name": OBJECT, "configs": ["А", "Б"], "limit": 40,
                    })
                    args = continuation(page.content[0].text)
                    assert args is not None
                    following = await session.call_tool("compare_configurations", args)
            finally:
                tasks.cancel_scope.cancel()
    assert rejected.is_error is True
    assert page.is_error is False and following.is_error is False
    assert "`А40`" in following.content[0].text
