"""Единая карта архива и отбор: что из ZIP считается кодом и под каким именем.

Распаковки в каталог кода больше нет, но тот же отбор решает, похож ли ZIP
в bootstrap на выгрузку в файлы. Проверяется результат
`intake._отобранные_записи`: имена после снятия обёртки и выбранная запись.
"""
import warnings
import zipfile
from pathlib import Path

from conftest import modules_configuration_xml
from mcp1c import intake


def _отбор(архив: Path) -> tuple[dict[str, bytes], str]:
    with zipfile.ZipFile(архив) as zf:
        записи, формат = intake._отобранные_записи(zf)
        return {имя: zf.read(info) for имя, info in записи.items()}, формат


def _архив(tmp_path: Path, члены: list[tuple[str, str | bytes]], имя="в.zip") -> Path:
    путь = tmp_path / имя
    with warnings.catch_warnings():
        # Дублирующееся имя при сборке архива — ожидаемый UserWarning zipfile.
        warnings.simplefilter("ignore", UserWarning)
        with zipfile.ZipFile(путь, "w") as zf:
            for член, тело in члены:
                zf.writestr(член, тело)
    return путь


def test_отбирается_только_код(tmp_path):
    отобрано, _ = _отбор(
        _архив(
            tmp_path,
            [
                ("Catalogs/Т/Ext/ObjectModule.bsl", "A" * 10),
                ("Ext/ParentConfigurations/П.cf", "C" * 100),
            ],
        )
    )

    assert отобрано == {"Catalogs/Т/Ext/ObjectModule.bsl": b"A" * 10}


def test_отбираются_дескриптор_формы_и_form_bin(tmp_path):
    отобрано, формат = _отбор(
        _архив(
            tmp_path,
            [
                ("Documents/Заказ/Forms/Основная.xml", b"descriptor"),
                ("Documents/Заказ/Forms/Основная/Ext/Form.bin", b"container"),
                ("Documents/Заказ/Ext/Other.bin", b"ballast"),
                ("Documents/Заказ.xml", b"metadata"),
                ("Configuration.xml", b"configuration"),
            ],
        )
    )

    assert формат == "tree"
    assert set(отобрано) == {
        "Documents/Заказ/Forms/Основная.xml",
        "Documents/Заказ/Forms/Основная/Ext/Form.bin",
    }


def test_член_наружу_не_отбирается(tmp_path):
    отобрано, _ = _отбор(_архив(tmp_path, [("../наружу.bsl", "X")]))

    assert отобрано == {}


def test_из_дублей_берётся_последний(tmp_path):
    # Второй каталог верхнего уровня — чтобы не сработало правило обёртки.
    отобрано, _ = _отбор(
        _архив(
            tmp_path,
            [
                ("Catalogs/Т/Ext/ObjectModule.bsl", "A" * 100),
                ("Catalogs/Т/Ext/ObjectModule.bsl", "B" * 999),
                ("Ext/ParentConfigurations/П.cf", "C" * 100),
            ],
        )
    )

    assert отобрано == {"Catalogs/Т/Ext/ObjectModule.bsl": b"B" * 999}


def test_обёртка_архива_снимается_с_имён(tmp_path):
    """`zip -r архив.zip папка` или Finder: всё внутри одного каталога."""
    отобрано, _ = _отбор(
        _архив(
            tmp_path,
            [
                ("Расширение/Catalogs/Т/Ext/ObjectModule.bsl", "A"),
                ("Расширение/Catalogs/Т/Forms/Ф/Ext/Form.xml", "B"),
            ],
        )
    )

    assert set(отобрано) == {
        "Catalogs/Т/Ext/ObjectModule.bsl",
        "Catalogs/Т/Forms/Ф/Ext/Form.xml",
    }


def test_несколько_каталогов_верхнего_уровня_не_обёртка(tmp_path):
    """Угадывать, какой из двух каталогов «тот самый», рискованнее, чем не
    снимать уровень вовсе."""
    отобрано, _ = _отбор(
        _архив(
            tmp_path,
            [
                ("А/Catalogs/Т/Ext/ObjectModule.bsl", "A"),
                ("Б/Catalogs/Д/Ext/ObjectModule.bsl", "B"),
            ],
        )
    )

    assert set(отобрано) == {
        "А/Catalogs/Т/Ext/ObjectModule.bsl",
        "Б/Catalogs/Д/Ext/ObjectModule.bsl",
    }


def test_мусор_finder_не_ломает_обёртку_и_не_отбирается(tmp_path):
    отобрано, _ = _отбор(
        _архив(
            tmp_path,
            [
                (".DS_Store", b"\x00\x00\x00\x00"),
                ("__MACOSX/", ""),
                ("__MACOSX/Расширение/", ""),
                ("__MACOSX/Расширение/._ObjectModule.bsl", b"\x00\x05\x16\x07"),
                ("Расширение/Catalogs/Т/Ext/ObjectModule.bsl", "A" * 10),
            ],
        )
    )

    assert отобрано == {"Catalogs/Т/Ext/ObjectModule.bsl": b"A" * 10}


def test_опасный_и_finder_мусор_не_переключают_формат_и_не_ломают_обёртку(
    tmp_path,
):
    отобрано, формат = _отбор(
        _архив(
            tmp_path,
            [
                ("Обёртка/Catalogs/Т/Ext/ObjectModule.bsl", "A" * 10),
                ("../escape.Form", "B" * 100),
                ("__MACOSX/junk.Form", "C" * 100),
                ("../CommonModule.Ложный.Module", "D" * 100),
            ],
        )
    )

    assert формат == "tree"
    assert отобрано == {"Catalogs/Т/Ext/ObjectModule.bsl": b"A" * 10}


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
