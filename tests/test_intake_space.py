"""Требуемый объём считается по центральному каталогу, без распаковки."""
import zipfile
from pathlib import Path

from conftest import modules_configuration_xml
from mcp1c import intake
from mcp1c.intake import FORMAT_TREE, enough_space, planned_size


def _архив(tmp_path: Path) -> Path:
    путь = tmp_path / "выгрузка.zip"
    with zipfile.ZipFile(путь, "w") as zf:
        zf.writestr("Configuration.xml", modules_configuration_xml())
        zf.writestr("Catalogs/Товары/Ext/ObjectModule.bsl", "A" * 1000)
        zf.writestr("Catalogs/Товары/Forms/Ф/Ext/Form.xml", "B" * 500)
        zf.writestr("Ext/ParentConfigurations/Поставка.cf", "C" * 100_000)
    return путь


def test_считается_только_отобранное(tmp_path):
    нужно, формат = planned_size(_архив(tmp_path))

    assert формат == FORMAT_TREE
    # 1000 + 500 плюс запас под индекс; балласт в 100 КБ не учитывается.
    assert 1500 < нужно < 1500 + 26 * 1024 * 1024


def test_места_не_хватает_называется_свободное(tmp_path):
    хватает, свободно = enough_space(10 ** 18, tmp_path)

    assert хватает is False
    assert свободно > 0


def test_резерв_равен_пятнадцати_процентам_отобранного(tmp_path, monkeypatch):
    monkeypatch.setattr(intake, "INDEX_RESERVE_MIN", 0)
    архив = tmp_path / "доля.zip"
    with zipfile.ZipFile(архив, "w") as zf:
        zf.writestr("Catalogs/Т/Ext/ObjectModule.bsl", "A" * 101)

    нужно, _ = planned_size(архив)

    assert нужно == 101 + 16


def test_резерв_не_меньше_двадцати_пяти_мегабайт(tmp_path):
    архив = tmp_path / "пол.zip"
    with zipfile.ZipFile(архив, "w") as zf:
        zf.writestr("Catalogs/Т/Ext/ObjectModule.bsl", "A")

    нужно, _ = planned_size(архив)

    assert нужно == 1 + 25 * 1024 * 1024


def test_существующий_корень_не_вычитается_из_уже_свободного_места(tmp_path):
    архив = tmp_path / "одна.zip"
    with zipfile.ZipFile(архив, "w") as zf:
        zf.writestr("Catalogs/Т/Ext/ObjectModule.bsl", "A" * 100)

    новый, _ = planned_size(архив, existing=False)
    переразбор, _ = planned_size(архив, existing=True)

    assert новый == 100 + 25 * 1024 * 1024
    assert переразбор == новый


def test_единая_карта_zip_нормализует_обёртку_и_берёт_последний_дубль(
    tmp_path
):
    archive = tmp_path / "normalized.zip"
    with zipfile.ZipFile(archive, "w") as zf:
        zf.writestr("._Wrap", "ресурсная вилка корневого каталога")
        zf.writestr("Wrap/Configuration.xml", "<повреждённый />")
        zf.writestr("Wrap/./Configuration.xml", modules_configuration_xml())
        zf.writestr("Wrap/manifest.xml", "старый манифест")
        zf.writestr("Wrap//./manifest.xml", "последний манифест")
        zf.writestr(
            "Wrap/CommonModules/Пример/Ext/./Module.bsl",
            "Процедура Старая()\nКонецПроцедуры",
        )
        zf.writestr(
            "Wrap//CommonModules/Пример/Ext/Module.bsl",
            "Процедура Новая() Экспорт\nКонецПроцедуры",
        )

    with zipfile.ZipFile(archive) as zf:
        карта = intake.карта_архива(zf)
        assert zf.read(карта["manifest.xml"]).decode() == "последний манифест"
        assert zf.read(карта["Configuration.xml"]).decode() == (
            modules_configuration_xml()
        )
        assert "Новая" in zf.read(
            карта["CommonModules/Пример/Ext/Module.bsl"]
        ).decode()
