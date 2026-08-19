"""Приём справки платформы: что реестр обязан отвергнуть.

Из каталога установленной 1С подходит ровно один файл — `shcntx_ru.hbk`.
Рядом с ним лежат ещё 37 файлов того же расширения, и человек берёт наугад.
Часть из них — вообще не контейнеры 1С, и парсер отбивает их сам. Но
`config_ru.hbk` (справка конфигуратора) — настоящий контейнер с элементом
`FileStorage`, разбор проходит и даёт ноль элементов. Такая «справка»
раньше вставала в реестр как успешная и вытесняла рабочую.
"""

from __future__ import annotations

import pytest

from mcp1c.registry import Registry, RegistryError
from mcp1c.store import save_syntax
from mcp1c.syntax_model import SyntaxIndex, SyntaxItem

from conftest import write_syntax


def _empty_syntax(directory) -> "object":
    """Разбор, не давший ни одного элемента, — как у `config_ru.hbk`."""
    directory.mkdir(parents=True, exist_ok=True)
    return save_syntax(SyntaxIndex(platforms=["8.3.99.1"], source="пусто"),
                       directory / "пустая.json.gz")


def _syntax_without_descriptions(directory):
    """Скелет справки — то, что даёт `shcntx_root.hbk`.

    Элементы есть, текстов нет: языконезависимая часть несёт дерево страниц и
    английские идентификаторы, а описания, русские имена и версии появления
    лежат в локализованных `_ru`/`_en`.
    """
    directory.mkdir(parents=True, exist_ok=True)
    # Версия указана намеренно: без неё загрузка отвергается раньше — по
    # отсутствию версии, — и тест перестал бы проверять то, ради чего написан.
    index = SyntaxIndex(platforms=["8.3.99.1"], source="root")
    for name in ("StrFind", "JSONWriter"):
        index.add(SyntaxItem(id=f"objects/{name}", kind="object", name_ru=name))
    return save_syntax(index, directory / "скелет.json.gz")


def test_справка_без_описаний_не_принимается(tmp_path):
    registry = Registry(tmp_path / "data")

    with pytest.raises(RegistryError, match="описани"):
        registry.add_syntax(_syntax_without_descriptions(tmp_path / "incoming"))

    assert registry.syntax is None
    assert registry.sources == {}


def test_справка_без_описаний_не_вытесняет_загруженную(tmp_path):
    registry = Registry(tmp_path / "data")
    registry.add_syntax(write_syntax(tmp_path / "incoming"))
    было = registry.syntax

    with pytest.raises(RegistryError, match="описани"):
        registry.add_syntax(_syntax_without_descriptions(tmp_path / "incoming"))

    assert registry.syntax is было
    assert sorted(registry.sources) == ["syntax-8.3.99.1"]


def test_пустая_справка_не_принимается(tmp_path):
    registry = Registry(tmp_path / "data")

    with pytest.raises(RegistryError, match="ни одного элемента"):
        registry.add_syntax(_empty_syntax(tmp_path / "incoming"))

    assert registry.syntax is None
    assert registry.sources == {}


def test_пустая_справка_не_вытесняет_загруженную(tmp_path):
    registry = Registry(tmp_path / "data")
    registry.add_syntax(write_syntax(tmp_path / "incoming"))
    было = registry.syntax

    with pytest.raises(RegistryError, match="ни одного элемента"):
        registry.add_syntax(_empty_syntax(tmp_path / "incoming"))

    assert registry.syntax is было
    assert sorted(registry.sources) == ["syntax-8.3.99.1"]


def test_справка_другой_версии_встаёт_рядом_и_обе_видны(tmp_path):
    """Прежде справка была одна на процесс и новая вытесняла прежнюю.

    Замер на пяти настоящих справках это отменил: одна свежая справка на
    конфигурации 8.3.5 ошибается в 730 местах, а слияние версий стоит +0,9%
    объёма. Теперь справки разных версий стоят рядом, и обе видны в списке
    источников — иначе непонятно, чем сервер отвечает. Отвечает слитый вид,
    его версии перечислены в `registry.syntax.syntax.platforms`.
    """
    registry = Registry(tmp_path / "data")
    incoming = tmp_path / "incoming"
    registry.add_syntax(write_syntax(incoming, platform="8.3.99.1"))

    registry.add_syntax(write_syntax(incoming, platform="8.3.100.1"))

    assert sorted(registry.sources) == ["syntax-8.3.100.1", "syntax-8.3.99.1"]
    assert registry.syntax is not None
    assert registry.syntax.source.id == "syntax-8.3.100.1"
    assert registry.syntax.syntax.platforms == ["8.3.99.1", "8.3.100.1"]
