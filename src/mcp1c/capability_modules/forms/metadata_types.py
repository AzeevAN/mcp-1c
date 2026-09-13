"""Закрытые отображения объектных типов метаданных формы."""

from __future__ import annotations

import re


_IDENTIFIER = re.compile(r"[A-Za-zА-Яа-яЁё_][A-Za-zА-Яа-яЁё0-9_]*\Z")

_REGISTRY_KIND_TO_XML_OBJECT = {
    "Справочник": "CatalogObject",
    "Документ": "DocumentObject",
    "Обработка": "DataProcessorObject",
    "Отчет": "ReportObject",
    "ПланОбмена": "ExchangePlanObject",
    "БизнесПроцесс": "BusinessProcessObject",
    "Задача": "TaskObject",
    "ПланВидовХарактеристик": "ChartOfCharacteristicTypesObject",
    "ПланСчетов": "ChartOfAccountsObject",
    "ПланВидовРасчета": "ChartOfCalculationTypesObject",
}
_XML_OBJECT_TO_REGISTRY_KIND = {
    xml_kind: registry_kind
    for registry_kind, xml_kind in _REGISTRY_KIND_TO_XML_OBJECT.items()
}


def metadata_object_xml_type(value: str) -> str | None:
    """Преобразовать каноническую ссылку Registry в тип Form.xml."""

    parts = value.split(".")
    if len(parts) != 2 or not all(_IDENTIFIER.fullmatch(part) for part in parts):
        return None
    kind, name = parts
    xml_kind = _REGISTRY_KIND_TO_XML_OBJECT.get(kind)
    if xml_kind is None:
        return None
    return f"cfg:{xml_kind}.{name}"


def metadata_object_registry_ref(value: str) -> str | None:
    """Преобразовать поддержанный объектный тип Form.xml в ссылку Registry."""

    if not value.startswith("cfg:"):
        return None
    parts = value.removeprefix("cfg:").split(".")
    if len(parts) != 2 or not all(_IDENTIFIER.fullmatch(part) for part in parts):
        return None
    xml_kind, name = parts
    registry_kind = _XML_OBJECT_TO_REGISTRY_KIND.get(xml_kind)
    if registry_kind is None:
        return None
    return f"{registry_kind}.{name}"
