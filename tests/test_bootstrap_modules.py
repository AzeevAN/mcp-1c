"""Выгрузка в файлы, положенная в bootstrap, объясняется, а не падает."""
import zipfile

from conftest import modules_configuration_xml
from mcp1c import intake
from mcp1c.registry import KIND_CONFIGURATION, Registry


def test_выгрузка_в_файлы_в_bootstrap_объясняется(tmp_path):
    registry = Registry(tmp_path / "data")
    registry.bootstrap_dir.mkdir(parents=True)
    архив = registry.bootstrap_dir / "модули.zip"
    with zipfile.ZipFile(архив, "w") as zf:
        zf.writestr("Configuration.xml", modules_configuration_xml())
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
        zf.writestr("Configuration.xml", modules_configuration_xml())
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
        zf.writestr("Configuration.xml", modules_configuration_xml())
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


def test_опасный_манифест_не_переключает_выгрузку_кода_в_schema_v1(tmp_path):
    registry = Registry(tmp_path / "data")
    registry.bootstrap_dir.mkdir(parents=True)
    архив = registry.bootstrap_dir / "модули-с-мусором.zip"
    with zipfile.ZipFile(архив, "w") as zf:
        zf.writestr("Configuration.xml", modules_configuration_xml())
        zf.writestr("Catalogs/Т/Ext/ObjectModule.bsl", "Процедура А()\nКонецПроцедуры")
        zf.writestr("../manifest.xml", "небезопасный мусор")
        zf.writestr("__MACOSX/manifest.json", "мусор Finder")

    сообщения = registry.bootstrap()

    assert any("incoming" in message for message in сообщения)
    assert not registry.sources
    with zipfile.ZipFile(архив) as zf:
        assert set(intake.карта_архива(zf)) == {
            "Configuration.xml",
            "Catalogs/Т/Ext/ObjectModule.bsl",
        }


def test_bootstrap_использует_нормализованную_карту_обёртки(tmp_path):
    registry = Registry(tmp_path / "data")
    registry.bootstrap_dir.mkdir(parents=True)
    архив = registry.bootstrap_dir / "wrapped-normalized.zip"
    with zipfile.ZipFile(архив, "w") as zf:
        zf.writestr("._Wrap", "ресурсная вилка")
        zf.writestr("Wrap//Configuration.xml", modules_configuration_xml())
        zf.writestr(
            "Wrap/./Catalogs/Т/Ext/ObjectModule.bsl",
            "Процедура А()\nКонецПроцедуры",
        )
        zf.writestr("Wrap/../manifest.xml", "небезопасный манифест")

    сообщения = registry.bootstrap()

    assert any("incoming" in message for message in сообщения)
    assert not registry.sources
    with zipfile.ZipFile(архив) as zf:
        assert set(intake.карта_архива(zf)) == {
            "Configuration.xml",
            "Catalogs/Т/Ext/ObjectModule.bsl",
        }
