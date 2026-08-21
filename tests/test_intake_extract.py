"""Распаковка кладёт только отобранное и не выпускает члены наружу корня."""
import warnings
import zipfile
from pathlib import Path

from mcp1c.intake import INDEX_RESERVE, extract, planned_size


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
    # Дублирующееся имя при сборке архива генерирует UserWarning от zipfile.
    # Это ожидаемо: тест проверяет именно обработку дублирующихся имён.
    # Второй, ненужный член — на другом каталоге верхнего уровня: без него
    # весь архив лежал бы в одном "Catalogs" и попал бы под правило обёртки,
    # а тест — не про неё.
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", UserWarning)
        with zipfile.ZipFile(архив, "w") as zf:
            zf.writestr("Catalogs/Т/Ext/ObjectModule.bsl", "A" * 100)
            zf.writestr("Catalogs/Т/Ext/ObjectModule.bsl", "B" * 999)
            zf.writestr("Ext/ParentConfigurations/П.cf", "C" * 100)
    корень = tmp_path / "modules"

    файлов, байт = extract(архив, корень)

    assert файлов == 1
    assert байт == 999
    assert (корень / "Catalogs/Т/Ext/ObjectModule.bsl").read_text() == "B" * 999


# ------------------------------------------------------- обёртка архива


def test_обёртка_архива_не_воспроизводится_в_пути(tmp_path):
    """Архив, упакованный `zip -r архив.zip папка` (или Finder): всё лежит
    внутри одного каталога верхнего уровня — этот уровень при раскладке на
    диск не повторяется."""
    архив = tmp_path / "обёрнутый.zip"
    with zipfile.ZipFile(архив, "w") as zf:
        zf.writestr("Расширение/Catalogs/Т/Ext/ObjectModule.bsl", "A" * 10)
        zf.writestr("Расширение/Catalogs/Т/Forms/Ф/Ext/Form.xml", "B" * 20)
    корень = tmp_path / "modules"

    файлов, байт = extract(архив, корень)

    assert файлов == 2
    assert байт == 30
    assert (корень / "Catalogs/Т/Ext/ObjectModule.bsl").read_text() == "A" * 10
    assert (корень / "Catalogs/Т/Forms/Ф/Ext/Form.xml").read_text() == "B" * 20
    # Лишнего уровня «Расширение/» в путях на диске нет.
    assert not (корень / "Расширение").exists()


def test_несколько_каталогов_верхнего_уровня_не_обёртка(tmp_path):
    """Два разных каталога верхнего уровня — не обёртка: угадывать, какой
    из них «тот самый», рискованнее, чем не снимать уровень вовсе."""
    архив = tmp_path / "два.zip"
    with zipfile.ZipFile(архив, "w") as zf:
        zf.writestr("А/Catalogs/Т/Ext/ObjectModule.bsl", "A" * 10)
        zf.writestr("Б/Catalogs/Д/Ext/ObjectModule.bsl", "B" * 20)
    корень = tmp_path / "modules"

    файлов, байт = extract(архив, корень)

    assert файлов == 2
    assert байт == 30
    assert (корень / "А/Catalogs/Т/Ext/ObjectModule.bsl").read_text() == "A" * 10
    assert (корень / "Б/Catalogs/Д/Ext/ObjectModule.bsl").read_text() == "B" * 20


def test_мусор_finder_не_ломает_обёртку_и_не_попадает_в_отбор(tmp_path):
    """`__MACOSX/` и корневой `.DS_Store` — мусор конкретного архиватора, а
    не второй каталог верхнего уровня: обёртка распознаётся, как если бы их
    не было, а сами они на диск не попадают."""
    архив = tmp_path / "finder.zip"
    with zipfile.ZipFile(архив, "w") as zf:
        zf.writestr(".DS_Store", b"\x00\x00\x00\x00")
        zf.writestr("__MACOSX/", "")
        zf.writestr("__MACOSX/Расширение/", "")
        zf.writestr(
            "__MACOSX/Расширение/._ObjectModule.bsl", b"\x00\x05\x16\x07\x00\x02"
        )
        zf.writestr("Расширение/Catalogs/Т/Ext/ObjectModule.bsl", "A" * 10)
    корень = tmp_path / "modules"

    файлов, байт = extract(архив, корень)

    assert файлов == 1
    assert байт == 10
    assert (корень / "Catalogs/Т/Ext/ObjectModule.bsl").read_text() == "A" * 10
    assert not (корень / "Расширение").exists()
    assert not any(p.name.startswith("._") for p in корень.rglob("*"))


def test_planned_size_и_extract_совпадают_на_обёрнутом_архиве(tmp_path):
    """`planned_size` (по центральному каталогу) и `extract` (по факту на
    диске) обязаны считать одно и то же — иначе проверка места на входе
    разойдётся с тем, что реально ляжет."""
    архив = tmp_path / "обёрнутый.zip"
    with zipfile.ZipFile(архив, "w") as zf:
        zf.writestr("Расширение/Catalogs/Т/Ext/ObjectModule.bsl", "A" * 10)
        zf.writestr("Расширение/Catalogs/Т/Forms/Ф/Ext/Form.xml", "B" * 20)
        zf.writestr("Расширение/Ext/ParentConfigurations/П.cf", "C" * 100_000)
    корень = tmp_path / "modules"

    нужно, _формат = planned_size(архив)
    файлов, байт = extract(архив, корень)

    assert файлов == 2
    assert нужно - INDEX_RESERVE == байт


def test_planned_size_совпадает_с_extract_для_дублей_и_опасных_путей(
    tmp_path, monkeypatch
):
    from mcp1c import intake

    monkeypatch.setattr(intake, "INDEX_RESERVE_MIN", 0)
    архив = tmp_path / "точный.zip"
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", UserWarning)
        with zipfile.ZipFile(архив, "w") as zf:
            zf.writestr("Catalogs/Т/Ext/ObjectModule.bsl", "A" * 100)
            zf.writestr("Catalogs/Т/Ext/ObjectModule.bsl", "B" * 999)
            zf.writestr("../наружу.bsl", "X" * 500)
            zf.writestr("Ext/ParentConfigurations/П.cf", "C" * 1000)
    корень = tmp_path / "modules"

    нужно, _ = planned_size(архив)
    файлов, байт = extract(архив, корень)

    assert файлов == 1 and байт == 999
    assert нужно == байт + 150


def test_опасный_и_finder_мусор_не_переключают_формат_и_не_ломают_обёртку(
    tmp_path, monkeypatch
):
    from mcp1c import intake

    monkeypatch.setattr(intake, "INDEX_RESERVE_MIN", 0)
    архив = tmp_path / "безопасный-формат.zip"
    with zipfile.ZipFile(архив, "w") as zf:
        zf.writestr("Обёртка/Catalogs/Т/Ext/ObjectModule.bsl", "A" * 10)
        zf.writestr("../escape.Form", "B" * 100)
        zf.writestr("__MACOSX/junk.Form", "C" * 100)
        zf.writestr("../CommonModule.Ложный.Module", "D" * 100)
    корень = tmp_path / "modules-safe"

    нужно, формат = planned_size(архив)
    файлов, байт = extract(архив, корень)

    assert формат == "tree"
    assert файлов == 1 and байт == 10
    assert нужно == 12
    assert (корень / "Catalogs/Т/Ext/ObjectModule.bsl").read_text() == "A" * 10
    assert not (корень / "Обёртка").exists()
