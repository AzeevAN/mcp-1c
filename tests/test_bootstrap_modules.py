"""Выгрузка в файлы, положенная в bootstrap, объясняется, а не падает."""
import zipfile

from mcp1c.registry import KIND_CONFIGURATION, Registry


def test_выгрузка_в_файлы_в_bootstrap_объясняется(tmp_path):
    registry = Registry(tmp_path / "data")
    registry.bootstrap_dir.mkdir(parents=True)
    архив = registry.bootstrap_dir / "модули.zip"
    with zipfile.ZipFile(архив, "w") as zf:
        zf.writestr("Configuration.xml", "<x/>")
        zf.writestr("Catalogs/Т/Ext/ObjectModule.bsl", "Процедура А() КонецПроцедуры")

    сообщения = registry.bootstrap()

    assert any("incoming" in m for m in сообщения)
    # Источник не заведён: разбирать здесь нечего.
    assert not registry.sources


def test_плоская_выгрузка_в_файлы_узнаётся(tmp_path):
    """Плоская выгрузка (8.3.5) с модулями в .txt без форм узнаётся."""
    registry = Registry(tmp_path / "data")
    registry.bootstrap_dir.mkdir(parents=True)
    архив = registry.bootstrap_dir / "плоская.zip"
    with zipfile.ZipFile(архив, "w") as zf:
        zf.writestr("Configuration.xml", "<x/>")
        zf.writestr("Constants/М/Ext.txt", "Процедура А() КонецПроцедуры")
        zf.writestr("Documents/Д/Ext.txt", "Процедура Б() КонецПроцедуры")

    сообщения = registry.bootstrap()

    assert any("incoming" in m for m in сообщения)
    assert not registry.sources


def test_выгрузка_schema_v1_с_xml_манифестом_не_перехватывается(tmp_path):
    """Schema v1 с manifest.xml не должна быть распознана как выгрузка в файлы."""
    registry = Registry(tmp_path / "data")
    registry.bootstrap_dir.mkdir(parents=True)
    архив = registry.bootstrap_dir / "metadata.zip"
    with zipfile.ZipFile(архив, "w") as zf:
        zf.writestr("Configuration.xml", "<x/>")
        zf.writestr("manifest.xml", "<?xml version='1.0'?>")
        # .txt может встречаться рядом с XML-манифестом
        zf.writestr("SomeModule.txt", "код")

    сообщения = registry.bootstrap()

    # Не должно быть сообщения про incoming — файл ушёл в обычный путь разбора
    assert not any("incoming" in m for m in сообщения)


def test_обычная_выгрузка_из_bootstrap_заводит_источник(tmp_path):
    """Положительное покрытие нормального пути `bootstrap()`.

    Ранний `continue` для выгрузки в файлы стоит до разбора и способен
    проглотить любой `.zip`. Проверка «в сообщениях нет слова incoming» этого
    не поймала бы: она зелена и когда источник не завёлся вовсе.
    """
    from conftest import build_configuration, write_export

    registry = Registry(tmp_path / "data")
    registry.bootstrap_dir.mkdir(parents=True)
    write_export(registry.bootstrap_dir, build_configuration(name="Розница"))

    сообщения = registry.bootstrap()

    assert сообщения == ["Розница"]
    assert registry.sources["Розница"].kind == KIND_CONFIGURATION
    assert "Розница" in registry.configurations
