"""Источник модулей не должен вытеснять метаданные той же конфигурации."""
import zipfile
from pathlib import Path

from conftest import build_configuration, write_export
from mcp1c.registry import KIND_CONFIGURATION, KIND_MODULES, Registry


def _выгрузка_в_файлы(tmp_path: Path) -> Path:
    путь = tmp_path / "модули.zip"
    with zipfile.ZipFile(путь, "w") as zf:
        zf.writestr("Configuration.xml", "<x/>")
        zf.writestr("Catalogs/Т/Ext/ObjectModule.bsl", "Процедура А() КонецПроцедуры")
    return путь


def test_ключ_источника_модулей_отдельный(tmp_path):
    входящее = tmp_path / "in"
    входящее.mkdir()
    registry = Registry(tmp_path / "data")
    метаданные = registry.add_configuration(
        write_export(входящее, build_configuration(name="Розница"))
    )

    модули = registry.add_modules(_выгрузка_в_файлы(tmp_path), configuration="Розница")

    assert метаданные.id == "Розница"
    assert модули.id == "Розница:modules"
    assert модули.kind == KIND_MODULES
    # Метаданные на месте: ключи разные, вытеснения не произошло.
    assert registry.sources["Розница"].kind == KIND_CONFIGURATION
    assert "Розница" in registry.configurations


def test_код_лёг_в_свой_каталог(tmp_path):
    входящее = tmp_path / "in"
    входящее.mkdir()
    registry = Registry(tmp_path / "data")
    registry.add_configuration(write_export(входящее, build_configuration(name="Розница")))

    registry.add_modules(_выгрузка_в_файлы(tmp_path), configuration="Розница")

    лежит = registry.modules_dir / "Розница" / "Catalogs/Т/Ext/ObjectModule.bsl"
    assert лежит.is_file()


def test_исходник_не_копируется(tmp_path):
    входящее = tmp_path / "in"
    входящее.mkdir()
    registry = Registry(tmp_path / "data")
    registry.add_configuration(write_export(входящее, build_configuration(name="Розница")))

    registry.add_modules(_выгрузка_в_файлы(tmp_path), configuration="Розница")

    # `keep_source=True` положил бы второй экземпляр архива на том.
    скопировано = list((registry.sources_dir).rglob("модули.zip"))
    assert скопировано == []
