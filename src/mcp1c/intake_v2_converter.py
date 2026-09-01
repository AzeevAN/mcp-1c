"""Преобразование collection source B в канонические слои структуры.

Базовая проекция использует существующую ``model.Configuration``.
Расширенный слой хранит только новые объекты и overlays ссылок на код, формы
и связи — поля schema v1 в нём не дублируются.
"""

from __future__ import annotations

import hashlib
import json
import xml.etree.ElementTree as ET
from dataclasses import dataclass, fields, is_dataclass
from enum import Enum
from pathlib import PurePosixPath
from types import MappingProxyType
from typing import Iterable, Mapping

from .bsl_lex import нормализовать, разобрать
from .form_reader import FormReadError, read_form
from .form_structure import (
    FormStructureError,
    parse_form_descriptor,
    parse_form_xml,
)
from .intake_v2 import LayerKind, MetadataKindSpec
from .intake_v2_collector import (
    DEFAULT_KIND_SPECS,
    ArtifactKind,
    CollectionArtifact,
    CollectionError,
    CollectionResult,
    open_collection_member,
)
from .model import Configuration, Field, MetadataObject, TabularPart
from .v8container import V8Container, V8ContainerError, V8ResourceLimitError


_NS_MDCLASSES = "http://v8.1c.ru/8.3/MDClasses"
_NS_EXTERNAL_PROPERTIES = "http://v8.1c.ru/8.3/xcf/extrnprops"
_MAX_DIAGNOSTIC_EXAMPLES = 3


class ConversionError(RuntimeError):
    """Известный структурный контракт source B нарушен."""


class RelationState(str, Enum):
    RESOLVED = "resolved"
    UNRESOLVED = "unresolved"


class AutoRecordPolicy(str, Enum):
    ALLOW = "Allow"
    DENY = "Deny"


class BindingState(str, Enum):
    """Состояние ссылки metadata на процедуру общего модуля."""

    UNAVAILABLE = "unavailable"
    RESOLVED = "resolved"
    UNRESOLVED = "unresolved"
    MODULE_MISSING = "module_missing"
    PROCEDURE_MISSING = "procedure_missing"


@dataclass(frozen=True, slots=True)
class TypeDescription:
    """Тип из файловой выгрузки вместе с квалификаторами."""

    types: tuple[str, ...] = ()
    string_length: int | None = None
    string_allowed_length: str = ""
    digits: int | None = None
    fraction_digits: int | None = None
    number_allowed_sign: str = ""
    date_parts: str = ""

    def to_field(self, name: str, **head: str) -> Field:
        return Field(
            name=name,
            synonym=head.get("synonym", ""),
            comment=head.get("comment", ""),
            indexing=head.get("indexing", ""),
            types=list(self.types),
            string_length=self.string_length,
            digits=self.digits,
            fraction_digits=self.fraction_digits,
            date_parts=self.date_parts,
        )


@dataclass(frozen=True, slots=True)
class MetadataRelation:
    kind: str
    target: str
    state: RelationState
    properties: tuple[tuple[str, str], ...] = ()


@dataclass(frozen=True, slots=True)
class CommonAttributePayload:
    value_type: TypeDescription
    password_mode: bool | None = None
    format: str = ""
    edit_format: str = ""
    tooltip: str = ""
    mark_negatives: bool | None = None
    mask: str = ""
    multi_line: bool | None = None
    extended_edit: bool | None = None
    min_value: str = ""
    max_value: str = ""
    fill_from_filling_value: bool | None = None
    fill_value: str = ""
    fill_checking: str = ""
    choice_folders_and_items: str = ""
    choice_form: str = ""
    quick_choice: str = ""
    create_on_input: str = ""
    data_history: str = ""
    indexing: str = ""
    full_text_search: str = ""
    data_separation: str = ""
    data_separation_value: str = ""
    data_separation_use: str = ""
    conditional_separation: str = ""
    separated_data_use: str = ""
    auto_use: str = ""
    authentication_separation: str = ""
    users_separation: str = ""
    configuration_extensions_separation: str = ""
    link_by_type: str = ""


@dataclass(frozen=True, slots=True)
class SessionParameterPayload:
    value_type: TypeDescription


@dataclass(frozen=True, slots=True)
class JournalStandardAttribute:
    name: str
    synonym: str = ""
    comment: str = ""
    password_mode: bool | None = None
    format: str = ""
    edit_format: str = ""
    tooltip: str = ""
    mark_negatives: bool | None = None
    mask: str = ""
    multi_line: bool | None = None
    extended_edit: bool | None = None
    min_value: str = ""
    max_value: str = ""
    fill_from_filling_value: bool | None = None
    fill_value: str = ""
    fill_checking: str = ""
    choice_form: str = ""
    choice_history_on_input: str = ""
    quick_choice: str = ""
    create_on_input: str = ""
    data_history: str = ""
    full_text_search: str = ""
    link_by_type: str = ""
    type_reduction_mode: str = ""


@dataclass(frozen=True, slots=True)
class JournalColumnReference:
    raw: str
    target: str


@dataclass(frozen=True, slots=True)
class JournalColumn:
    name: str
    synonym: str = ""
    comment: str = ""
    indexing: str = ""
    references: tuple[JournalColumnReference, ...] = ()


@dataclass(frozen=True, slots=True)
class JournalCommand:
    name: str
    synonym: str = ""
    comment: str = ""
    parameter_type: TypeDescription = TypeDescription()
    group: str = ""
    modifies_data: bool | None = None
    parameter_use_mode: str = ""
    representation: str = ""
    shortcut: str = ""
    tooltip: str = ""
    picture: tuple[str, ...] = ()
    server_unavailable_behavior: str = ""


@dataclass(frozen=True, slots=True)
class DocumentJournalPayload:
    list_presentation: str = ""
    extended_list_presentation: str = ""
    explanation: str = ""
    default_form: str = ""
    auxiliary_form: str = ""
    use_standard_commands: bool | None = None
    include_help_in_contents: bool | None = None
    registered_documents: tuple[str, ...] = ()
    standard_attributes: tuple[JournalStandardAttribute, ...] = ()
    columns: tuple[JournalColumn, ...] = ()
    commands: tuple[JournalCommand, ...] = ()
    forms: tuple[str, ...] = ()
    templates: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class ExchangePlanContentItem:
    raw: str
    target: str
    auto_record: AutoRecordPolicy


@dataclass(frozen=True, slots=True)
class ExchangePlanPayload:
    """Свойства source B сверх schema-v1-проекции плана обмена."""

    based_on: tuple[str, ...] = ()
    characteristics: str = ""
    choice_data_get_mode_on_input_by_string: str = ""
    choice_history_on_input: str = ""
    choice_mode: str = ""
    code_allowed_length: str = ""
    code_length: int | None = None
    create_on_input: str = ""
    data_history: str = ""
    data_lock_control_mode: str = ""
    data_lock_fields: tuple[str, ...] = ()
    default_presentation: str = ""
    description_length: int | None = None
    edit_type: str = ""
    execute_after_write_data_history_version_processing: bool | None = None
    explanation: str = ""
    extended_list_presentation: str = ""
    extended_object_presentation: str = ""
    full_text_search: str = ""
    full_text_search_on_input_by_string: str = ""
    include_configuration_extensions: bool | None = None
    include_help_in_contents: bool | None = None
    input_by_string: tuple[str, ...] = ()
    list_presentation: str = ""
    object_presentation: str = ""
    quick_choice: bool | None = None
    search_string_mode_on_input_by_string: str = ""
    update_data_history_immediately_after_write: bool | None = None
    use_standard_commands: bool | None = None
    default_choice_form: str = ""
    auxiliary_choice_form: str = ""
    default_list_form: str = ""
    auxiliary_list_form: str = ""
    default_object_form: str = ""
    auxiliary_object_form: str = ""
    standard_attributes: tuple[JournalStandardAttribute, ...] = ()
    commands: tuple[JournalCommand, ...] = ()
    forms: tuple[str, ...] = ()
    templates: tuple[str, ...] = ()
    content: tuple[ExchangePlanContentItem, ...] = ()


@dataclass(frozen=True, slots=True)
class CodeBinding:
    raw: str
    state: BindingState
    module_address: str = ""
    procedure_address: str = ""


@dataclass(frozen=True, slots=True)
class EventSubscriptionPayload:
    binding: CodeBinding


@dataclass(frozen=True, slots=True)
class ScheduledJobPayload:
    description: str
    restart_count_on_failure: int | None
    restart_interval_on_failure: int | None
    binding: CodeBinding


class FormStructureState(str, Enum):
    READY = "ready"
    PARTIAL = "partial"
    UNREADABLE = "unreadable"
    DESCRIPTOR_ONLY = "descriptor_only"


class FormModuleState(str, Enum):
    READY = "ready"
    MISSING = "module_missing"
    UNREADABLE = "unreadable"


@dataclass(frozen=True, slots=True)
class FormEventBinding:
    element: str | None
    event: str
    handler: str
    binding: CodeBinding


@dataclass(frozen=True, slots=True)
class CommonFormPayload:
    uuid: str = ""
    form_type: str = ""
    explanation: str = ""
    extended_presentation: str = ""
    include_help_in_contents: bool | None = None
    use_purposes: tuple[str, ...] = ()
    use_standard_commands: bool | None = None
    structure_state: FormStructureState = FormStructureState.DESCRIPTOR_ONLY
    module_state: FormModuleState = FormModuleState.MISSING
    container_marker: int | None = None
    attributes: tuple[str, ...] = ()
    elements: tuple[str, ...] = ()
    events: tuple[FormEventBinding, ...] = ()


@dataclass(frozen=True, slots=True)
class ExtendedObject:
    full_name: str
    kind: str
    name: str
    synonym: str = ""
    comment: str = ""
    code_address: str = ""
    base_object: bool = False
    payload: object | None = None
    modules: tuple[str, ...] = ()
    forms: tuple[str, ...] = ()
    relations: tuple[MetadataRelation, ...] = ()


@dataclass(frozen=True, slots=True)
class ExtendedStructure:
    objects: Mapping[str, ExtendedObject]

    def __post_init__(self) -> None:
        ordered = dict(sorted(self.objects.items(), key=lambda item: _order(item[0])))
        if any(key != value.full_name for key, value in ordered.items()):
            raise ConversionError("ключ extended object не совпадает с full_name")
        object.__setattr__(self, "objects", MappingProxyType(ordered))

    def __len__(self) -> int:
        return len(self.objects)

    def get(self, full_name: str) -> ExtendedObject | None:
        return self.objects.get(full_name)


@dataclass(frozen=True, slots=True)
class ConversionDiagnostic:
    code: str
    signature: str
    count: int
    examples: tuple[str, ...]
    severity: str = "info"


@dataclass(frozen=True, slots=True)
class StructureConversion:
    base: Configuration
    extended: ExtendedStructure
    base_content_sha256: str
    extended_content_sha256: str
    diagnostics: tuple[ConversionDiagnostic, ...] = ()


class _Diagnostics:
    def __init__(self) -> None:
        self._items: dict[tuple[str, str, str], list[object]] = {}

    def add(
        self,
        code: str,
        signature: str,
        example: str,
        *,
        severity: str = "info",
    ) -> None:
        item = self._items.setdefault((code, signature, severity), [0, []])
        item[0] = int(item[0]) + 1
        examples = item[1]
        assert isinstance(examples, list)
        if example not in examples and len(examples) < _MAX_DIAGNOSTIC_EXAMPLES:
            examples.append(example)

    def freeze(self) -> tuple[ConversionDiagnostic, ...]:
        return tuple(
            ConversionDiagnostic(code, signature, int(value[0]), tuple(value[1]), severity)
            for (code, signature, severity), value in sorted(self._items.items())
        )


def _order(value: str) -> tuple[str, str]:
    return value.casefold(), value


def _tag(element: ET.Element) -> str:
    return element.tag.rsplit("}", 1)[-1]


def _namespace(element: ET.Element) -> str:
    return element.tag[1:].split("}", 1)[0] if element.tag.startswith("{") else ""


def _child(element: ET.Element | None, name: str) -> ET.Element | None:
    if element is None:
        return None
    return next((item for item in element if _tag(item) == name), None)


def _text(element: ET.Element | None) -> str:
    return (element.text or "").strip() if element is not None else ""


def _leaf_texts(element: ET.Element | None) -> tuple[str, ...]:
    if element is None:
        return ()
    values = []
    for item in element.iter():
        if len(item) == 0 and _text(item):
            values.append(_text(item))
    return tuple(values)


def _required_text(element: ET.Element | None, label: str) -> str:
    value = _text(element)
    if not value:
        raise ConversionError(
            f"{label}: обязательное значение отсутствует"
        )
    return value


def _bool(element: ET.Element | None, label: str) -> bool | None:
    if element is None:
        return None
    value = _text(element)
    if value == "true":
        return True
    if value == "false":
        return False
    raise ConversionError(f"{label}: ожидается true или false")


def _int(element: ET.Element | None, label: str) -> int | None:
    if element is None or not _text(element):
        return None
    value = _text(element)
    if not value.isdecimal():
        raise ConversionError(
            f"{label}: ожидается неотрицательное целое"
        )
    return int(value)


def _localized(element: ET.Element | None) -> str:
    if element is None:
        return ""
    variants: list[tuple[str, str]] = []
    for item in element:
        language = _text(_child(item, "lang"))
        content = _text(_child(item, "content"))
        if content:
            variants.append((language, content))
    if not variants:
        return _text(element)
    return next((value for language, value in variants if language == "ru"), variants[0][1])


_TYPE_NAMES = {
    "xs:string": "Строка",
    "xs:decimal": "Число",
    "xs:boolean": "Булево",
    "xs:dateTime": "Дата",
    "v8:UUID": "УникальныйИдентификатор",
    "v8:ValueStorage": "ХранилищеЗначения",
    "v8:FixedArray": "ФиксированныйМассив",
    "v8:FixedMap": "ФиксированноеСоответствие",
    "v8:FixedStructure": "ФиксированнаяСтруктура",
    "v8:BinaryData": "ДвоичныеДанные",
}

_REFERENCE_KINDS = {
    "Catalog": "Справочник",
    "Document": "Документ",
    "InformationRegister": "РегистрСведений",
    "AccumulationRegister": "РегистрНакопления",
    "AccountingRegister": "РегистрБухгалтерии",
    "CalculationRegister": "РегистрРасчета",
    "Constant": "Константа",
    "Enum": "Перечисление",
    "ChartOfCharacteristicTypes": "ПланВидовХарактеристик",
    "ChartOfAccounts": "ПланСчетов",
    "ChartOfCalculationTypes": "ПланВидовРасчета",
    "ExchangePlan": "ПланОбмена",
    "BusinessProcess": "БизнесПроцесс",
    "Task": "Задача",
    "DefinedType": "ОпределяемыйТип",
    "CommonModule": "ОбщийМодуль",
    "EventSubscription": "ПодпискаНаСобытие",
    "ScheduledJob": "РегламентноеЗадание",
    "Report": "Отчет",
    "DataProcessor": "Обработка",
    "SessionParameter": "ПараметрСеанса",
    "CommonAttribute": "ОбщийРеквизит",
    "DocumentJournal": "ЖурналДокументов",
}

# В Source подписки платформа пишет не вид metadata, а runtime-тип объекта.
# Суффикс нельзя отрезать эвристикой: ``DefinedType`` оканчивается на Type,
# а ``DocumentJournalManager`` и ``ConstantValueManager`` имеют разные
# основы. Закрытая таблица составлена по фактическим source-B дескрипторам.
_RUNTIME_REFERENCE_KINDS = {
    "AccountingRegisterManager": "AccountingRegister",
    "AccountingRegisterRecordSet": "AccountingRegister",
    "AccumulationRegisterManager": "AccumulationRegister",
    "AccumulationRegisterRecordSet": "AccumulationRegister",
    "BusinessProcessManager": "BusinessProcess",
    "BusinessProcessObject": "BusinessProcess",
    "CalculationRegisterManager": "CalculationRegister",
    "CalculationRegisterRecordSet": "CalculationRegister",
    "CatalogManager": "Catalog",
    "CatalogObject": "Catalog",
    "ChartOfAccountsManager": "ChartOfAccounts",
    "ChartOfAccountsObject": "ChartOfAccounts",
    "ChartOfCalculationTypesManager": "ChartOfCalculationTypes",
    "ChartOfCalculationTypesObject": "ChartOfCalculationTypes",
    "ChartOfCharacteristicTypesManager": "ChartOfCharacteristicTypes",
    "ChartOfCharacteristicTypesObject": "ChartOfCharacteristicTypes",
    "ConstantValueManager": "Constant",
    "DataProcessorManager": "DataProcessor",
    "DefinedType": "DefinedType",
    "DocumentJournalManager": "DocumentJournal",
    "DocumentManager": "Document",
    "DocumentObject": "Document",
    "ExchangePlanObject": "ExchangePlan",
    "InformationRegisterManager": "InformationRegister",
    "InformationRegisterRecordSet": "InformationRegister",
    "ReportManager": "Report",
    "TaskManager": "Task",
    "TaskObject": "Task",
}


def _reference(value: str) -> str:
    if not value or "." not in value:
        return value
    kind, tail = value.split(".", 1)
    return f"{_REFERENCE_KINDS.get(kind, kind)}.{tail}"


_CONTENT_SEGMENTS = {
    "Form": "Форма",
    "Command": "Команда",
    "Template": "Макет",
}


def _content_address(value: str) -> str:
    parts = _reference(value).split(".")
    return ".".join(_CONTENT_SEGMENTS.get(part, part) for part in parts)


def _type_name(value: str) -> str:
    if value in _TYPE_NAMES:
        return _TYPE_NAMES[value]
    if value.startswith("cfg:") and "." in value:
        kind, tail = value.removeprefix("cfg:").split(".", 1)
        if kind.endswith("Ref"):
            kind = kind[:-3]
        kind = _RUNTIME_REFERENCE_KINDS.get(kind, kind)
        public_kind = _REFERENCE_KINDS.get(kind)
        if public_kind:
            return f"{public_kind}.{tail}"
    return value


def _type_description(element: ET.Element | None) -> TypeDescription:
    if element is None:
        return TypeDescription()
    raw_types = tuple(
        _text(item)
        for item in element
        if _tag(item) in {"Type", "TypeSet"} and _text(item)
    )
    string = _child(element, "StringQualifiers")
    number = _child(element, "NumberQualifiers")
    date = _child(element, "DateQualifiers")
    return TypeDescription(
        types=tuple(_type_name(value) for value in raw_types),
        string_length=_int(_child(string, "Length"), "длина строки"),
        string_allowed_length=_text(_child(string, "AllowedLength")),
        digits=_int(_child(number, "Digits"), "разрядность числа"),
        fraction_digits=_int(
            _child(number, "FractionDigits"), "разрядность дробной части"
        ),
        number_allowed_sign=_text(_child(number, "AllowedSign")),
        date_parts=_text(_child(date, "DateFractions")),
    )


_INDEXING = {
    "DontIndex": "",
    "Index": "Индексировать",
    "IndexWithAdditionalOrder": "ИндексироватьСДопУпорядочиванием",
}
_HIERARCHY_TYPES = {
    "HierarchyFoldersAndItems": "ИерархияГруппИЭлементов",
    "HierarchyItems": "ИерархияЭлементов",
}
_VALUE_TYPES = {"String": "Строка", "Number": "Число"}


def _field(
    element: ET.Element,
    diagnostics: _Diagnostics,
    where: str,
) -> Field:
    properties = _child(element, "Properties")
    if properties is None:
        raise ConversionError(f"{where}: у поля нет Properties")
    name = _required_text(_child(properties, "Name"), f"{where}.Name")
    known = {
        "Name",
        "Synonym",
        "Comment",
        "Indexing",
        "Type",
        "ChoiceFoldersAndItems",
        "ChoiceForm",
        "ChoiceHistoryOnInput",
        "ChoiceParameterLinks",
        "ChoiceParameters",
        "CreateOnInput",
        "DataHistory",
        "EditFormat",
        "ExtendedConfigurationObject",
        "ExtendedEdit",
        "FillChecking",
        "FillFromFillingValue",
        "FillValue",
        "Format",
        "FullTextSearch",
        "LinkByType",
        "MarkNegatives",
        "Mask",
        "MaxValue",
        "MinValue",
        "MultiLine",
        "ObjectBelonging",
        "PasswordMode",
        "QuickChoice",
        "ScheduleLink",
        "ToolTip",
        "Use",
    }
    _unknown_properties(properties, known, diagnostics, f"field:{where}")
    indexing_raw = _text(_child(properties, "Indexing"))
    indexing = _INDEXING.get(indexing_raw, indexing_raw)
    type_spec = _type_description(_child(properties, "Type"))
    return type_spec.to_field(
        name,
        synonym=_localized(_child(properties, "Synonym")),
        comment=_text(_child(properties, "Comment")),
        indexing=indexing,
    )


def _unknown_properties(
    properties: ET.Element,
    known: set[str] | frozenset[str],
    diagnostics: _Diagnostics,
    signature: str,
) -> None:
    for item in properties:
        name = _tag(item)
        if name not in known:
            diagnostics.add("unknown_property", signature, name)


_SCHEMA_KINDS = {
    "Catalogs": "Catalog",
    "Documents": "Document",
    "InformationRegisters": "InformationRegister",
    "AccumulationRegisters": "AccumulationRegister",
    "AccountingRegisters": "AccountingRegister",
    "CalculationRegisters": "CalculationRegister",
    "Constants": "Constant",
    "Enums": "Enum",
    "ChartsOfCharacteristicTypes": "ChartOfCharacteristicTypes",
    "ChartsOfAccounts": "ChartOfAccounts",
    "ChartsOfCalculationTypes": "ChartOfCalculationTypes",
    "ExchangePlans": "ExchangePlan",
    "BusinessProcesses": "BusinessProcess",
    "Tasks": "Task",
    "DefinedTypes": "DefinedType",
    "CommonModules": "CommonModule",
    "EventSubscriptions": "EventSubscription",
    "ScheduledJobs": "ScheduledJob",
    "Reports": "Report",
    "DataProcessors": "DataProcessor",
}

_BASE_HEAD = frozenset({"Name", "Synonym", "Comment"})
_BASE_PROPERTIES: dict[str, dict[str, tuple[str, str]]] = {
    "Catalog": {
        "Hierarchical": ("hierarchical", "bool"),
        "HierarchyType": ("hierarchy_type", "hierarchy"),
        "CodeLength": ("code_length", "int"),
        "DescriptionLength": ("description_length", "int"),
        "CodeType": ("code_type", "value_type"),
        "Owners": ("owners", "refs"),
    },
    "Document": {
        "Posting": ("posting", "text"),
        "NumberLength": ("number_length", "int"),
        "NumberPeriodicity": ("number_periodicity", "text"),
        "NumberType": ("number_type", "value_type"),
        "Numerator": ("numerator", "ref"),
        "RealTimePosting": ("real_time_posting", "text"),
        "RegisterRecordsDeletion": ("register_records_deletion", "text"),
        "RegisterRecordsWritingOnPost": ("register_records_on_post", "text"),
        "RegisterRecords": ("movements", "refs"),
        "BasedOn": ("based_on", "refs"),
    },
    "InformationRegister": {
        "InformationRegisterPeriodicity": ("periodicity", "text"),
        "WriteMode": ("write_mode", "text"),
    },
    "AccumulationRegister": {"RegisterType": ("register_kind", "text")},
    "AccountingRegister": {
        "Correspondence": ("correspondence", "bool"),
        "ChartOfAccounts": ("chart_of_accounts", "ref"),
        "PeriodAdjustmentLength": ("period_adjustment_length", "int"),
    },
    "CalculationRegister": {
        "ActionPeriod": ("action_period", "bool"),
        "BasePeriod": ("base_period", "bool"),
        "Periodicity": ("periodicity", "text"),
        "ChartOfCalculationTypes": ("chart_of_calculation_types", "ref"),
        "Schedule": ("schedule", "ref"),
        "ScheduleDate": ("schedule_date", "short_ref"),
        "ScheduleValue": ("schedule_value", "short_ref"),
    },
    "ChartOfAccounts": {
        "ExtDimensionTypes": ("ext_dimension_types", "ref"),
        "MaxExtDimensionCount": ("max_ext_dimension_count", "int"),
        "CodeMask": ("code_mask", "text"),
        "CodeLength": ("code_length", "int"),
        "DescriptionLength": ("description_length", "int"),
    },
    "ChartOfCalculationTypes": {
        "DependenceOnCalculationTypes": ("dependence_on_calculation_types", "text"),
        "ActionPeriodUse": ("action_period_use", "bool"),
        "BaseCalculationTypes": ("base_calculation_types", "refs"),
    },
    "ChartOfCharacteristicTypes": {
        "CharacteristicExtValues": ("characteristic_ext_values", "ref"),
        "Hierarchical": ("hierarchical", "bool"),
    },
    "Task": {
        "Addressing": ("addressing", "ref"),
        "MainAddressingAttribute": ("main_addressing_attribute", "short_ref"),
        "CurrentPerformer": ("current_performer", "short_ref"),
    },
    "BusinessProcess": {"Task": ("task", "ref")},
    "ExchangePlan": {"DistributedInfoBase": ("distributed_infobase", "bool")},
    "CommonModule": {
        "Global": ("global", "bool"),
        "Server": ("server", "bool"),
        "ClientManagedApplication": ("client_managed", "bool"),
        "ServerCall": ("server_call", "bool"),
        "Privileged": ("privileged", "bool"),
        "ExternalConnection": ("external_connection", "bool"),
        "ReturnValuesReuse": ("return_values_reuse", "text"),
    },
    "EventSubscription": {
        "Event": ("event", "text"),
        "Handler": ("handler", "text"),
        "Source": ("source", "types"),
    },
    "ScheduledJob": {
        "MethodName": ("method", "text"),
        "Use": ("use", "bool"),
        "Predefined": ("is_predefined", "bool"),
        "Key": ("key", "text"),
    },
}

_EXCHANGE_PLAN_PROPERTIES = frozenset(
    {
        "AuxiliaryChoiceForm",
        "AuxiliaryListForm",
        "AuxiliaryObjectForm",
        "BasedOn",
        "Characteristics",
        "ChoiceDataGetModeOnInputByString",
        "ChoiceHistoryOnInput",
        "ChoiceMode",
        "CodeAllowedLength",
        "CodeLength",
        "CreateOnInput",
        "DataHistory",
        "DataLockControlMode",
        "DataLockFields",
        "DefaultChoiceForm",
        "DefaultListForm",
        "DefaultObjectForm",
        "DefaultPresentation",
        "DescriptionLength",
        "EditType",
        "ExecuteAfterWriteDataHistoryVersionProcessing",
        "Explanation",
        "ExtendedListPresentation",
        "ExtendedObjectPresentation",
        "FullTextSearch",
        "FullTextSearchOnInputByString",
        "IncludeConfigurationExtensions",
        "IncludeHelpInContents",
        "InputByString",
        "ListPresentation",
        "ObjectPresentation",
        "QuickChoice",
        "SearchStringModeOnInputByString",
        "StandardAttributes",
        "UpdateDataHistoryImmediatelyAfterWrite",
        "UseStandardCommands",
    }
)
_EVENT_SUBSCRIPTION_PROPERTIES = frozenset(
    {
        "Name",
        "Synonym",
        "Comment",
        "Source",
        "Event",
        "Handler",
        "ExtendedConfigurationObject",
        "ObjectBelonging",
    }
)
_SCHEDULED_JOB_PROPERTIES = frozenset(
    {
        "Name",
        "Synonym",
        "Comment",
        "MethodName",
        "Use",
        "Predefined",
        "Key",
        "Description",
        "RestartCountOnFailure",
        "RestartIntervalOnFailure",
    }
)
_COMMON_FORM_PROPERTIES = frozenset(
    {
        "Name",
        "Synonym",
        "Comment",
        "Explanation",
        "ExtendedPresentation",
        "FormType",
        "IncludeHelpInContents",
        "UsePurposes",
        "UseStandardCommands",
        "ExtendedConfigurationObject",
        "ObjectBelonging",
    }
)


def _property_value(element: ET.Element, kind: str, where: str) -> object:
    if kind == "bool":
        return _bool(element, where)
    if kind == "int":
        return _int(element, where)
    if kind == "ref":
        return _reference(_text(element))
    if kind == "short_ref":
        return _text(element).rsplit(".", 1)[-1]
    if kind == "refs":
        return [_reference(value) for value in _leaf_texts(element)]
    if kind == "types":
        return list(_type_description(element).types)
    if kind == "hierarchy":
        return _HIERARCHY_TYPES.get(_text(element), _text(element))
    if kind == "value_type":
        return _VALUE_TYPES.get(_text(element), _text(element))
    return _text(element)


def _descriptor(
    root: ET.Element,
    expected_kind: str,
    where: str,
) -> tuple[ET.Element, ET.Element, ET.Element | None]:
    if _namespace(root) != _NS_MDCLASSES or _tag(root) != "MetaDataObject":
        raise ConversionError(f"{where}: неверный корень XML MDClasses")
    objects = [item for item in root if _tag(item) == expected_kind]
    if len(objects) != 1:
        raise ConversionError(f"{where}: ожидается один {expected_kind}")
    properties = _child(objects[0], "Properties")
    if properties is None:
        raise ConversionError(f"{where}: нет Properties")
    return objects[0], properties, _child(objects[0], "ChildObjects")


def _base_object(
    root: ET.Element,
    spec: MetadataKindSpec,
    diagnostics: _Diagnostics,
    where: str,
) -> MetadataObject:
    expected_kind = _SCHEMA_KINDS[spec.source_name]
    _node, properties, children = _descriptor(root, expected_kind, where)
    name = _required_text(_child(properties, "Name"), f"{where}.Name")
    obj = MetadataObject(
        full_name=f"{spec.canonical_kind}.{name}",
        kind=spec.canonical_kind,
        name=name,
        synonym=_localized(_child(properties, "Synonym")),
        comment=_text(_child(properties, "Comment")),
    )
    property_specs = _BASE_PROPERTIES.get(expected_kind, {})
    known = _BASE_HEAD | property_specs.keys()
    if spec.extended_adapter == "exchange_plan":
        # Эти поля принадлежат typed extended payload и потому не являются
        # неизвестными только из-за узкой schema-v1-проекции base.
        known = known | _EXCHANGE_PLAN_PROPERTIES
    elif spec.extended_adapter == "event_subscription":
        known = known | _EVENT_SUBSCRIPTION_PROPERTIES
    elif spec.extended_adapter == "scheduled_job":
        known = known | _SCHEDULED_JOB_PROPERTIES
    if expected_kind in {"Constant", "DefinedType", "ChartOfCharacteristicTypes"}:
        known = known | {"Type"}
        value_type = _type_description(_child(properties, "Type"))
        if value_type.types:
            obj.value_type = value_type.to_field("ТипЗначения")
    _unknown_properties(properties, known, diagnostics, expected_kind)
    for source_name, (target_name, value_kind) in property_specs.items():
        element = _child(properties, source_name)
        if element is None:
            continue
        value = _property_value(element, value_kind, f"{where}.{source_name}")
        if target_name in {"owners", "movements", "based_on"}:
            setattr(obj, target_name, value)
        else:
            obj.props[target_name] = value

    for child in children if children is not None else ():
        child_kind = _tag(child)
        if child_kind == "Attribute":
            obj.attributes.append(_field(child, diagnostics, f"{obj.full_name}.Attribute"))
        elif child_kind == "Dimension":
            obj.dimensions.append(_field(child, diagnostics, f"{obj.full_name}.Dimension"))
        elif child_kind == "Resource":
            obj.resources.append(_field(child, diagnostics, f"{obj.full_name}.Resource"))
        elif child_kind == "TabularSection":
            part_props = _child(child, "Properties")
            if part_props is None:
                raise ConversionError(f"{where}: у TabularSection нет Properties")
            part_name = _required_text(
                _child(part_props, "Name"), f"{where}.TabularSection.Name"
            )
            part = TabularPart(
                name=part_name,
                synonym=_localized(_child(part_props, "Synonym")),
            )
            part_children = _child(child, "ChildObjects")
            for item in part_children if part_children is not None else ():
                if _tag(item) == "Attribute":
                    part.attributes.append(
                        _field(item, diagnostics, f"{obj.full_name}.{part_name}")
                    )
                else:
                    diagnostics.add(
                        "unknown_child",
                        "TabularSection",
                        _tag(item),
                    )
            obj.tabular_parts.append(part)
        elif child_kind == "EnumValue":
            value_props = _child(child, "Properties")
            if value_props is None:
                raise ConversionError(f"{where}: у EnumValue нет Properties")
            obj.enum_values.append(
                (
                    _required_text(_child(value_props, "Name"), f"{where}.EnumValue.Name"),
                    _localized(_child(value_props, "Synonym")),
                )
            )
        elif child_kind in {"AccountingFlag", "ExtDimensionAccountingFlag"}:
            flag_props = _child(child, "Properties")
            flag_name = _required_text(
                _child(flag_props, "Name"), f"{where}.{child_kind}.Name"
            )
            key = (
                "accounting_flags"
                if child_kind == "AccountingFlag"
                else "ext_dimension_accounting_flags"
            )
            obj.props.setdefault(key, []).append(flag_name)
        elif child_kind not in {"Command", "Form", "Template", "AddressingAttribute"}:
            diagnostics.add("unknown_child", expected_kind, child_kind)
    return obj


_COMMON_ATTRIBUTE_PROPERTIES = frozenset(
    {
        "Name",
        "Synonym",
        "Comment",
        "Type",
        "PasswordMode",
        "Format",
        "EditFormat",
        "ToolTip",
        "MarkNegatives",
        "Mask",
        "MultiLine",
        "ExtendedEdit",
        "MinValue",
        "MaxValue",
        "FillFromFillingValue",
        "FillValue",
        "FillChecking",
        "ChoiceFoldersAndItems",
        "ChoiceForm",
        "ChoiceHistoryOnInput",
        "ChoiceParameterLinks",
        "ChoiceParameters",
        "QuickChoice",
        "CreateOnInput",
        "DataHistory",
        "Indexing",
        "FullTextSearch",
        "DataSeparation",
        "DataSeparationValue",
        "DataSeparationUse",
        "ConditionalSeparation",
        "SeparatedDataUse",
        "AutoUse",
        "AuthenticationSeparation",
        "UsersSeparation",
        "ConfigurationExtensionsSeparation",
        "LinkByType",
        "Content",
    }
)


def _common_attribute(
    root: ET.Element,
    diagnostics: _Diagnostics,
    where: str,
) -> ExtendedObject:
    _node, properties, _children = _descriptor(root, "CommonAttribute", where)
    name = _required_text(_child(properties, "Name"), f"{where}.Name")
    _unknown_properties(
        properties,
        _COMMON_ATTRIBUTE_PROPERTIES,
        diagnostics,
        "CommonAttribute",
    )
    text = lambda key: _text(_child(properties, key))
    payload = CommonAttributePayload(
        value_type=_type_description(_child(properties, "Type")),
        password_mode=_bool(_child(properties, "PasswordMode"), f"{where}.PasswordMode"),
        format=_localized(_child(properties, "Format")),
        edit_format=_localized(_child(properties, "EditFormat")),
        tooltip=_localized(_child(properties, "ToolTip")),
        mark_negatives=_bool(_child(properties, "MarkNegatives"), f"{where}.MarkNegatives"),
        mask=text("Mask"),
        multi_line=_bool(_child(properties, "MultiLine"), f"{where}.MultiLine"),
        extended_edit=_bool(_child(properties, "ExtendedEdit"), f"{where}.ExtendedEdit"),
        min_value=text("MinValue"),
        max_value=text("MaxValue"),
        fill_from_filling_value=_bool(
            _child(properties, "FillFromFillingValue"),
            f"{where}.FillFromFillingValue",
        ),
        fill_value=text("FillValue"),
        fill_checking=text("FillChecking"),
        choice_folders_and_items=text("ChoiceFoldersAndItems"),
        choice_form=text("ChoiceForm"),
        quick_choice=text("QuickChoice"),
        create_on_input=text("CreateOnInput"),
        data_history=text("DataHistory"),
        indexing=text("Indexing"),
        full_text_search=text("FullTextSearch"),
        data_separation=text("DataSeparation"),
        data_separation_value=text("DataSeparationValue"),
        data_separation_use=_reference(text("DataSeparationUse")),
        conditional_separation=_reference(text("ConditionalSeparation")),
        separated_data_use=text("SeparatedDataUse"),
        auto_use=text("AutoUse"),
        authentication_separation=text("AuthenticationSeparation"),
        users_separation=text("UsersSeparation"),
        configuration_extensions_separation=text(
            "ConfigurationExtensionsSeparation"
        ),
        link_by_type=text("LinkByType"),
    )
    relations: list[MetadataRelation] = []
    content = _child(properties, "Content")
    for item in content if content is not None else ():
        metadata = _reference(
            _required_text(
                _child(item, "Metadata"), f"{where}.Content.Metadata"
            )
        )
        relation_properties = tuple(
            sorted(
                (
                    (
                        "conditional_separation",
                        _reference(_text(_child(item, "ConditionalSeparation"))),
                    ),
                    ("use", _text(_child(item, "Use"))),
                )
            )
        )
        relations.append(
            MetadataRelation(
                "applies_to",
                metadata,
                RelationState.UNRESOLVED,
                relation_properties,
            )
        )
    if payload.conditional_separation:
        relations.append(
            MetadataRelation(
                "conditional_separation",
                payload.conditional_separation,
                RelationState.UNRESOLVED,
            )
        )
    if payload.data_separation_use:
        relations.append(
            MetadataRelation(
                "data_separation_use",
                payload.data_separation_use,
                RelationState.UNRESOLVED,
            )
        )
    return ExtendedObject(
        full_name=f"ОбщийРеквизит.{name}",
        kind="ОбщийРеквизит",
        name=name,
        synonym=_localized(_child(properties, "Synonym")),
        comment=_text(_child(properties, "Comment")),
        code_address=f"ОбщиеРеквизиты.{name}",
        payload=payload,
        relations=tuple(relations),
    )


def _session_parameter(root: ET.Element, where: str) -> ExtendedObject:
    _node, properties, _children = _descriptor(root, "SessionParameter", where)
    name = _required_text(_child(properties, "Name"), f"{where}.Name")
    return ExtendedObject(
        full_name=f"ПараметрСеанса.{name}",
        kind="ПараметрСеанса",
        name=name,
        synonym=_localized(_child(properties, "Synonym")),
        comment=_text(_child(properties, "Comment")),
        code_address=f"ПараметрыСеанса.{name}",
        payload=SessionParameterPayload(_type_description(_child(properties, "Type"))),
    )


_JOURNAL_STANDARD_PROPERTIES = frozenset(
    {
        "Synonym",
        "Comment",
        "PasswordMode",
        "Format",
        "EditFormat",
        "ToolTip",
        "MarkNegatives",
        "Mask",
        "MultiLine",
        "ExtendedEdit",
        "MinValue",
        "MaxValue",
        "FillFromFillingValue",
        "FillValue",
        "FillChecking",
        "ChoiceForm",
        "ChoiceHistoryOnInput",
        "ChoiceParameterLinks",
        "ChoiceParameters",
        "QuickChoice",
        "CreateOnInput",
        "DataHistory",
        "FullTextSearch",
        "LinkByType",
        "TypeReductionMode",
    }
)


def _journal_standard_attribute(
    element: ET.Element,
    diagnostics: _Diagnostics,
    where: str,
) -> JournalStandardAttribute:
    name = element.get("name", "").strip()
    if not name:
        raise ConversionError(f"{where}: у StandardAttribute нет name")
    _unknown_properties(
        element,
        _JOURNAL_STANDARD_PROPERTIES,
        diagnostics,
        "DocumentJournal.StandardAttribute",
    )
    for container_name in ("ChoiceParameterLinks", "ChoiceParameters"):
        container = _child(element, container_name)
        if container is not None and len(container):
            diagnostics.add(
                "unknown_property_content",
                f"DocumentJournal.StandardAttribute.{container_name}",
                name,
            )
    text = lambda key: _text(_child(element, key))
    return JournalStandardAttribute(
        name=name,
        synonym=_localized(_child(element, "Synonym")),
        comment=text("Comment"),
        password_mode=_bool(
            _child(element, "PasswordMode"), f"{where}.PasswordMode"
        ),
        format=_localized(_child(element, "Format")),
        edit_format=_localized(_child(element, "EditFormat")),
        tooltip=_localized(_child(element, "ToolTip")),
        mark_negatives=_bool(
            _child(element, "MarkNegatives"), f"{where}.MarkNegatives"
        ),
        mask=text("Mask"),
        multi_line=_bool(_child(element, "MultiLine"), f"{where}.MultiLine"),
        extended_edit=_bool(
            _child(element, "ExtendedEdit"), f"{where}.ExtendedEdit"
        ),
        min_value=text("MinValue"),
        max_value=text("MaxValue"),
        fill_from_filling_value=_bool(
            _child(element, "FillFromFillingValue"),
            f"{where}.FillFromFillingValue",
        ),
        fill_value=text("FillValue"),
        fill_checking=text("FillChecking"),
        choice_form=_content_address(text("ChoiceForm")),
        choice_history_on_input=text("ChoiceHistoryOnInput"),
        quick_choice=text("QuickChoice"),
        create_on_input=text("CreateOnInput"),
        data_history=text("DataHistory"),
        full_text_search=text("FullTextSearch"),
        link_by_type=text("LinkByType"),
        type_reduction_mode=text("TypeReductionMode"),
    )


def _journal_field_reference(value: str, where: str) -> JournalColumnReference:
    parts = value.split(".")
    if len(parts) != 4 or parts[0] != "Document" or parts[2] != "Attribute":
        raise ConversionError(
            f"{where}: field ref должен иметь вид "
            "Document.<name>.Attribute.<name>"
        )
    if not parts[1] or not parts[3]:
        raise ConversionError(f"{where}: field ref содержит пустое имя")
    return JournalColumnReference(
        raw=value,
        target=f"Документ.{parts[1]}.{parts[3]}",
    )


def _journal_column(
    element: ET.Element,
    diagnostics: _Diagnostics,
    where: str,
) -> JournalColumn:
    properties = _child(element, "Properties")
    if properties is None:
        raise ConversionError(f"{where}: у Column нет Properties")
    _unknown_properties(
        properties,
        {"Name", "Synonym", "Comment", "Indexing", "References"},
        diagnostics,
        "DocumentJournal.Column",
    )
    name = _required_text(_child(properties, "Name"), f"{where}.Name")
    references_element = _child(properties, "References")
    references = tuple(
        _journal_field_reference(value, f"{where}.{name}")
        for value in _leaf_texts(references_element)
    )
    indexing_raw = _text(_child(properties, "Indexing"))
    return JournalColumn(
        name=name,
        synonym=_localized(_child(properties, "Synonym")),
        comment=_text(_child(properties, "Comment")),
        indexing=_INDEXING.get(indexing_raw, indexing_raw),
        references=references,
    )


_JOURNAL_COMMAND_PROPERTIES = frozenset(
    {
        "Name",
        "Synonym",
        "Comment",
        "CommandParameterType",
        "Group",
        "ModifiesData",
        "OnMainServerUnavalableBehavior",
        "ParameterUseMode",
        "Picture",
        "Representation",
        "Shortcut",
        "ToolTip",
    }
)


def _journal_command(
    element: ET.Element,
    diagnostics: _Diagnostics,
    where: str,
) -> JournalCommand:
    properties = _child(element, "Properties")
    if properties is None:
        raise ConversionError(f"{where}: у Command нет Properties")
    _unknown_properties(
        properties,
        _JOURNAL_COMMAND_PROPERTIES,
        diagnostics,
        "DocumentJournal.Command",
    )
    name = _required_text(_child(properties, "Name"), f"{where}.Name")
    picture = _child(properties, "Picture")
    picture_values = _leaf_texts(picture)
    if not picture_values and _text(picture):
        picture_values = (_text(picture),)
    return JournalCommand(
        name=name,
        synonym=_localized(_child(properties, "Synonym")),
        comment=_text(_child(properties, "Comment")),
        parameter_type=_type_description(
            _child(properties, "CommandParameterType")
        ),
        group=_content_address(_text(_child(properties, "Group"))),
        modifies_data=_bool(
            _child(properties, "ModifiesData"), f"{where}.ModifiesData"
        ),
        parameter_use_mode=_text(_child(properties, "ParameterUseMode")),
        representation=_text(_child(properties, "Representation")),
        shortcut=_text(_child(properties, "Shortcut")),
        tooltip=_localized(_child(properties, "ToolTip")),
        picture=picture_values,
        server_unavailable_behavior=_text(
            _child(properties, "OnMainServerUnavalableBehavior")
        ),
    )


_JOURNAL_PROPERTIES = frozenset(
    {
        "Name",
        "Synonym",
        "Comment",
        "ListPresentation",
        "ExtendedListPresentation",
        "Explanation",
        "DefaultForm",
        "AuxiliaryForm",
        "UseStandardCommands",
        "IncludeHelpInContents",
        "RegisteredDocuments",
        "StandardAttributes",
    }
)


def _document_journal(
    root: ET.Element,
    diagnostics: _Diagnostics,
    where: str,
) -> ExtendedObject:
    _node, properties, children = _descriptor(root, "DocumentJournal", where)
    name = _required_text(_child(properties, "Name"), f"{where}.Name")
    full_name = f"ЖурналДокументов.{name}"
    _unknown_properties(
        properties,
        _JOURNAL_PROPERTIES,
        diagnostics,
        "DocumentJournal",
    )
    registered_documents = tuple(
        _reference(value)
        for value in _leaf_texts(_child(properties, "RegisteredDocuments"))
    )
    standard_attributes_element = _child(properties, "StandardAttributes")
    standard_attributes = tuple(
        _journal_standard_attribute(
            item,
            diagnostics,
            f"{where}.StandardAttribute",
        )
        for item in (
            standard_attributes_element
            if standard_attributes_element is not None
            else ()
        )
        if _tag(item) == "StandardAttribute"
    )
    columns: list[JournalColumn] = []
    commands: list[JournalCommand] = []
    forms: list[str] = []
    templates: list[str] = []
    for item in children if children is not None else ():
        kind = _tag(item)
        if kind == "Column":
            columns.append(
                _journal_column(item, diagnostics, f"{where}.Column")
            )
        elif kind == "Command":
            commands.append(
                _journal_command(item, diagnostics, f"{where}.Command")
            )
        elif kind == "Form":
            forms.append(_required_text(item, f"{where}.Form"))
        elif kind == "Template":
            templates.append(_required_text(item, f"{where}.Template"))
        else:
            diagnostics.add("unknown_child", "DocumentJournal", kind)
    payload = DocumentJournalPayload(
        list_presentation=_localized(_child(properties, "ListPresentation")),
        extended_list_presentation=_localized(
            _child(properties, "ExtendedListPresentation")
        ),
        explanation=_localized(_child(properties, "Explanation")),
        default_form=_content_address(_text(_child(properties, "DefaultForm"))),
        auxiliary_form=_content_address(
            _text(_child(properties, "AuxiliaryForm"))
        ),
        use_standard_commands=_bool(
            _child(properties, "UseStandardCommands"),
            f"{where}.UseStandardCommands",
        ),
        include_help_in_contents=_bool(
            _child(properties, "IncludeHelpInContents"),
            f"{where}.IncludeHelpInContents",
        ),
        registered_documents=registered_documents,
        standard_attributes=standard_attributes,
        columns=tuple(columns),
        commands=tuple(commands),
        forms=tuple(forms),
        templates=tuple(templates),
    )
    relations = [
        MetadataRelation(
            "registers_document",
            target,
            RelationState.UNRESOLVED,
        )
        for target in registered_documents
    ]
    for column in columns:
        relations.extend(
            MetadataRelation(
                "column_field",
                reference.target,
                RelationState.UNRESOLVED,
                (("column", column.name),),
            )
            for reference in column.references
        )
    return ExtendedObject(
        full_name=full_name,
        kind="ЖурналДокументов",
        name=name,
        synonym=_localized(_child(properties, "Synonym")),
        comment=_text(_child(properties, "Comment")),
        code_address=full_name,
        payload=payload,
        relations=tuple(relations),
    )


def resolve_journal_column_types(
    base: Configuration,
    column: JournalColumn,
) -> tuple[str, ...]:
    """Получить типы графы через base fields, не копируя их в журнал."""
    if not isinstance(base, Configuration) or not isinstance(column, JournalColumn):
        raise ConversionError("нужны Configuration и JournalColumn")
    fields_by_address = {
        f"{obj.full_name}.{path}".casefold(): field
        for obj in base.objects.values()
        for path, field in obj.all_fields()
    }
    result: list[str] = []
    for reference in column.references:
        field = fields_by_address.get(reference.target.casefold())
        if field is None:
            continue
        for type_name in field.types:
            if type_name not in result:
                result.append(type_name)
    return tuple(result)


def _metadata_fields(element: ET.Element | None) -> tuple[str, ...]:
    if element is None:
        return ()
    return tuple(_text(item) for item in element if _text(item))


def _exchange_plan_content(
    root: ET.Element,
    diagnostics: _Diagnostics,
    where: str,
) -> tuple[ExchangePlanContentItem, ...]:
    if (
        _namespace(root) != _NS_EXTERNAL_PROPERTIES
        or _tag(root) != "ExchangePlanContent"
    ):
        raise ConversionError(f"{where}: неверный корень ExchangePlanContent")
    content: list[ExchangePlanContentItem] = []
    seen: set[str] = set()
    for element in root:
        if _tag(element) != "Item":
            diagnostics.add(
                "unknown_child",
                "ExchangePlanContent",
                _tag(element),
            )
            continue
        _unknown_properties(
            element,
            {"Metadata", "AutoRecord"},
            diagnostics,
            "ExchangePlanContent.Item",
        )
        raw = _required_text(
            _child(element, "Metadata"), f"{where}.Item.Metadata"
        )
        auto_record_raw = _required_text(
            _child(element, "AutoRecord"), f"{where}.Item.AutoRecord"
        )
        try:
            auto_record = AutoRecordPolicy(auto_record_raw)
        except ValueError as error:
            raise ConversionError(
                f"{where}.Item.AutoRecord: ожидается Allow или Deny"
            ) from error
        key = raw.casefold()
        if key in seen:
            raise ConversionError(f"{where}: Metadata дублируется в Content")
        seen.add(key)
        content.append(
            ExchangePlanContentItem(
                raw=raw,
                target=_reference(raw),
                auto_record=auto_record,
            )
        )
    return tuple(sorted(content, key=lambda item: _order(item.raw)))


def _exchange_plan(
    root: ET.Element,
    content_root: ET.Element,
    diagnostics: _Diagnostics,
    where: str,
    content_where: str,
) -> ExtendedObject:
    _node, properties, children = _descriptor(root, "ExchangePlan", where)
    name = _required_text(_child(properties, "Name"), f"{where}.Name")
    full_name = f"ПланОбмена.{name}"
    _unknown_properties(
        properties,
        _BASE_HEAD | {"DistributedInfoBase"} | _EXCHANGE_PLAN_PROPERTIES,
        diagnostics,
        "ExchangePlan",
    )
    commands: list[JournalCommand] = []
    forms: list[str] = []
    templates: list[str] = []
    for item in children if children is not None else ():
        kind = _tag(item)
        if kind == "Command":
            commands.append(
                _journal_command(item, diagnostics, f"{where}.Command")
            )
        elif kind == "Form":
            forms.append(_required_text(item, f"{where}.Form"))
        elif kind == "Template":
            templates.append(_required_text(item, f"{where}.Template"))
        elif kind not in {"Attribute", "TabularSection"}:
            diagnostics.add("unknown_child", "ExchangePlan", kind)

    based_on = tuple(
        _reference(value) for value in _leaf_texts(_child(properties, "BasedOn"))
    )
    content = _exchange_plan_content(content_root, diagnostics, content_where)
    text = lambda key: _text(_child(properties, key))
    form = lambda key: _content_address(text(key))
    standard_attributes_element = _child(properties, "StandardAttributes")
    payload = ExchangePlanPayload(
        based_on=based_on,
        characteristics=text("Characteristics"),
        choice_data_get_mode_on_input_by_string=text(
            "ChoiceDataGetModeOnInputByString"
        ),
        choice_history_on_input=text("ChoiceHistoryOnInput"),
        choice_mode=text("ChoiceMode"),
        code_allowed_length=text("CodeAllowedLength"),
        code_length=_int(_child(properties, "CodeLength"), f"{where}.CodeLength"),
        create_on_input=text("CreateOnInput"),
        data_history=text("DataHistory"),
        data_lock_control_mode=text("DataLockControlMode"),
        data_lock_fields=_metadata_fields(_child(properties, "DataLockFields")),
        default_presentation=text("DefaultPresentation"),
        description_length=_int(
            _child(properties, "DescriptionLength"),
            f"{where}.DescriptionLength",
        ),
        edit_type=text("EditType"),
        execute_after_write_data_history_version_processing=_bool(
            _child(properties, "ExecuteAfterWriteDataHistoryVersionProcessing"),
            f"{where}.ExecuteAfterWriteDataHistoryVersionProcessing",
        ),
        explanation=_localized(_child(properties, "Explanation")),
        extended_list_presentation=_localized(
            _child(properties, "ExtendedListPresentation")
        ),
        extended_object_presentation=_localized(
            _child(properties, "ExtendedObjectPresentation")
        ),
        full_text_search=text("FullTextSearch"),
        full_text_search_on_input_by_string=text("FullTextSearchOnInputByString"),
        include_configuration_extensions=_bool(
            _child(properties, "IncludeConfigurationExtensions"),
            f"{where}.IncludeConfigurationExtensions",
        ),
        include_help_in_contents=_bool(
            _child(properties, "IncludeHelpInContents"),
            f"{where}.IncludeHelpInContents",
        ),
        input_by_string=_metadata_fields(_child(properties, "InputByString")),
        list_presentation=_localized(_child(properties, "ListPresentation")),
        object_presentation=_localized(_child(properties, "ObjectPresentation")),
        quick_choice=_bool(
            _child(properties, "QuickChoice"), f"{where}.QuickChoice"
        ),
        search_string_mode_on_input_by_string=text(
            "SearchStringModeOnInputByString"
        ),
        update_data_history_immediately_after_write=_bool(
            _child(properties, "UpdateDataHistoryImmediatelyAfterWrite"),
            f"{where}.UpdateDataHistoryImmediatelyAfterWrite",
        ),
        use_standard_commands=_bool(
            _child(properties, "UseStandardCommands"),
            f"{where}.UseStandardCommands",
        ),
        default_choice_form=form("DefaultChoiceForm"),
        auxiliary_choice_form=form("AuxiliaryChoiceForm"),
        default_list_form=form("DefaultListForm"),
        auxiliary_list_form=form("AuxiliaryListForm"),
        default_object_form=form("DefaultObjectForm"),
        auxiliary_object_form=form("AuxiliaryObjectForm"),
        standard_attributes=tuple(
            _journal_standard_attribute(
                item,
                diagnostics,
                f"{where}.StandardAttribute",
            )
            for item in (
                standard_attributes_element
                if standard_attributes_element is not None
                else ()
            )
            if _tag(item) == "StandardAttribute"
        ),
        commands=tuple(commands),
        forms=tuple(forms),
        templates=tuple(templates),
        content=content,
    )
    relations = [
        MetadataRelation("based_on", target, RelationState.UNRESOLVED)
        for target in based_on
    ]
    relations.extend(
        MetadataRelation(
            "exchange_content",
            item.target,
            RelationState.UNRESOLVED,
            (("auto_record", item.auto_record.value),),
        )
        for item in content
    )
    return ExtendedObject(
        full_name=full_name,
        kind="ПланОбмена",
        name=name,
        synonym=_localized(_child(properties, "Synonym")),
        comment=text("Comment"),
        code_address=full_name,
        base_object=True,
        payload=payload,
        relations=tuple(relations),
    )


class _DigestReader:
    def __init__(self, source):
        self.source = source
        self.digest = hashlib.sha256()
        self.size = 0

    def read(self, size: int = -1) -> bytes:
        payload = self.source.read(size)
        self.digest.update(payload)
        self.size += len(payload)
        return payload


def _parse_xml(
    collection: CollectionResult,
    artifact: CollectionArtifact,
    *,
    invalid_xml: type[Exception] = ConversionError,
) -> ET.Element:
    try:
        with open_collection_member(collection.root, artifact.relative_path) as source:
            reader = _DigestReader(source)
            root = ET.parse(reader).getroot()
    except ET.ParseError as error:
        raise invalid_xml(
            f"{artifact.source_path}: XML не разбирается"
        ) from error
    except ConversionError:
        raise
    except CollectionError as error:
        raise ConversionError(
            f"{artifact.source_path}: payload не является обычным файлом"
        ) from error
    except OSError as error:
        raise ConversionError(f"{artifact.source_path}: XML недоступен") from error
    if reader.size != artifact.size or reader.digest.hexdigest() != artifact.sha256:
        raise ConversionError(
            f"{artifact.source_path}: XML изменился после collection"
        )
    return root


def _read_artifact_bytes(
    collection: CollectionResult,
    artifact: CollectionArtifact,
) -> bytes:
    try:
        with open_collection_member(collection.root, artifact.relative_path) as source:
            payload = source.read(artifact.size + 1)
    except CollectionError as error:
        raise ConversionError(
            f"{artifact.source_path}: payload артефакта небезопасен"
        ) from error
    except OSError as error:
        raise ConversionError(
            f"{artifact.source_path}: payload артефакта недоступен"
        ) from error
    if (
        len(payload) != artifact.size
        or hashlib.sha256(payload).hexdigest() != artifact.sha256
    ):
        raise ConversionError(
            f"{artifact.source_path}: payload изменился после collection"
        )
    return payload


@dataclass(frozen=True, slots=True)
class _ModuleSymbols:
    address: str
    procedures: Mapping[str, str]
    ambiguous: frozenset[str] = frozenset()
    readable: bool = True
    content_sha256: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "procedures",
            MappingProxyType(dict(self.procedures)),
        )


def _module_symbols_from_payload(
    address: str,
    payload: bytes,
) -> _ModuleSymbols:
    try:
        text = нормализовать(payload.decode("utf-8-sig"))
    except UnicodeDecodeError:
        return _ModuleSymbols(address, {}, readable=False)
    procedures: dict[str, str] = {}
    ambiguous: set[str] = set()
    for procedure in разобрать(text):
        procedure_key = procedure.имя.casefold()
        if procedure_key in procedures:
            ambiguous.add(procedure_key)
            continue
        procedures[procedure_key] = procedure.имя
    return _ModuleSymbols(
        address,
        procedures,
        frozenset(ambiguous),
        content_sha256=hashlib.sha256(text.encode("utf-8")).hexdigest(),
    )


def _binding_module_symbols(
    collection: CollectionResult,
    diagnostics: _Diagnostics,
) -> Mapping[str, _ModuleSymbols]:
    grouped: dict[str, list[CollectionArtifact]] = {}
    for artifact in collection.code:
        if artifact.source_name not in {"CommonModules", "CommonForms"}:
            continue
        grouped.setdefault(artifact.address.casefold(), []).append(artifact)

    result: dict[str, _ModuleSymbols] = {}
    for key, artifacts in sorted(grouped.items()):
        canonical = sorted((item.address for item in artifacts), key=_order)[0]
        if len({item.sha256 for item in artifacts}) != 1:
            source_kind = (
                "CommonForm"
                if artifacts[0].source_name == "CommonForms"
                else "CommonModule"
            )
            diagnostics.add(
                "ambiguous_code_module",
                source_kind,
                canonical,
                severity="warning",
            )
            result[key] = _ModuleSymbols(canonical, {}, readable=False)
            continue
        artifact = artifacts[0]
        if not artifact.source_path.endswith((".bsl", ".txt")):
            result[key] = _ModuleSymbols(canonical, {}, readable=False)
            continue
        result[key] = _module_symbols_from_payload(
            canonical,
            _read_artifact_bytes(collection, artifact),
        )
    return MappingProxyType(result)


def _resolve_binding(
    raw: str,
    modules: Mapping[str, _ModuleSymbols],
    diagnostics: _Diagnostics,
    owner: str,
) -> CodeBinding:
    parts = raw.split(".") if raw else []
    if (
        len(parts) != 3
        or parts[0] != "CommonModule"
        or not parts[1]
        or not parts[2]
    ):
        binding = CodeBinding(raw, BindingState.UNRESOLVED)
    else:
        requested_module = f"ОбщийМодуль.{parts[1]}"
        module = modules.get(requested_module.casefold())
        if module is None:
            binding = CodeBinding(
                raw,
                BindingState.MODULE_MISSING,
                module_address=requested_module,
            )
        elif not module.readable or parts[2].casefold() in module.ambiguous:
            binding = CodeBinding(
                raw,
                BindingState.UNRESOLVED,
                module_address=module.address,
            )
        else:
            procedure = module.procedures.get(parts[2].casefold())
            if procedure is None:
                binding = CodeBinding(
                    raw,
                    BindingState.PROCEDURE_MISSING,
                    module_address=module.address,
                )
            else:
                binding = CodeBinding(
                    raw,
                    BindingState.RESOLVED,
                    module_address=module.address,
                    procedure_address=f"{module.address}::{procedure}",
                )
    if binding.state is not BindingState.RESOLVED:
        diagnostics.add(
            "unresolved_code_binding",
            binding.state.value,
            owner,
            severity="warning",
        )
    return binding


def _event_subscription(
    root: ET.Element,
    modules: Mapping[str, _ModuleSymbols],
    diagnostics: _Diagnostics,
    where: str,
) -> ExtendedObject:
    _node, properties, _children = _descriptor(
        root, "EventSubscription", where
    )
    _unknown_properties(
        properties,
        _EVENT_SUBSCRIPTION_PROPERTIES,
        diagnostics,
        "EventSubscription",
    )
    name = _required_text(_child(properties, "Name"), f"{where}.Name")
    full_name = f"ПодпискаНаСобытие.{name}"
    sources = _type_description(_child(properties, "Source")).types
    handler = _text(_child(properties, "Handler"))
    return ExtendedObject(
        full_name=full_name,
        kind="ПодпискаНаСобытие",
        name=name,
        synonym=_localized(_child(properties, "Synonym")),
        comment=_text(_child(properties, "Comment")),
        base_object=True,
        payload=EventSubscriptionPayload(
            _resolve_binding(handler, modules, diagnostics, full_name)
        ),
        relations=tuple(
            MetadataRelation(
                "event_source",
                source,
                RelationState.UNRESOLVED,
            )
            for source in sources
        ),
    )


def _scheduled_job(
    root: ET.Element,
    modules: Mapping[str, _ModuleSymbols],
    diagnostics: _Diagnostics,
    where: str,
) -> ExtendedObject:
    _node, properties, _children = _descriptor(root, "ScheduledJob", where)
    _unknown_properties(
        properties,
        _SCHEDULED_JOB_PROPERTIES,
        diagnostics,
        "ScheduledJob",
    )
    name = _required_text(_child(properties, "Name"), f"{where}.Name")
    full_name = f"РегламентноеЗадание.{name}"
    method = _text(_child(properties, "MethodName"))
    return ExtendedObject(
        full_name=full_name,
        kind="РегламентноеЗадание",
        name=name,
        synonym=_localized(_child(properties, "Synonym")),
        comment=_text(_child(properties, "Comment")),
        base_object=True,
        payload=ScheduledJobPayload(
            description=_text(_child(properties, "Description")),
            restart_count_on_failure=_int(
                _child(properties, "RestartCountOnFailure"),
                f"{where}.RestartCountOnFailure",
            ),
            restart_interval_on_failure=_int(
                _child(properties, "RestartIntervalOnFailure"),
                f"{where}.RestartIntervalOnFailure",
            ),
            binding=_resolve_binding(method, modules, diagnostics, full_name),
        ),
    )


def _resolve_form_binding(
    raw: str,
    module_address: str,
    module: _ModuleSymbols | None,
    diagnostics: _Diagnostics,
    owner: str,
) -> CodeBinding:
    if not raw:
        binding = CodeBinding(raw, BindingState.UNRESOLVED)
    elif module is None:
        binding = CodeBinding(
            raw,
            BindingState.MODULE_MISSING,
            module_address=module_address,
        )
    elif not module.readable or raw.casefold() in module.ambiguous:
        binding = CodeBinding(
            raw,
            BindingState.UNRESOLVED,
            module_address=module.address,
        )
    else:
        procedure = module.procedures.get(raw.casefold())
        if procedure is None:
            binding = CodeBinding(
                raw,
                BindingState.PROCEDURE_MISSING,
                module_address=module.address,
            )
        else:
            binding = CodeBinding(
                raw,
                BindingState.RESOLVED,
                module_address=module.address,
                procedure_address=f"{module.address}::{procedure}",
            )
    if binding.state is not BindingState.RESOLVED:
        diagnostics.add(
            "unresolved_form_handler",
            binding.state.value,
            owner,
            severity="warning",
        )
    return binding


def _one_representation(
    artifacts: list[CollectionArtifact],
    label: str,
) -> CollectionArtifact | None:
    if not artifacts:
        return None
    if len({item.sha256 for item in artifacts}) != 1:
        raise ConversionError(f"{label}: конфликтуют представления формы")
    return sorted(artifacts, key=lambda item: item.source_path)[0]


def _common_form_object(
    collection: CollectionResult,
    address: str,
    form_artifacts: list[CollectionArtifact],
    modules: Mapping[str, _ModuleSymbols],
    diagnostics: _Diagnostics,
) -> ExtendedObject:
    descriptors: list[CollectionArtifact] = []
    form_xml: list[CollectionArtifact] = []
    containers: list[CollectionArtifact] = []
    for artifact in form_artifacts:
        parts = PurePosixPath(artifact.source_path).parts
        if len(parts) == 2 and artifact.source_path.endswith(".xml"):
            descriptors.append(artifact)
        elif parts[-2:] == ("Ext", "Form.xml"):
            form_xml.append(artifact)
        elif parts[-2:] == ("Ext", "Form.bin") or (
            len(parts) == 1 and artifact.source_path.endswith(".Form")
        ):
            containers.append(artifact)

    descriptor_artifact = _one_representation(descriptors, address)
    form_xml_artifact = _one_representation(form_xml, address)
    container_artifact = _one_representation(containers, address)
    name = address.split(".", 1)[1]
    synonym = ""
    comment = ""
    uuid = ""
    form_type = ""
    explanation = ""
    extended_presentation = ""
    include_help_in_contents: bool | None = None
    use_purposes: tuple[str, ...] = ()
    use_standard_commands: bool | None = None
    if descriptor_artifact is not None:
        root = _parse_xml(collection, descriptor_artifact)
        _node, properties, _children = _descriptor(
            root, "CommonForm", descriptor_artifact.source_path
        )
        _unknown_properties(
            properties,
            _COMMON_FORM_PROPERTIES,
            diagnostics,
            "CommonForm",
        )
        descriptor = parse_form_descriptor(root)
        descriptor_name = _required_text(
            _child(properties, "Name"),
            f"{descriptor_artifact.source_path}.Name",
        )
        if descriptor_name.casefold() != name.casefold():
            raise ConversionError("имя общей формы не совпадает с адресом")
        name = descriptor_name
        synonym = _localized(_child(properties, "Synonym"))
        comment = _text(_child(properties, "Comment"))
        uuid = descriptor.uuid or ""
        form_type = _text(_child(properties, "FormType"))
        explanation = _localized(_child(properties, "Explanation"))
        extended_presentation = _localized(
            _child(properties, "ExtendedPresentation")
        )
        include_help_in_contents = _bool(
            _child(properties, "IncludeHelpInContents"),
            f"{descriptor_artifact.source_path}.IncludeHelpInContents",
        )
        use_purposes = _leaf_texts(_child(properties, "UsePurposes"))
        use_standard_commands = _bool(
            _child(properties, "UseStandardCommands"),
            f"{descriptor_artifact.source_path}.UseStandardCommands",
        )

    attributes: tuple[str, ...] = ()
    elements: tuple[str, ...] = ()
    parsed_events = ()
    marker: int | None = None
    embedded_module: _ModuleSymbols | None = None
    container_unreadable = False
    if form_xml_artifact is not None:
        try:
            structure = parse_form_xml(
                _parse_xml(
                    collection,
                    form_xml_artifact,
                    invalid_xml=FormStructureError,
                )
            )
        except FormStructureError:
            structure_state = FormStructureState.UNREADABLE
        else:
            structure_state = FormStructureState.READY
            attributes = structure.attributes
            elements = structure.elements
            parsed_events = structure.events
    elif container_artifact is not None:
        try:
            payload = _read_artifact_bytes(collection, container_artifact)
            with V8Container(payload) as container:
                if "module" in container:
                    embedded_module = _module_symbols_from_payload(
                        address,
                        container.read("module"),
                    )
                result = read_form(container.read("form"))
        except (
            KeyError,
            FormReadError,
            V8ContainerError,
            V8ResourceLimitError,
        ):
            structure_state = FormStructureState.UNREADABLE
            container_unreadable = True
        else:
            structure_state = FormStructureState.PARTIAL
            marker = result.marker
    else:
        structure_state = FormStructureState.DESCRIPTOR_ONLY

    module = modules.get(address.casefold())
    if module is not None and embedded_module is not None:
        if (
            not module.readable
            or not embedded_module.readable
            or module.content_sha256 != embedded_module.content_sha256
        ):
            diagnostics.add(
                "ambiguous_code_module",
                "CommonForm",
                address,
                severity="warning",
            )
            module = _ModuleSymbols(address, {}, readable=False)
    elif embedded_module is not None:
        module = embedded_module
    module_state = (
        FormModuleState.UNREADABLE
        if module is None and container_unreadable
        else FormModuleState.MISSING
        if module is None
        else FormModuleState.READY
        if module.readable
        else FormModuleState.UNREADABLE
    )
    if structure_state is not FormStructureState.READY:
        diagnostics.add(
            "form_coverage",
            structure_state.value,
            address,
            severity=(
                "warning"
                if structure_state is FormStructureState.UNREADABLE
                else "info"
            ),
        )
    if module_state is not FormModuleState.READY:
        diagnostics.add(
            "form_coverage",
            module_state.value,
            address,
            severity="warning",
        )
    events = tuple(
        FormEventBinding(
            event.element,
            event.event,
            event.handler,
            _resolve_form_binding(
                event.handler,
                address,
                module,
                diagnostics,
                address,
            ),
        )
        for event in parsed_events
    )
    return ExtendedObject(
        full_name=address,
        kind="ОбщаяФорма",
        name=name,
        synonym=synonym,
        comment=comment,
        code_address=address,
        payload=CommonFormPayload(
            uuid=uuid,
            form_type=form_type,
            explanation=explanation,
            extended_presentation=extended_presentation,
            include_help_in_contents=include_help_in_contents,
            use_purposes=use_purposes,
            use_standard_commands=use_standard_commands,
            structure_state=structure_state,
            module_state=module_state,
            container_marker=marker,
            attributes=attributes,
            elements=elements,
            events=events,
        ),
    )


def _common_form_objects(
    collection: CollectionResult,
    modules: Mapping[str, _ModuleSymbols],
    diagnostics: _Diagnostics,
) -> dict[str, ExtendedObject]:
    grouped: dict[str, tuple[str, list[CollectionArtifact]]] = {}
    for artifact in (*collection.forms, *collection.code):
        if artifact.source_name != "CommonForms":
            continue
        key = artifact.address.casefold()
        current = grouped.get(key)
        if current is None:
            grouped[key] = artifact.address, []
            current = grouped[key]
        elif current[0] != artifact.address:
            raise ConversionError("адреса общих форм различаются только регистром")
        if artifact.kind is ArtifactKind.FORMS:
            current[1].append(artifact)
    return {
        address: _common_form_object(
            collection,
            address,
            artifacts,
            modules,
            diagnostics,
        )
        for _key, (address, artifacts) in sorted(grouped.items())
    }


def _is_descriptor(artifact: CollectionArtifact, spec: MetadataKindSpec) -> bool:
    parts = PurePosixPath(artifact.source_path).parts
    if len(parts) == 2:
        return parts[0] == spec.source_name and parts[1].endswith(".xml")
    if len(parts) != 1 or not artifact.source_path.endswith(".xml"):
        return False
    prefix = artifact.source_path.split(".", 1)[0]
    return prefix in spec.aliases


def _known_supplementary_metadata(
    artifact: CollectionArtifact,
    spec: MetadataKindSpec,
) -> bool:
    if spec.extended_adapter not in {
        "common_form",
        "document_journal",
        "exchange_plan",
    }:
        return False
    parts = PurePosixPath(artifact.source_path).parts
    if parts[-2:] == ("Ext", "Help.xml"):
        return True
    if spec.extended_adapter == "common_form":
        return False
    if len(parts) >= 4 and parts[2] == "Templates":
        return parts[-1].endswith(".xml")
    return (
        spec.extended_adapter == "exchange_plan"
        and len(parts) == 4
        and parts[0] == "ExchangePlans"
        and parts[2:] == ("Ext", "Content.xml")
    )


def _exchange_plan_content_artifacts(
    collection: CollectionResult,
) -> dict[str, CollectionArtifact]:
    result: dict[str, CollectionArtifact] = {}
    for artifact in collection.metadata:
        if artifact.source_name != "ExchangePlans":
            continue
        parts = PurePosixPath(artifact.source_path).parts
        if not (
            len(parts) == 4
            and parts[0] == "ExchangePlans"
            and parts[2:] == ("Ext", "Content.xml")
        ):
            continue
        key = parts[1].casefold()
        if key in result:
            raise ConversionError("дублируется Content.xml плана обмена")
        result[key] = artifact
    return result


def _configuration(
    root: ET.Element,
    collection: CollectionResult,
    diagnostics: _Diagnostics,
) -> Configuration:
    _node, properties, _children = _descriptor(root, "Configuration", "Configuration.xml")
    known = {
        "Name",
        "Synonym",
        "Comment",
        "Vendor",
        "Version",
        "CompatibilityMode",
    }
    _unknown_properties(properties, known, diagnostics, "Configuration")
    name = _required_text(_child(properties, "Name"), "Configuration.Name")
    if name != collection.probe.internal_name:
        raise ConversionError("Configuration.Name не совпадает с probe")
    return Configuration(
        name=name,
        synonym=_localized(_child(properties, "Synonym")),
        version=_text(_child(properties, "Version")),
        vendor=_text(_child(properties, "Vendor")),
        platform=_text(_child(properties, "CompatibilityMode")),
        source_format="source-b",
    )


def _resolve_relations(
    objects: dict[str, ExtendedObject],
    base: Configuration,
    diagnostics: _Diagnostics,
) -> None:
    targets = {name.casefold() for name in base.objects}
    targets.update(
        f"{obj.full_name}.{path}".casefold()
        for obj in base.objects.values()
        for path, _field_value in obj.all_fields()
    )
    targets.update(name.casefold() for name in objects)
    for name, item in tuple(objects.items()):
        relations = []
        for relation in item.relations:
            state = (
                RelationState.RESOLVED
                if relation.target.casefold() in targets
                else RelationState.UNRESOLVED
            )
            resolved = MetadataRelation(
                relation.kind,
                relation.target,
                state,
                tuple(sorted(relation.properties)),
            )
            relations.append(resolved)
            if state is RelationState.UNRESOLVED:
                diagnostics.add(
                    "unresolved_relation",
                    "metadata_relation",
                    f"{name} -> {relation.target}",
                )
        objects[name] = ExtendedObject(
            full_name=item.full_name,
            kind=item.kind,
            name=item.name,
            synonym=item.synonym,
            comment=item.comment,
            code_address=item.code_address,
            base_object=item.base_object,
            payload=item.payload,
            modules=item.modules,
            forms=item.forms,
            relations=tuple(
                sorted(
                    relations,
                    key=lambda value: (value.kind, _order(value.target)),
                )
            ),
        )


def _attach_content(
    collection: CollectionResult,
    base: Configuration,
    objects: dict[str, ExtendedObject],
    diagnostics: _Diagnostics,
) -> None:
    addresses = tuple(
        sorted(
            (*base.objects, *objects),
            key=lambda value: (-len(value), _order(value)),
        )
    )
    attached: dict[str, dict[ArtifactKind, set[str]]] = {}
    for artifact in (*collection.code, *collection.forms):
        owner = next(
            (
                address
                for address in addresses
                if artifact.address == address or artifact.address.startswith(address + ".")
            ),
            "",
        )
        if not owner:
            diagnostics.add(
                "unresolved_content_owner",
                artifact.kind.value,
                artifact.address,
            )
            continue
        attached.setdefault(owner, {}).setdefault(artifact.kind, set()).add(artifact.address)
    for owner, groups in attached.items():
        current = objects.get(owner)
        if current is None:
            base_object = base.objects[owner]
            current = ExtendedObject(
                full_name=owner,
                kind=base_object.kind,
                name=base_object.name,
                synonym=base_object.synonym,
                comment=base_object.comment,
                base_object=True,
            )
        objects[owner] = ExtendedObject(
            full_name=current.full_name,
            kind=current.kind,
            name=current.name,
            synonym=current.synonym,
            comment=current.comment,
            code_address=current.code_address,
            base_object=current.base_object,
            payload=current.payload,
            modules=tuple(sorted(groups.get(ArtifactKind.CODE, set()), key=_order)),
            forms=tuple(sorted(groups.get(ArtifactKind.FORMS, set()), key=_order)),
            relations=current.relations,
        )


def _canonical(value: object) -> object:
    if isinstance(value, Enum):
        return value.value
    if is_dataclass(value):
        return {item.name: _canonical(getattr(value, item.name)) for item in fields(value)}
    if isinstance(value, Mapping):
        return {
            key: _canonical(item)
            for key, item in sorted(value.items(), key=lambda pair: _order(str(pair[0])))
        }
    if isinstance(value, (list, tuple)):
        return [_canonical(item) for item in value]
    return value


def _field_data(value: Field) -> dict[str, object]:
    return {
        "name": value.name,
        "synonym": value.synonym,
        "comment": value.comment,
        "indexing": value.indexing,
        "types": sorted(value.types, key=_order),
        "string_length": value.string_length,
        "digits": value.digits,
        "fraction_digits": value.fraction_digits,
        "date_parts": value.date_parts,
    }


def _base_data(base: Configuration) -> dict[str, object]:
    objects = []
    for value in sorted(base.objects.values(), key=lambda item: _order(item.full_name)):
        objects.append(
            {
                "full_name": value.full_name,
                "kind": value.kind,
                "name": value.name,
                "synonym": value.synonym,
                "comment": value.comment,
                "props": _canonical(value.props),
                "attributes": sorted(
                    (_field_data(item) for item in value.attributes),
                    key=lambda item: _order(str(item["name"])),
                ),
                "dimensions": sorted(
                    (_field_data(item) for item in value.dimensions),
                    key=lambda item: _order(str(item["name"])),
                ),
                "resources": sorted(
                    (_field_data(item) for item in value.resources),
                    key=lambda item: _order(str(item["name"])),
                ),
                "tabular_parts": [
                    {
                        "name": part.name,
                        "synonym": part.synonym,
                        "attributes": sorted(
                            (_field_data(item) for item in part.attributes),
                            key=lambda item: _order(str(item["name"])),
                        ),
                    }
                    for part in sorted(value.tabular_parts, key=lambda item: _order(item.name))
                ],
                "movements": sorted(value.movements, key=_order),
                "based_on": sorted(value.based_on, key=_order),
                "owners": sorted(value.owners, key=_order),
                "predefined": sorted(value.predefined, key=_order),
                "enum_values": sorted(value.enum_values, key=lambda item: _order(item[0])),
                "value_type": _field_data(value.value_type) if value.value_type else None,
            }
        )
    return {
        "name": base.name,
        "synonym": base.synonym,
        "version": base.version,
        "vendor": base.vendor,
        "schema_version": base.schema_version,
        "objects": objects,
    }


def _sha256(value: object, prefix: bytes) -> str:
    payload = json.dumps(
        _canonical(value), ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(prefix + payload).hexdigest()


def convert_collection(
    collection: CollectionResult,
    *,
    kind_specs: Iterable[MetadataKindSpec] = DEFAULT_KIND_SPECS,
) -> StructureConversion:
    """Построить обе структуры из collection без Registry."""
    if not isinstance(collection, CollectionResult):
        raise ConversionError("collection должен быть CollectionResult")
    specs = {item.source_name: item for item in kind_specs}
    diagnostics = _Diagnostics()
    configuration_artifacts = tuple(
        item
        for item in collection.metadata
        if item.source_path == "Configuration.xml"
    )
    if len(configuration_artifacts) != 1:
        raise ConversionError(
            "collection должен содержать один Configuration.xml"
        )
    base = _configuration(
        _parse_xml(collection, configuration_artifacts[0]),
        collection,
        diagnostics,
    )
    extended_objects: dict[str, ExtendedObject] = {}
    exchange_plan_contents = _exchange_plan_content_artifacts(collection)
    needs_binding_resolver = any(
        item.source_name in {"EventSubscriptions", "ScheduledJobs"}
        for item in collection.metadata
    ) or any(item.source_name == "CommonForms" for item in collection.artifacts)
    module_symbols = (
        _binding_module_symbols(collection, diagnostics)
        if needs_binding_resolver
        else MappingProxyType({})
    )

    for artifact in collection.metadata:
        if artifact.source_path == "Configuration.xml":
            continue
        spec = specs.get(artifact.source_name)
        if spec is None:
            diagnostics.add("unknown_metadata", artifact.source_name, artifact.source_path)
            continue
        if not _is_descriptor(artifact, spec):
            if _known_supplementary_metadata(artifact, spec):
                continue
            diagnostics.add(
                "unhandled_metadata_member",
                spec.source_name,
                artifact.source_path,
            )
            continue
        root = _parse_xml(collection, artifact)
        if spec.base_adapter == "schema_v1":
            obj = _base_object(root, spec, diagnostics, artifact.source_path)
            if obj.full_name in base.objects:
                raise ConversionError(f"дублируется base object {obj.full_name}")
            base.objects[obj.full_name] = obj
        if spec.extended_adapter == "common_attribute":
            obj = _common_attribute(root, diagnostics, artifact.source_path)
            if obj.full_name in extended_objects:
                raise ConversionError(f"дублируется extended object {obj.full_name}")
            extended_objects[obj.full_name] = obj
        elif spec.extended_adapter == "session_parameter":
            properties = _descriptor(root, "SessionParameter", artifact.source_path)[1]
            _unknown_properties(
                properties,
                {"Name", "Synonym", "Comment", "Type"},
                diagnostics,
                "SessionParameter",
            )
            obj = _session_parameter(root, artifact.source_path)
            if obj.full_name in extended_objects:
                raise ConversionError(f"дублируется extended object {obj.full_name}")
            extended_objects[obj.full_name] = obj
        elif spec.extended_adapter == "document_journal":
            obj = _document_journal(root, diagnostics, artifact.source_path)
            if obj.full_name in extended_objects:
                raise ConversionError(f"дублируется extended object {obj.full_name}")
            extended_objects[obj.full_name] = obj
        elif spec.extended_adapter == "exchange_plan":
            properties = _descriptor(root, "ExchangePlan", artifact.source_path)[1]
            name = _required_text(
                _child(properties, "Name"), f"{artifact.source_path}.Name"
            )
            content_artifact = exchange_plan_contents.pop(name.casefold(), None)
            if content_artifact is None:
                raise ConversionError(
                    f"{artifact.source_path}: отсутствует обязательный Content.xml"
                )
            obj = _exchange_plan(
                root,
                _parse_xml(collection, content_artifact),
                diagnostics,
                artifact.source_path,
                content_artifact.source_path,
            )
            if obj.full_name in extended_objects:
                raise ConversionError(f"дублируется extended object {obj.full_name}")
            extended_objects[obj.full_name] = obj
        elif spec.extended_adapter == "event_subscription":
            obj = _event_subscription(
                root,
                module_symbols,
                diagnostics,
                artifact.source_path,
            )
            if obj.full_name in extended_objects:
                raise ConversionError(f"дублируется extended object {obj.full_name}")
            extended_objects[obj.full_name] = obj
        elif spec.extended_adapter == "scheduled_job":
            obj = _scheduled_job(
                root,
                module_symbols,
                diagnostics,
                artifact.source_path,
            )
            if obj.full_name in extended_objects:
                raise ConversionError(f"дублируется extended object {obj.full_name}")
            extended_objects[obj.full_name] = obj
        elif spec.extended_adapter and spec.extended_adapter not in {
            "common_attribute",
            "common_form",
            "document_journal",
            "event_subscription",
            "exchange_plan",
            "scheduled_job",
            "session_parameter",
        }:
            raise ConversionError(
                f"для {spec.source_name} не реализован adapter "
                f"{spec.extended_adapter}"
            )

    if exchange_plan_contents:
        raise ConversionError("Content.xml не соответствует descriptor плана обмена")

    for name, obj in _common_form_objects(
        collection,
        module_symbols,
        diagnostics,
    ).items():
        if name in extended_objects:
            raise ConversionError(f"дублируется extended object {name}")
        extended_objects[name] = obj

    _attach_content(collection, base, extended_objects, diagnostics)
    _resolve_relations(extended_objects, base, diagnostics)
    extended = ExtendedStructure(extended_objects)
    return StructureConversion(
        base=base,
        extended=extended,
        base_content_sha256=_sha256(_base_data(base), b"mcp1c-base-structure-v1\0"),
        extended_content_sha256=_sha256(
            extended.objects, b"mcp1c-extended-structure-v1\0"
        ),
        diagnostics=diagnostics.freeze(),
    )


__all__ = [
    "AutoRecordPolicy",
    "BindingState",
    "CodeBinding",
    "CommonAttributePayload",
    "CommonFormPayload",
    "ConversionDiagnostic",
    "ConversionError",
    "DocumentJournalPayload",
    "EventSubscriptionPayload",
    "ExchangePlanContentItem",
    "ExchangePlanPayload",
    "ExtendedObject",
    "ExtendedStructure",
    "JournalColumn",
    "JournalColumnReference",
    "JournalCommand",
    "JournalStandardAttribute",
    "FormEventBinding",
    "FormModuleState",
    "FormStructureState",
    "MetadataRelation",
    "RelationState",
    "ScheduledJobPayload",
    "SessionParameterPayload",
    "StructureConversion",
    "TypeDescription",
    "convert_collection",
    "resolve_journal_column_types",
]
