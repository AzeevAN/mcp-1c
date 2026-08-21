"""Загрузчик не принимает выгрузку с нарушенным контрактом schema v1.

Один раз обработка выгрузки положила в поле `type` — вид метаданных — список
типов значения плана видов характеристик: ключ на уровне объекта тот же, а
`Структура.Вставить` заменяет значение молча. Разбор при этом проходил, и
падало гораздо позже, на построении поискового индекса, с
`TypeError: unhashable type: 'list'` — по стеку причина не читалась.

Загружать такое нельзя: вид объекта участвует в весах поиска, в графе и в
подписях выдачи. Лучше отказ с внятным текстом, чем модель, испорченная тихо.
"""

import json
import zipfile

import pytest

from mcp1c.loader import ExportError, load
from mcp1c.model import MetadataObject

from conftest import build_configuration, write_export


def _write_export(directory, objects: list[dict]):
    """Минимальная выгрузка schema v1 в формате JSON."""
    manifest = {
        "schema_version": "1",
        "format": "json",
        "exporter_version": "test",
        "name": "ТестоваяКонфигурация",
        "version": "1.0",
        "platform": "8.3.23.1997",
        "exported_at": "2026-08-17T00:00:00",
        "objects_total": len(objects),
        "files": [
            {
                "path": "objects/chartofcharacteristictypes.001.json",
                "type": "ПланВидовХарактеристик",
                "count": len(objects),
            }
        ],
    }

    target = directory / "СтруктураКонфигурации_Тестовая.zip"
    with zipfile.ZipFile(target, "w") as archive:
        archive.writestr("manifest.json", json.dumps(manifest, ensure_ascii=False))
        archive.writestr(
            "objects/chartofcharacteristictypes.001.json",
            json.dumps({"objects": objects}, ensure_ascii=False),
        )
    return target


def test_вид_объекта_списком_отвергается(tmp_path):
    """Ровно тот случай: `type` затёрт массивом типов значения."""
    архив = _write_export(
        tmp_path,
        [
            {
                "full_name": "ПланВидовХарактеристик.ВопросыДляАнкетирования",
                "type": ["Строка", "Число"],
                "name": "ВопросыДляАнкетирования",
                "synonym": "Вопросы для анкетирования",
            }
        ],
    )

    with pytest.raises(ExportError) as ошибка:
        load(архив)

    # В тексте должно быть видно, какой объект и что именно пришло, — иначе
    # отказ не лучше прежнего падения где-то ниже по стеку.
    assert "ПланВидовХарактеристик.ВопросыДляАнкетирования" in str(ошибка.value)
    assert "list" in str(ошибка.value)


def test_булево_вместо_списка_предопределённых_отвергается(tmp_path):
    """Второй случай того же рода: `predefined` занят флагом регламентного задания.

    Загрузчик перебирает `predefined` как список элементов справочника, и
    булево роняло загрузку — «'bool' object is not iterable».
    """
    архив = _write_export(
        tmp_path,
        [
            {
                "full_name": "РегламентноеЗадание.АктуализацияДанных",
                "type": "РегламентноеЗадание",
                "name": "АктуализацияДанных",
                "synonym": "Актуализация данных",
                "predefined": True,
            }
        ],
    )

    with pytest.raises(ExportError) as ошибка:
        load(архив)

    assert "predefined" in str(ошибка.value)
    assert "bool" in str(ошибка.value)


def test_нарушения_перечисляются_все_сразу(tmp_path):
    """Чинить по одному значит перевыгружать конфигурацию на каждую ошибку."""
    архив = _write_export(
        tmp_path,
        [
            {
                "full_name": f"РегламентноеЗадание.Задание{номер}",
                "type": "РегламентноеЗадание",
                "name": f"Задание{номер}",
                "synonym": "",
                "predefined": True,
            }
            for номер in range(3)
        ],
    )

    with pytest.raises(ExportError) as ошибка:
        load(архив)

    текст = str(ошибка.value)
    assert "3 объектов" in текст
    for номер in range(3):
        assert f"Задание{номер}" in текст


def test_правильная_выгрузка_плана_видов_характеристик_грузится(tmp_path):
    """Тип значения живёт в `value_type`, а `type` остаётся видом объекта."""
    архив = _write_export(
        tmp_path,
        [
            {
                "full_name": "ПланВидовХарактеристик.ВопросыДляАнкетирования",
                "type": "ПланВидовХарактеристик",
                "name": "ВопросыДляАнкетирования",
                "synonym": "Вопросы для анкетирования",
                "value_type": {"type": ["Строка", "Число"], "string_length": 150},
            }
        ],
    )

    config = load(архив)
    obj = config.objects["ПланВидовХарактеристик.ВопросыДляАнкетирования"]

    assert obj.kind == "ПланВидовХарактеристик"
    assert obj.value_type is not None
    assert obj.value_type.types == ["Строка", "Число"]


@pytest.mark.parametrize(
    ("reuse", "expected"),
    [("DuringCall", "DuringCall"), ("DontUse", "DontUse")],
)
def test_xml_свойства_общего_модуля_сохраняют_типы(tmp_path, reuse, expected):
    """Булевы строки приводятся, а строковое перечисление не теряется."""
    manifest = (
        '<manifest schema_version="1" format="xml" name="Пример" version="1.0" '
        'platform="8.3.23.1997" objects_total="1">'
        '<files><item path="objects/common.xml" type="ОбщийМодуль" count="1"/></files>'
        "</manifest>"
    )
    objects = (
        '<objects><object full_name="ОбщийМодуль.Пример" type="ОбщийМодуль" '
        'name="Пример" privileged="false" external_connection="false" '
        f'return_values_reuse="{reuse}" server="true"/></objects>'
    )
    архив = tmp_path / "структура.zip"
    with zipfile.ZipFile(архив, "w") as zf:
        zf.writestr("manifest.xml", manifest)
        zf.writestr("objects/common.xml", objects)

    config = load(архив)
    props = config.objects["ОбщийМодуль.Пример"].props

    assert props["server"] is True
    assert props["privileged"] is False
    assert props["external_connection"] is False
    assert props["return_values_reuse"] == expected


@pytest.mark.parametrize("reuse", ["DuringCall", "DontUse"])
def test_json_return_values_reuse_остаётся_строковым_enum(tmp_path, reuse):
    config = build_configuration(name="Пример")
    config.objects["ОбщийМодуль.Пример"] = MetadataObject(
        full_name="ОбщийМодуль.Пример",
        kind="ОбщийМодуль",
        name="Пример",
        props={"return_values_reuse": reuse},
    )

    loaded = load(write_export(tmp_path, config))

    assert loaded.objects["ОбщийМодуль.Пример"].props["return_values_reuse"] == reuse
