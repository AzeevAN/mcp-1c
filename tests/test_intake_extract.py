"""Распаковка кладёт только отобранное и не выпускает члены наружу корня."""
import zipfile
from pathlib import Path

from mcp1c.intake import extract


def test_ложится_только_код(tmp_path):
    архив = tmp_path / "в.zip"
    with zipfile.ZipFile(архив, "w") as zf:
        zf.writestr("Catalogs/Т/Ext/ObjectModule.bsl", "A" * 10)
        zf.writestr("Ext/ParentConfigurations/П.cf", "C" * 100)
    корень = tmp_path / "modules"

    файлов, байт = extract(архив, корень)

    assert файлов == 1
    assert байт == 10
    assert (корень / "Catalogs/Т/Ext/ObjectModule.bsl").read_text() == "A" * 10
    assert not (корень / "Ext").exists()


def test_член_наружу_не_записывается(tmp_path):
    архив = tmp_path / "злой.zip"
    with zipfile.ZipFile(архив, "w") as zf:
        zf.writestr("../наружу.bsl", "X")
    корень = tmp_path / "modules"

    файлов, _ = extract(архив, корень)

    assert файлов == 0
    assert not (tmp_path / "наружу.bsl").exists()


def test_дублирующиеся_имена_считаются_по_факту(tmp_path):
    архив = tmp_path / "дубли.zip"
    with zipfile.ZipFile(архив, "w") as zf:
        zf.writestr("Catalogs/Т/Ext/ObjectModule.bsl", "A" * 100)
        zf.writestr("Catalogs/Т/Ext/ObjectModule.bsl", "B" * 999)
    корень = tmp_path / "modules"

    файлов, байт = extract(архив, корень)

    assert файлов == 1
    assert байт == 999
    assert (корень / "Catalogs/Т/Ext/ObjectModule.bsl").read_text() == "B" * 999
