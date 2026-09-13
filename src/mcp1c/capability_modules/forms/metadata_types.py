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
METADATA_OBJECT_KINDS = tuple(_REGISTRY_KIND_TO_XML_OBJECT)
_XML_OBJECT_TO_REGISTRY_KIND = {
    xml_kind: registry_kind
    for registry_kind, xml_kind in _REGISTRY_KIND_TO_XML_OBJECT.items()
}

_REGISTRY_KIND_TO_XML_REFERENCE = {
    "Справочник": "CatalogRef",
    "Документ": "DocumentRef",
    "Перечисление": "EnumRef",
    "ПланОбмена": "ExchangePlanRef",
    "БизнесПроцесс": "BusinessProcessRef",
    "Задача": "TaskRef",
    "ПланВидовХарактеристик": "ChartOfCharacteristicTypesRef",
    "ПланСчетов": "ChartOfAccountsRef",
    "ПланВидовРасчета": "ChartOfCalculationTypesRef",
}
METADATA_REFERENCE_KINDS = tuple(_REGISTRY_KIND_TO_XML_REFERENCE)
_XML_REFERENCE_TO_REGISTRY_KIND = {
    xml_kind: registry_kind
    for registry_kind, xml_kind in _REGISTRY_KIND_TO_XML_REFERENCE.items()
}

_REGISTRY_KIND_TO_DYNAMIC_LIST_TABLE = {
    "Справочник": "Catalog",
    "Документ": "Document",
    "ЖурналДокументов": "DocumentJournal",
    "ПланОбмена": "ExchangePlan",
    "ПланВидовХарактеристик": "ChartOfCharacteristicTypes",
    "ПланСчетов": "ChartOfAccounts",
    "ПланВидовРасчета": "ChartOfCalculationTypes",
    "РегистрСведений": "InformationRegister",
    "РегистрНакопления": "AccumulationRegister",
    "РегистрБухгалтерии": "AccountingRegister",
    "РегистрРасчета": "CalculationRegister",
    "БизнесПроцесс": "BusinessProcess",
    "Задача": "Task",
    "КритерийОтбора": "FilterCriterion",
}
DYNAMIC_LIST_KINDS = tuple(_REGISTRY_KIND_TO_DYNAMIC_LIST_TABLE)
_DYNAMIC_LIST_TABLE_TO_REGISTRY_KIND = {
    table_kind: registry_kind
    for registry_kind, table_kind in _REGISTRY_KIND_TO_DYNAMIC_LIST_TABLE.items()
}


def _canonical_reference_pattern(kinds: tuple[str, ...]) -> str:
    alternatives = "|".join(re.escape(kind) for kind in kinds)
    identifier = r"[A-Za-zА-Яа-яЁё_][A-Za-zА-Яа-яЁё0-9_]*"
    return rf"^(?:{alternatives})\.{identifier}$"


METADATA_OBJECT_REF_PATTERN = _canonical_reference_pattern(METADATA_OBJECT_KINDS)
METADATA_REFERENCE_REF_PATTERN = _canonical_reference_pattern(
    METADATA_REFERENCE_KINDS
)
DYNAMIC_LIST_REF_PATTERN = _canonical_reference_pattern(DYNAMIC_LIST_KINDS)


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


def metadata_reference_xml_type(value: str) -> str | None:
    """Преобразовать ссылочный тип Registry в тип Form.xml."""

    parts = value.split(".")
    if len(parts) != 2 or not all(_IDENTIFIER.fullmatch(part) for part in parts):
        return None
    kind, name = parts
    xml_kind = _REGISTRY_KIND_TO_XML_REFERENCE.get(kind)
    if xml_kind is None:
        return None
    return f"cfg:{xml_kind}.{name}"


def metadata_reference_registry_ref(value: str) -> str | None:
    """Преобразовать поддержанный ссылочный тип Form.xml в ссылку Registry."""

    if not value.startswith("cfg:"):
        return None
    parts = value.removeprefix("cfg:").split(".")
    if len(parts) != 2 or not all(_IDENTIFIER.fullmatch(part) for part in parts):
        return None
    xml_kind, name = parts
    registry_kind = _XML_REFERENCE_TO_REGISTRY_KIND.get(xml_kind)
    if registry_kind is None:
        return None
    return f"{registry_kind}.{name}"


def dynamic_list_xml_table(value: str) -> str | None:
    """Преобразовать прямую ссылку Registry в MainTable динамического списка."""

    parts = value.split(".")
    if len(parts) != 2 or not all(_IDENTIFIER.fullmatch(part) for part in parts):
        return None
    kind, name = parts
    table_kind = _REGISTRY_KIND_TO_DYNAMIC_LIST_TABLE.get(kind)
    if table_kind is None:
        return None
    return f"{table_kind}.{name}"


def dynamic_list_registry_ref(value: str) -> str | None:
    """Преобразовать поддержанную MainTable в прямую ссылку Registry."""

    parts = value.split(".")
    if len(parts) != 2 or not all(_IDENTIFIER.fullmatch(part) for part in parts):
        return None
    table_kind, name = parts
    registry_kind = _DYNAMIC_LIST_TABLE_TO_REGISTRY_KIND.get(table_kind)
    if registry_kind is None:
        return None
    return f"{registry_kind}.{name}"
