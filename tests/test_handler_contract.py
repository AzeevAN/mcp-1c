"""Все формы handler/method дают одну каноническую привязку."""

from pathlib import Path

import pytest

from mcp1c.graph import Edge, Graph
from mcp1c.loader import load
from mcp1c.model import Configuration, MetadataObject

from conftest import write_export


CANONICAL_HANDLER = "Цель.Обработать"
LEGACY_HANDLER = "ОбщийМодуль.Цель.Обработать"
# Так обработчик пишет native-выгрузка конфигуратора (`<Handler>`).
NATIVE_HANDLER = "CommonModule.Цель.Обработать"
JOB = "РегламентноеЗадание.НочнойОбмен"
SUBSCRIPTION = "ПодпискаНаСобытие.ПослеЗаписи"
TARGET = "ОбщийМодуль.Цель"


def _configuration(handler: str, method: str | None = None) -> Configuration:
    config = Configuration(
        name="ТестоваяКонфигурация",
        version="1.0",
        platform="8.3.23.1997",
    )
    objects = (
        MetadataObject(
            full_name=TARGET,
            kind="ОбщийМодуль",
            name="Цель",
        ),
        MetadataObject(
            full_name=SUBSCRIPTION,
            kind="ПодпискаНаСобытие",
            name="ПослеЗаписи",
            props={"handler": handler},
        ),
    )
    if method is not None:
        objects += (
            MetadataObject(
                full_name=JOB,
                kind="РегламентноеЗадание",
                name="НочнойОбмен",
                props={"method": method},
            ),
        )
    config.objects = {obj.full_name: obj for obj in objects}
    return config


@pytest.mark.parametrize("handler", [CANONICAL_HANDLER, LEGACY_HANDLER])
def test_loader_нормализует_обе_формы_handler_и_строит_одно_ребро(
    tmp_path, handler
):
    loaded = load(write_export(tmp_path, _configuration(handler)))

    assert loaded.objects[SUBSCRIPTION].props["handler"] == CANONICAL_HANDLER
    assert Graph(loaded).outgoing(SUBSCRIPTION) == [
        Edge(SUBSCRIPTION, TARGET, "handler", "Обработать")
    ]


@pytest.mark.parametrize(
    "handler", [CANONICAL_HANDLER, LEGACY_HANDLER, NATIVE_HANDLER]
)
def test_graph_принимает_обе_исторические_формы_handler(handler):
    assert Graph(_configuration(handler)).outgoing(SUBSCRIPTION) == [
        Edge(SUBSCRIPTION, TARGET, "handler", "Обработать")
    ]


@pytest.mark.parametrize(
    "method", [CANONICAL_HANDLER, LEGACY_HANDLER, NATIVE_HANDLER]
)
def test_loader_и_graph_нормализуют_method_регламентного_задания(tmp_path, method):
    loaded = load(write_export(tmp_path, _configuration(CANONICAL_HANDLER, method)))

    assert loaded.objects[JOB].props["method"] == CANONICAL_HANDLER
    assert Graph(loaded).outgoing(JOB) == [
        Edge(JOB, TARGET, "method", "Обработать")
    ]


@pytest.mark.parametrize("prefix", ["ОбщийМодуль", "CommonModule"])
def test_модуль_с_именем_префикса_не_теряет_имя(prefix):
    # `CommonModule.Обработать` — короткая форма для модуля с таким именем.
    config = _configuration(f"{prefix}.Обработать")
    модуль = MetadataObject(
        full_name=f"ОбщийМодуль.{prefix}", kind="ОбщийМодуль", name=prefix
    )
    config.objects[модуль.full_name] = модуль

    assert Graph(config).outgoing(SUBSCRIPTION) == [
        Edge(SUBSCRIPTION, f"ОбщийМодуль.{prefix}", "handler", "Обработать")
    ]


def test_schema_называет_каноническую_короткую_форму_handler():
    schema = (Path(__file__).parents[1] / "docs/schema-v1.md").read_text(
        encoding="utf-8"
    )

    assert "`handler` (`X.ИмяПроцедуры`;" in schema
    assert "`handler` (`ОбщийМодуль.X.ИмяПроцедуры`)" not in schema
