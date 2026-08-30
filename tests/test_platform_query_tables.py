"""Таблицы запросов из версионной справки платформы."""

from __future__ import annotations

from mcp1c.registry import Registry
from mcp1c.store import save_syntax
from mcp1c.syntax_model import (
    KIND_QUERY_TABLE,
    SyntaxIndex,
    SyntaxItem,
    SyntaxVariant,
)
from mcp1c.tools import get_syntax

from conftest import build_configuration, write_export


def test_query_table_версионируется_как_платформа(tmp_path):
    incoming = tmp_path / "incoming"
    incoming.mkdir()

    старая = SyntaxIndex(platforms=["8.3.5.1570"], source="test-8.3.5.1570")
    старая.add(
        SyntaxItem(
            id="tables/КритерийОтбора",
            kind=KIND_QUERY_TABLE,
            name_ru="КритерийОтбора",
            name_en="SelectionCriterion",
            description="Таблица критерия отбора",
            variants=[
                SyntaxVariant(signature="КритерийОтбора.<Имя критерия отбора>")
            ],
        )
    )
    новая = SyntaxIndex(platforms=["8.3.27.2130"], source="test-8.3.27.2130")
    новая.add(
        SyntaxItem(
            id="tables/КритерийОтбора",
            kind=KIND_QUERY_TABLE,
            name_ru="КритерийОтбора",
            name_en="SelectionCriterion",
            description="Таблица критерия отбора",
            variants=[
                SyntaxVariant(
                    signature="КритерийОтбора.<Имя критерия отбора>.<Доп>"
                )
            ],
        )
    )

    registry = Registry(tmp_path / "data")
    registry.add_syntax(
        save_syntax(старая, incoming / "8.3.5.1570.json.gz")
    )
    registry.add_syntax(
        save_syntax(новая, incoming / "8.3.27.2130.json.gz")
    )

    config = build_configuration()
    config.platform = "8.3.5.1570"
    registry.add_configuration(write_export(incoming, config))

    ответ = get_syntax(registry, "КритерийОтбора")

    assert "КритерийОтбора.<Имя критерия отбора>" in ответ
    assert "<Доп>" not in ответ
