"""Отбор членов архива: что берём и что отвергаем.

Формат выгрузки определяется раскладкой (разведка, раздел 6): иерархическая
даёт `.bsl` и `Form.xml`, плоская — `.txt` и контейнеры `.Form`.
"""
from pathlib import Path

import pytest

from mcp1c.intake import (
    FORMAT_FLAT,
    FORMAT_TREE,
    detect_format,
    is_wanted,
    safe_target,
)


def test_иерархическая_выгрузка_узнаётся_по_bsl():
    имена = ["Configuration.xml", "Catalogs/Товары/Ext/ObjectModule.bsl"]
    assert detect_format(имена) == FORMAT_TREE


def test_плоская_выгрузка_узнаётся_по_контейнеру_формы():
    имена = ["Configuration.xml", "Catalog.Товары.Форма.Form"]
    assert detect_format(имена) == FORMAT_FLAT


def test_плоская_выгрузка_узнаётся_по_скомпилированному_общему_модулю():
    имена = ["Configuration.xml", "CommonModules/Пример.Module"]

    assert detect_format(имена) == FORMAT_FLAT


def test_плоское_имя_скомпилированного_модуля_узнаётся_после_снятия_обёртки():
    # `detect_format` получает ключи единой карты: обёртка уже снята.
    имена = ["Configuration.xml", "CommonModule.Пример.Module"]

    assert detect_format(имена) == FORMAT_FLAT


def test_в_иерархической_берём_модули_и_формы():
    assert is_wanted("Catalogs/Товары/Ext/ObjectModule.bsl", FORMAT_TREE)
    assert is_wanted("Catalogs/Товары/Forms/Форма/Ext/Form.xml", FORMAT_TREE)


def test_балласт_не_берём():
    for имя in (
        "Ext/ParentConfigurations/Поставка.cf",
        "Catalogs/Товары/Ext/Макет.bin",
        "Catalogs/Товары.xml",
    ):
        assert not is_wanted(имя, FORMAT_TREE), имя


def test_скомпилированный_общий_модуль_берётся_только_в_плоском_формате():
    assert is_wanted("CommonModules/Пример.Module", FORMAT_FLAT)
    assert not is_wanted("CommonModules/Пример.Module", FORMAT_TREE)
    assert not is_wanted("Catalogs/Пример.Module", FORMAT_FLAT)
    assert is_wanted("CommonModule.Пример.Module", FORMAT_FLAT)


def test_канонический_регистр_скомпилированного_модуля_строгий():
    assert not is_wanted("CommonModules/Пример.module", FORMAT_FLAT)
    assert not is_wanted("CommonModules/Пример.MODULE", FORMAT_FLAT)
    assert not is_wanted("commonmodules/Пример.Module", FORMAT_FLAT)
    assert not is_wanted("commonModule.Пример.Module", FORMAT_FLAT)
    assert not is_wanted("CommonModule.Пример.module", FORMAT_FLAT)
    assert not is_wanted("CommonModules/Лишний/Пример.Module", FORMAT_FLAT)
    assert not is_wanted("CommonModules/Пример.Лишний.Module", FORMAT_FLAT)


@pytest.mark.parametrize(
    "junk",
    ["Junk/CommonModule.False.Module", "Junk/CommonModules/False.Module"],
)
def test_вложенный_неканонический_module_не_переключает_tree_в_flat(junk):
    имена = ["Catalogs/Т/Ext/ObjectModule.bsl", junk]

    assert detect_format(имена) == FORMAT_TREE


def test_канонический_макет_исключён_но_настоящие_txt_остаются():
    assert not is_wanted("Catalogs/Пример.Template.txt", FORMAT_FLAT)
    assert is_wanted("Catalogs/Пример.template.txt", FORMAT_FLAT)
    assert is_wanted("Catalogs/Пример.Module.txt", FORMAT_FLAT)


def test_член_с_выходом_наружу_отвергается(tmp_path):
    for имя in ("../наружу.bsl", "/абсолютный.bsl", "a/../../наружу.bsl"):
        assert safe_target(имя, tmp_path) is None, имя


def test_обычный_член_ложится_внутрь_корня(tmp_path):
    цель = safe_target("Catalogs/Товары/Ext/ObjectModule.bsl", tmp_path)
    assert цель is not None
    assert tmp_path in цель.parents


def test_ресурсная_вилка_finder_отвергается():
    """Мелочь 3, ре-ревью: правило про `__MACOSX/` и правило про `._` —
    два разных условия, и тест на архив «как Finder» бьёт сразу по обоим
    сразу (`__MACOSX/.../._ObjectModule.bsl`). Здесь — по одному отдельно:
    имя, начинающееся на `._`, отвергается вне `__MACOSX/` тоже."""
    assert not is_wanted("Catalogs/Товары/Ext/._ObjectModule.bsl", FORMAT_TREE)
    assert not is_wanted("Constants/М/._Ext.txt", FORMAT_FLAT)


def test_каталог_метаданных_named___macosx__не_путается_со_служебным():
    """Обратная сторона: `__MACOSX` — законное имя каталога метаданных,
    если оно не на первом (верхнем) уровне пути. Правило исключает только
    `__MACOSX/` как каталог ВЕРХНЕГО уровня архива (мусор Finder), а не
    любое вхождение этой строки где-то в пути."""
    assert is_wanted("Catalogs/__MACOSX/Ext/ObjectModule.bsl", FORMAT_TREE)
