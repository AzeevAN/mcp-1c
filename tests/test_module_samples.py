"""Синтетические раскладки выгрузки модулей для сквозных тестов."""

from __future__ import annotations

import zipfile

from module_samples import ModulesArchiveBuilder
from mcp1c.registry import _сведения_о_выгрузке
from mcp1c.v8container import V8Container


def test_конструктор_создаёт_обе_раскладки_и_боевые_контейнеры(tmp_path):
    tree = ModulesArchiveBuilder.tree()
    tree.form_descriptor("Document", "Документ", "Основная")
    tree.xml_form("Documents", "Документ", "Основная", with_module=True)
    tree.xml_form("Documents", "Документ", "БезМодуля", with_module=False)
    tree.binary_form(
        "Documents",
        "Документ",
        "Контейнерная",
        module="Процедура Открыть()\nКонецПроцедуры",
        form="{25,\"Контейнерная\"}",
    )

    flat = ModulesArchiveBuilder.flat(wrapper="Выгрузка")
    flat.text("Document.Документ.ObjectModule.txt", "Процедура Записать()\nКонецПроцедуры")
    flat.container_form(
        "Document.Документ.Form.Основная.Form",
        module="Процедура Открыть()\nКонецПроцедуры",
        form="{19,\"Основная\"}",
    )
    flat.text("Document.Документ.Form.ТолькоМодуль.Form.Module.txt", "")
    flat.compiled("CommonModule.Закрытый.Module")

    tree_zip = tree.write(tmp_path / "tree.zip")
    flat_zip = flat.write(tmp_path / "flat.zip")

    with zipfile.ZipFile(tree_zip) as archive:
        assert "Documents/Документ/Forms/Контейнерная/Ext/Form.bin" in archive.namelist()
        with V8Container(archive.read("Documents/Документ/Forms/Контейнерная/Ext/Form.bin")) as container:
            assert container.namelist() == ["module", "form"]
            assert container.read("module").decode("utf-8").startswith("Процедура")
        assert archive.read("Documents/Документ/Forms/Основная/Ext/Form.xml").startswith(
            b"<Form>"
        )
        assert "Documents/Документ/Forms/БезМодуля/Ext/Form/Module.bsl" not in archive.namelist()

    with zipfile.ZipFile(flat_zip) as archive:
        assert "Выгрузка/Document.Документ.ObjectModule.txt" in archive.namelist()
        with V8Container(
            archive.read("Выгрузка/Document.Документ.Form.Основная.Form")
        ) as container:
            assert container.read("form").decode("utf-8") == "{19,\"Основная\"}"
        assert archive.read(
            "Выгрузка/Document.Документ.Form.ТолькоМодуль.Form.Module.txt"
        ) == b""
        with V8Container(archive.read("Выгрузка/CommonModule.Закрытый.Module")) as compiled:
            assert compiled.read("image") == b"compiled"


def test_конструктор_умеет_граничные_случаи(tmp_path):
    archive = ModulesArchiveBuilder.tree(extension=True)
    archive.form_descriptor("Catalog", "Объект", "БезТела")
    archive.binary_form("Catalogs", "Объект", "Пустая", module="", form="{999}")
    archive.binary_form(
        "Catalogs", "Объект", "БитыйМодуль", module=b"\xff", form="{25,\"читабельно\"}"
    )
    archive.broken_binary_form("Catalogs", "Объект", "Битая")
    archive.duplicate(
        "Catalogs/Объект/Ext/ObjectModule.bsl",
        "первый".encode(),
        "второй".encode(),
    )
    archive.raw("__MACOSX/._мусор", b"junk")
    archive.raw("../опасный.bsl", b"unsafe")

    path = archive.write(tmp_path / "edge.zip")
    with zipfile.ZipFile(path) as opened:
        assert opened.read("Catalogs/Объект/Forms/Битая/Ext/Form.bin") == b"broken"
        assert opened.read("Catalogs/Объект/Ext/ObjectModule.bsl") == "второй".encode()
        assert opened.namelist().count("Catalogs/Объект/Ext/ObjectModule.bsl") == 2
        assert "Configuration.xml" in opened.namelist()
        with V8Container(opened.read("Catalogs/Объект/Forms/Пустая/Ext/Form.bin")) as empty:
            assert empty.read("module") == b""
            assert empty.read("form") == b"{999}"
        with V8Container(
            opened.read("Catalogs/Объект/Forms/БитыйМодуль/Ext/Form.bin")
        ) as partial:
            assert partial.read("module") == b"\xff"
            assert partial.read("form").decode("utf-8") == "{25,\"читабельно\"}"

    assert _сведения_о_выгрузке(path)[:2] == (True, "СинтетическаяКонфигурация")


def test_одинаковый_дубль_и_расширение_строятся_тем_же_api(tmp_path):
    archive = ModulesArchiveBuilder.flat(extension=True)
    source = "Процедура Одинаковая()\nКонецПроцедуры\n".encode()
    archive.duplicate("CommonModule.Одинаковый.Module.txt", source, source)
    path = archive.write(tmp_path / "extension.zip")

    with zipfile.ZipFile(path) as opened:
        assert opened.namelist().count("CommonModule.Одинаковый.Module.txt") == 2
        assert opened.read("CommonModule.Одинаковый.Module.txt") == source

    assert _сведения_о_выгрузке(path)[:2] == (True, "СинтетическаяКонфигурация")
