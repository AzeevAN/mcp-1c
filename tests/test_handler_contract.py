"""Обе исторические формы handler дают одну каноническую привязку."""

from pathlib import Path

import pytest

from mcp1c.graph import Edge, Graph
from mcp1c.loader import load
from mcp1c.model import Configuration, MetadataObject

from conftest import write_export


CANONICAL_HANDLER = "Цель.Обработать"
LEGACY_HANDLER = "ОбщийМодуль.Цель.Обработать"
SUBSCRIPTION = "ПодпискаНаСобытие.ПослеЗаписи"
TARGET = "ОбщийМодуль.Цель"


def _configuration(handler: str) -> Configuration:
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


@pytest.mark.parametrize("handler", [CANONICAL_HANDLER, LEGACY_HANDLER])
def test_graph_принимает_обе_исторические_формы_handler(handler):
    assert Graph(_configuration(handler)).outgoing(SUBSCRIPTION) == [
        Edge(SUBSCRIPTION, TARGET, "handler", "Обработать")
    ]


def test_schema_называет_каноническую_короткую_форму_handler():
    schema = (Path(__file__).parents[1] / "docs/schema-v1.md").read_text(
        encoding="utf-8"
    )

    assert "`handler` (`X.ИмяПроцедуры`;" in schema
    assert "`handler` (`ОбщийМодуль.X.ИмяПроцедуры`)" not in schema
