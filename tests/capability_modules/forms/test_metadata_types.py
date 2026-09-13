import pytest

from mcp1c.capability_modules.forms.metadata_types import (
    metadata_object_registry_ref,
    metadata_object_xml_type,
)


@pytest.mark.parametrize(
    ("registry_ref", "xml_type"),
    [
        ("Справочник.Товары", "cfg:CatalogObject.Товары"),
        ("Документ.Заказ", "cfg:DocumentObject.Заказ"),
        ("Обработка.Импорт", "cfg:DataProcessorObject.Импорт"),
        ("Отчет.Остатки", "cfg:ReportObject.Остатки"),
        ("ПланОбмена.Основной", "cfg:ExchangePlanObject.Основной"),
        ("БизнесПроцесс.Заявка", "cfg:BusinessProcessObject.Заявка"),
        ("Задача.Исполнение", "cfg:TaskObject.Исполнение"),
        (
            "ПланВидовХарактеристик.Свойства",
            "cfg:ChartOfCharacteristicTypesObject.Свойства",
        ),
        ("ПланСчетов.Основной", "cfg:ChartOfAccountsObject.Основной"),
        (
            "ПланВидовРасчета.Начисления",
            "cfg:ChartOfCalculationTypesObject.Начисления",
        ),
    ],
)
def test_объектный_тип_имеет_взаимно_однозначное_отображение(
    registry_ref, xml_type
):
    assert metadata_object_xml_type(registry_ref) == xml_type
    assert metadata_object_registry_ref(xml_type) == registry_ref


@pytest.mark.parametrize(
    "value",
    [
        "НеизвестныйВид.Объект",
        "Обработка",
        "Обработка.Неверное-Имя",
        "Обработка.Имя.Лишнее",
    ],
)
def test_неподдержанная_ссылка_registry_не_преобразуется(value):
    assert metadata_object_xml_type(value) is None


@pytest.mark.parametrize(
    "value",
    [
        "cfg:UnknownObject.Объект",
        "cfg:DataProcessorObject",
        "cfg:DataProcessorObject.Неверное-Имя",
        "cfg:DataProcessorObject.Имя.Лишнее",
    ],
)
def test_неподдержанный_xml_тип_не_выдаётся_за_объект_registry(value):
    assert metadata_object_registry_ref(value) is None
