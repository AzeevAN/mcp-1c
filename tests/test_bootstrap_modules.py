"""Выгрузка в файлы, положенная в bootstrap, объясняется, а не падает."""
import zipfile

from mcp1c.registry import Registry


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
