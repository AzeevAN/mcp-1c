"""Требуемый объём считается по центральному каталогу, без распаковки."""
import zipfile
from pathlib import Path

from conftest import modules_configuration_xml
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
