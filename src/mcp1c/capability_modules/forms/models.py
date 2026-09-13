"""Строгая спецификация поддержанного слоя управляемой формы."""

from __future__ import annotations

import re
from collections.abc import Hashable, Iterator, Mapping
from dataclasses import dataclass
from typing import Literal, NotRequired, TypeAlias, TypedDict

from .command_catalog import (
    DOCUMENT_FORM_STANDARD_COMMANDS,
    OBJECT_FORM_STANDARD_COMMANDS,
    standard_command_supported,
)
from .diagnostics import Diagnostic
from .event_catalog import OBJECT_FORM_EVENTS, event_signature
from .metadata_types import (
    dynamic_list_xml_table,
    metadata_object_xml_type,
    metadata_reference_xml_type,
)
from .version_catalog import normalized_platform_version, platform_profile


SPECIFICATION_VERSION = 1
SUPPORTED_FORMAT_VERSION = "2.16"
_IDENTIFIER = re.compile(r"[A-Za-zА-Яа-яЁё_][A-Za-zА-Яа-яЁё0-9_]*\Z")
_DATA_PATH = re.compile(
    r"[A-Za-zА-Яа-яЁё_][A-Za-zА-Яа-яЁё0-9_]*"
    r"(?:\.[A-Za-zА-Яа-яЁё_][A-Za-zА-Яа-яЁё0-9_]*)*\Z"
)
BSL_RESERVED_KEYWORDS = frozenset(
    word.casefold()
    for word in (
        "Если", "If", "Тогда", "Then", "ИначеЕсли", "ElsIf",
        "Иначе", "Else", "КонецЕсли", "EndIf", "Для", "For",
        "Каждого", "Each", "Из", "In", "По", "To", "Пока", "While",
        "Цикл", "Do", "КонецЦикла", "EndDo", "Ждать", "Await",
        "Процедура", "Procedure", "Функция", "Function",
        "КонецПроцедуры", "EndProcedure", "КонецФункции", "EndFunction",
        "Перем", "Var", "Перейти", "Goto", "Возврат", "Return",
        "Продолжить", "Continue", "Прервать", "Break", "И", "And",
        "Или", "Or", "Не", "Not", "Попытка", "Try",
        "Исключение", "Except", "ВызватьИсключение", "Raise",
        "КонецПопытки", "EndTry", "Новый", "New", "Выполнить", "Execute",
    )
)


def is_reserved_bsl_keyword(value: str) -> bool:
    """Проверить точное русское или английское ключевое слово BSL."""

    return value.casefold() in BSL_RESERVED_KEYWORDS


class LocalizedTextSpec(TypedDict):
    __pydantic_config__ = {"extra": "forbid"}

    ru: str


class StringTypeSpec(TypedDict):
    __pydantic_config__ = {"extra": "forbid"}

    kind: Literal["string"]
    length: int


class BooleanTypeSpec(TypedDict):
    __pydantic_config__ = {"extra": "forbid"}

    kind: Literal["boolean"]


class NumberTypeSpec(TypedDict):
    __pydantic_config__ = {"extra": "forbid"}

    kind: Literal["number"]
    digits: int
    fraction_digits: int
    allowed_sign: Literal["any", "nonnegative"]


class DateTypeSpec(TypedDict):
    __pydantic_config__ = {"extra": "forbid"}

    kind: Literal["date"]
    fractions: Literal["date", "date_time"]


ScalarTypeSpec: TypeAlias = (
    StringTypeSpec | BooleanTypeSpec | NumberTypeSpec | DateTypeSpec
)


class MetadataReferenceTypeSpec(TypedDict):
    __pydantic_config__ = {"extra": "forbid"}

    kind: Literal["metadata_reference"]
    object: str


ValueTypeSpec: TypeAlias = ScalarTypeSpec | MetadataReferenceTypeSpec


class CompositeTypeSpec(TypedDict):
    __pydantic_config__ = {"extra": "forbid"}

    kind: Literal["composite"]
    variants: list[ValueTypeSpec]


class ValueTableColumnSpec(TypedDict):
    __pydantic_config__ = {"extra": "forbid"}

    name: str
    type: ValueTypeSpec | CompositeTypeSpec
    title: NotRequired[LocalizedTextSpec]


class ValueTableTypeSpec(TypedDict):
    __pydantic_config__ = {"extra": "forbid"}

    kind: Literal["value_table"]
    columns: list[ValueTableColumnSpec]


class MetadataObjectTypeSpec(TypedDict):
    __pydantic_config__ = {"extra": "forbid"}

    kind: Literal["metadata_object"]
    object: str


class DynamicListTypeSpec(TypedDict):
    __pydantic_config__ = {"extra": "forbid"}

    kind: Literal["dynamic_list"]
    main_table: str
    dynamic_data_read: bool


AttributeTypeSpec: TypeAlias = (
    ValueTypeSpec
    | CompositeTypeSpec
    | ValueTableTypeSpec
    | MetadataObjectTypeSpec
    | DynamicListTypeSpec
)


class FormAttributeSpec(TypedDict):
    __pydantic_config__ = {"extra": "forbid"}

    name: str
    type: AttributeTypeSpec
    title: NotRequired[LocalizedTextSpec]
    main: NotRequired[bool]


class ChoiceListItemSpec(TypedDict):
    __pydantic_config__ = {"extra": "forbid"}

    value: str
    presentation: LocalizedTextSpec


class InputFieldSpec(TypedDict):
    __pydantic_config__ = {"extra": "forbid"}

    kind: Literal["input_field"]
    name: str
    data_path: str
    title: NotRequired[LocalizedTextSpec]
    multiline: NotRequired[bool]
    read_only: NotRequired[bool]
    list_choice_mode: NotRequired[bool]
    choice_list: NotRequired[list[ChoiceListItemSpec]]
    horizontal_stretch: NotRequired[bool]
    vertical_stretch: NotRequired[bool]


class CheckBoxFieldSpec(TypedDict):
    __pydantic_config__ = {"extra": "forbid"}

    kind: Literal["check_box_field"]
    name: str
    data_path: str
    title: NotRequired[LocalizedTextSpec]
    read_only: NotRequired[bool]


class LabelDecorationSpec(TypedDict):
    __pydantic_config__ = {"extra": "forbid"}

    kind: Literal["label_decoration"]
    name: str
    title: NotRequired[LocalizedTextSpec]
    hyperlink: NotRequired[bool]
    horizontal_stretch: NotRequired[bool]
    vertical_stretch: NotRequired[bool]


class LabelFieldSpec(TypedDict):
    __pydantic_config__ = {"extra": "forbid"}

    kind: Literal["label_field"]
    name: str
    data_path: str
    title: NotRequired[LocalizedTextSpec]
    hyperlink: NotRequired[bool]
    read_only: NotRequired[bool]
    horizontal_stretch: NotRequired[bool]
    vertical_stretch: NotRequired[bool]


class RadioButtonFieldSpec(TypedDict):
    __pydantic_config__ = {"extra": "forbid"}

    kind: Literal["radio_button_field"]
    name: str
    data_path: str
    choice_list: list[ChoiceListItemSpec]
    title: NotRequired[LocalizedTextSpec]
    radio_button_type: NotRequired[Literal["auto", "tumbler", "radio_buttons"]]
    columns_count: NotRequired[int]
    read_only: NotRequired[bool]


class ButtonSpec(TypedDict):
    __pydantic_config__ = {"extra": "forbid"}

    kind: Literal["button"]
    name: str
    command: str
    command_kind: NotRequired[Literal["custom", "form_standard", "item_standard"]]
    command_owner: NotRequired[str]
    default: NotRequired[bool]
    title: NotRequired[LocalizedTextSpec]


class TableSpec(TypedDict):
    __pydantic_config__ = {"extra": "forbid"}

    kind: Literal["table"]
    name: str
    data_path: str
    columns: list[InputFieldSpec | LabelFieldSpec]
    title: NotRequired[LocalizedTextSpec]
    read_only: NotRequired[bool]
    horizontal_stretch: NotRequired[bool]
    vertical_stretch: NotRequired[bool]


class PageSpec(TypedDict):
    __pydantic_config__ = {"extra": "forbid"}

    name: str
    title: LocalizedTextSpec
    children: list["ElementSpec"]


class PagesSpec(TypedDict):
    __pydantic_config__ = {"extra": "forbid"}

    kind: Literal["pages"]
    name: str
    title: LocalizedTextSpec
    representation: Literal["tabs_on_top"]
    pages: list[PageSpec]
    horizontal_stretch: NotRequired[bool]
    vertical_stretch: NotRequired[bool]


class UsualGroupSpec(TypedDict):
    __pydantic_config__ = {"extra": "forbid"}

    kind: Literal["usual_group"]
    name: str
    title: LocalizedTextSpec
    children: list["ElementSpec"]
    orientation: NotRequired[Literal["vertical", "horizontal", "always_horizontal"]]
    representation: NotRequired[
        Literal["none", "normal_separation", "strong_separation"]
    ]
    show_title: NotRequired[bool]
    horizontal_stretch: NotRequired[bool]
    vertical_stretch: NotRequired[bool]


ElementSpec: TypeAlias = (
    InputFieldSpec
    | CheckBoxFieldSpec
    | LabelDecorationSpec
    | LabelFieldSpec
    | RadioButtonFieldSpec
    | ButtonSpec
    | TableSpec
    | PagesSpec
    | UsualGroupSpec
)
GroupChildSpec: TypeAlias = ElementSpec


class FormCommandSpec(TypedDict):
    __pydantic_config__ = {"extra": "forbid"}

    name: str
    title: LocalizedTextSpec
    action: str


class FormEventSpec(TypedDict):
    __pydantic_config__ = {"extra": "forbid"}

    owner: NotRequired[str]
    event: Literal[
        "OnCreateAtServer",
        "OnOpen",
        "NotificationProcessing",
        "ExternalEvent",
        "FillCheckProcessingAtServer",
        "OnChange",
        "OnCurrentPageChange",
        "Selection",
        "OnActivateRow",
        "BeforeClose",
        "OnClose",
        "ChoiceProcessing",
        "StartChoice",
        "Clearing",
        "AutoComplete",
        "TextEditEnd",
        "Opening",
        "OnStartEdit",
        "BeforeAddRow",
        "BeforeRowChange",
        "BeforeDeleteRow",
        "AfterDeleteRow",
        "OnEditEnd",
        "OnReadAtServer",
        "BeforeWrite",
        "BeforeWriteAtServer",
        "OnWriteAtServer",
        "AfterWriteAtServer",
        "AfterWrite",
        "Click",
        "URLProcessing",
    ]
    handler: str


class ManagedFormSpec(TypedDict):
    __pydantic_config__ = {"extra": "forbid"}

    schema_version: Literal[1]
    form_name: str
    format_version: Literal["2.16"]
    platform_version: NotRequired[str]
    title: LocalizedTextSpec
    attributes: list[FormAttributeSpec]
    elements: list[ElementSpec]
    commands: list[FormCommandSpec]
    events: list[FormEventSpec]


@dataclass(frozen=True, slots=True)
class LocalizedText:
    ru: str


@dataclass(frozen=True, slots=True)
class StringType:
    length: int
    kind: Literal["string"] = "string"


@dataclass(frozen=True, slots=True)
class BooleanType:
    kind: Literal["boolean"] = "boolean"


@dataclass(frozen=True, slots=True)
class NumberType:
    digits: int
    fraction_digits: int
    allowed_sign: Literal["any", "nonnegative"]
    kind: Literal["number"] = "number"


@dataclass(frozen=True, slots=True)
class DateType:
    fractions: Literal["date", "date_time"]
    kind: Literal["date"] = "date"


ScalarType: TypeAlias = StringType | BooleanType | NumberType | DateType


@dataclass(frozen=True, slots=True)
class MetadataReferenceType:
    object: str
    kind: Literal["metadata_reference"] = "metadata_reference"


ValueType: TypeAlias = ScalarType | MetadataReferenceType


@dataclass(frozen=True, slots=True)
class CompositeType:
    variants: tuple[ValueType, ...]
    kind: Literal["composite"] = "composite"


@dataclass(frozen=True, slots=True)
class ValueTableColumn:
    name: str
    type: ValueType | CompositeType
    title: LocalizedText | None = None


@dataclass(frozen=True, slots=True)
class ValueTableType:
    columns: tuple[ValueTableColumn, ...]
    kind: Literal["value_table"] = "value_table"


@dataclass(frozen=True, slots=True)
class MetadataObjectType:
    object: str
    kind: Literal["metadata_object"] = "metadata_object"


@dataclass(frozen=True, slots=True)
class DynamicListType:
    main_table: str
    dynamic_data_read: bool
    kind: Literal["dynamic_list"] = "dynamic_list"


AttributeType: TypeAlias = (
    ValueType | CompositeType | ValueTableType | MetadataObjectType | DynamicListType
)


@dataclass(frozen=True, slots=True)
class FormAttribute:
    name: str
    type: AttributeType
    title: LocalizedText | None = None
    main: bool = False


@dataclass(frozen=True, slots=True)
class ChoiceListItem:
    value: str
    presentation: LocalizedText


@dataclass(frozen=True, slots=True)
class InputField:
    name: str
    data_path: str
    title: LocalizedText | None = None
    multiline: bool = False
    read_only: bool = False
    list_choice_mode: bool = False
    choice_list: tuple[ChoiceListItem, ...] = ()
    horizontal_stretch: bool | None = None
    vertical_stretch: bool | None = None
    kind: Literal["input_field"] = "input_field"


@dataclass(frozen=True, slots=True)
class CheckBoxField:
    name: str
    data_path: str
    title: LocalizedText | None = None
    read_only: bool = False
    kind: Literal["check_box_field"] = "check_box_field"


@dataclass(frozen=True, slots=True)
class LabelDecoration:
    name: str
    title: LocalizedText | None = None
    hyperlink: bool = False
    horizontal_stretch: bool | None = None
    vertical_stretch: bool | None = None
    kind: Literal["label_decoration"] = "label_decoration"


@dataclass(frozen=True, slots=True)
class LabelField:
    name: str
    data_path: str
    title: LocalizedText | None = None
    hyperlink: bool = False
    read_only: bool = False
    horizontal_stretch: bool | None = None
    vertical_stretch: bool | None = None
    kind: Literal["label_field"] = "label_field"


@dataclass(frozen=True, slots=True)
class RadioButtonField:
    name: str
    data_path: str
    choice_list: tuple[ChoiceListItem, ...]
    title: LocalizedText | None = None
    radio_button_type: Literal["auto", "tumbler", "radio_buttons"] = "auto"
    columns_count: int | None = None
    read_only: bool = False
    kind: Literal["radio_button_field"] = "radio_button_field"


@dataclass(frozen=True, slots=True)
class Button:
    name: str
    command: str
    command_kind: Literal["custom", "form_standard", "item_standard"] = "custom"
    command_owner: str | None = None
    default: bool = False
    title: LocalizedText | None = None
    kind: Literal["button"] = "button"


@dataclass(frozen=True, slots=True)
class Table:
    name: str
    data_path: str
    columns: tuple[InputField | LabelField, ...]
    title: LocalizedText | None = None
    read_only: bool = False
    horizontal_stretch: bool | None = None
    vertical_stretch: bool | None = None
    kind: Literal["table"] = "table"


@dataclass(frozen=True, slots=True)
class Page:
    name: str
    title: LocalizedText
    children: tuple["Element", ...]


@dataclass(frozen=True, slots=True)
class Pages:
    name: str
    title: LocalizedText
    representation: Literal["tabs_on_top"]
    pages: tuple[Page, ...]
    horizontal_stretch: bool | None = None
    vertical_stretch: bool | None = None
    kind: Literal["pages"] = "pages"


@dataclass(frozen=True, slots=True)
class UsualGroup:
    name: str
    title: LocalizedText
    children: tuple["Element", ...]
    orientation: Literal["vertical", "horizontal", "always_horizontal"] = "vertical"
    representation: Literal[
        "none", "normal_separation", "strong_separation"
    ] = "normal_separation"
    show_title: bool = True
    horizontal_stretch: bool | None = None
    vertical_stretch: bool | None = None
    kind: Literal["usual_group"] = "usual_group"


Element: TypeAlias = (
    InputField
    | CheckBoxField
    | LabelDecoration
    | LabelField
    | RadioButtonField
    | Button
    | Table
    | Pages
    | UsualGroup
)
GroupChild: TypeAlias = Element


@dataclass(frozen=True, slots=True)
class FormCommand:
    name: str
    title: LocalizedText
    action: str


@dataclass(frozen=True, slots=True)
class FormEvent:
    owner: str | None
    event: str
    handler: str


@dataclass(frozen=True, slots=True)
class ManagedForm:
    schema_version: Literal[1]
    form_name: str
    format_version: Literal["2.16"]
    platform_version: str | None
    event_profile: str
    title: LocalizedText
    attributes: tuple[FormAttribute, ...]
    elements: tuple[Element, ...]
    commands: tuple[FormCommand, ...]
    events: tuple[FormEvent, ...]


class FormsContractError(ValueError):
    """Спецификация не входит в доказанное подмножество Forms."""

    def __init__(self, diagnostics: tuple[Diagnostic, ...]) -> None:
        self.diagnostics = diagnostics
        super().__init__(
            "спецификация формы отклонена: "
            + "; ".join(item.message for item in diagnostics)
        )


class _Reader:
    def __init__(self) -> None:
        self.diagnostics: list[Diagnostic] = []

    def issue(self, code: str, path: str, message: str) -> None:
        self.diagnostics.append(
            Diagnostic("structural", "failed", code, path, message)
        )

    def object(self, value: object, path: str) -> Mapping[str, object]:
        if not isinstance(value, Mapping):
            self.issue("invalid_type", path, "Ожидался объект.")
            return {}
        return value

    def exact_keys(
        self,
        value: Mapping[str, object],
        path: str,
        *,
        required: frozenset[str],
        optional: frozenset[str] = frozenset(),
    ) -> None:
        for key in sorted(required.difference(value)):
            self.issue("missing_key", f"{path}.{key}", "Обязательное поле отсутствует.")
        unknown = [key for key in value if key not in required | optional]
        for key in sorted(unknown, key=lambda item: repr(item)):
            key_path = key if isinstance(key, str) else repr(key)
            self.issue("unknown_key", f"{path}.{key_path}", "Неизвестное поле запрещено.")

    def string(
        self,
        value: object,
        path: str,
        *,
        identifier: bool = False,
        data_path: bool = False,
    ) -> str:
        if not isinstance(value, str) or not value.strip():
            self.issue("invalid_string", path, "Ожидалась непустая строка.")
            return ""
        if value != value.strip():
            self.issue("surrounding_whitespace", path, "Пробелы по краям запрещены.")
        if identifier and not _IDENTIFIER.fullmatch(value):
            self.issue("invalid_identifier", path, "Недопустимый идентификатор 1С.")
        elif identifier and is_reserved_bsl_keyword(value):
            self.issue(
                "reserved_bsl_keyword",
                path,
                "Зарезервированное слово BSL нельзя использовать как идентификатор 1С.",
            )
        if data_path and not _DATA_PATH.fullmatch(value):
            self.issue("invalid_data_path", path, "Недопустимый обычный DataPath.")
        return value

    def boolean(self, value: object, path: str) -> bool:
        if type(value) is not bool:
            self.issue("invalid_type", path, "Ожидалось логическое значение.")
            return False
        return value

    def integer(
        self,
        value: object,
        path: str,
        *,
        minimum: int,
        maximum: int,
    ) -> int:
        if type(value) is not int or not minimum <= value <= maximum:
            self.issue(
                "invalid_integer",
                path,
                f"Ожидалось целое число от {minimum} до {maximum}.",
            )
            return minimum
        return value

    def array(
        self, value: object, path: str, *, allow_empty: bool = False
    ) -> list[object]:
        if not isinstance(value, list):
            self.issue("invalid_type", path, "Ожидался массив.")
            return []
        if not value and not allow_empty:
            self.issue("empty_collection", path, "Пустой массив не поддержан.")
        return value


def _localized(reader: _Reader, value: object, path: str) -> LocalizedText:
    item = reader.object(value, path)
    reader.exact_keys(item, path, required=frozenset({"ru"}))
    return LocalizedText(reader.string(item.get("ru"), f"{path}.ru"))


def _optional_localized(
    reader: _Reader, item: Mapping[str, object], path: str
) -> LocalizedText | None:
    if "title" not in item:
        return None
    return _localized(reader, item.get("title"), f"{path}.title")


def _optional_boolean(
    reader: _Reader,
    item: Mapping[str, object],
    key: str,
    path: str,
) -> bool | None:
    if key not in item:
        return None
    return reader.boolean(item[key], f"{path}.{key}")


def _duplicates(
    reader: _Reader,
    values: list[tuple[Hashable, str]],
    *,
    code: str,
    message: str,
) -> None:
    seen: set[Hashable] = set()
    for value, path in values:
        if value and value in seen:
            reader.issue(code, path, message)
        seen.add(value)


def _value_type(reader: _Reader, value: object, path: str) -> ValueType:
    item = reader.object(value, path)
    kind = item.get("kind")
    if kind == "metadata_reference":
        reader.exact_keys(item, path, required=frozenset({"kind", "object"}))
        object_name = reader.string(item.get("object"), f"{path}.object")
        if metadata_reference_xml_type(object_name) is None:
            reader.issue(
                "invalid_metadata_reference",
                f"{path}.object",
                "Ожидалась поддержанная ссылка вида Справочник.ИмяОбъекта.",
            )
        return MetadataReferenceType(object_name)
    if kind == "string":
        reader.exact_keys(item, path, required=frozenset({"kind", "length"}))
        return StringType(
            reader.integer(
                item.get("length"),
                f"{path}.length",
                minimum=0,
                maximum=1_048_576,
            )
        )
    if kind == "boolean":
        reader.exact_keys(item, path, required=frozenset({"kind"}))
        return BooleanType()
    if kind == "number":
        reader.exact_keys(
            item,
            path,
            required=frozenset(
                {"kind", "digits", "fraction_digits", "allowed_sign"}
            ),
        )
        digits = reader.integer(
            item.get("digits"), f"{path}.digits", minimum=1, maximum=32
        )
        fractions = reader.integer(
            item.get("fraction_digits"),
            f"{path}.fraction_digits",
            minimum=0,
            maximum=32,
        )
        if fractions > digits:
            reader.issue(
                "invalid_number_fraction_digits",
                f"{path}.fraction_digits",
                "Знаков дробной части не может быть больше общей разрядности.",
            )
        sign = item.get("allowed_sign")
        if sign not in {"any", "nonnegative"}:
            reader.issue(
                "invalid_allowed_sign",
                f"{path}.allowed_sign",
                "Допустимы значения any и nonnegative.",
            )
            sign = "any"
        return NumberType(digits, fractions, sign)
    if kind == "date":
        reader.exact_keys(item, path, required=frozenset({"kind", "fractions"}))
        fractions = item.get("fractions")
        if fractions not in {"date", "date_time"}:
            reader.issue(
                "invalid_date_fractions",
                f"{path}.fractions",
                "Допустимы значения date и date_time.",
            )
            fractions = "date_time"
        return DateType(fractions)
    reader.exact_keys(item, path, required=frozenset({"kind"}))
    reader.issue(
        "unsupported_attribute_type",
        f"{path}.kind",
        "Поддержаны string, boolean, number, date и metadata_reference.",
    )
    return StringType(1)


def _value_table_column(
    reader: _Reader, value: object, path: str
) -> ValueTableColumn:
    item = reader.object(value, path)
    reader.exact_keys(
        item,
        path,
        required=frozenset({"name", "type"}),
        optional=frozenset({"title"}),
    )
    return ValueTableColumn(
        name=reader.string(item.get("name"), f"{path}.name", identifier=True),
        type=_composite_or_value_type(reader, item.get("type"), f"{path}.type"),
        title=_optional_localized(reader, item, path),
    )


def _attribute_type(reader: _Reader, value: object, path: str) -> AttributeType:
    item = reader.object(value, path)
    if item.get("kind") == "dynamic_list":
        reader.exact_keys(
            item,
            path,
            required=frozenset({"kind", "main_table", "dynamic_data_read"}),
        )
        main_table = reader.string(item.get("main_table"), f"{path}.main_table")
        if dynamic_list_xml_table(main_table) is None:
            reader.issue(
                "invalid_dynamic_list_main_table",
                f"{path}.main_table",
                "Ожидалась прямая ссылка на поддержанный объект Registry.",
            )
        return DynamicListType(
            main_table,
            reader.boolean(
                item.get("dynamic_data_read"), f"{path}.dynamic_data_read"
            ),
        )
    if item.get("kind") == "metadata_object":
        reader.exact_keys(item, path, required=frozenset({"kind", "object"}))
        object_name = reader.string(item.get("object"), f"{path}.object")
        if metadata_object_xml_type(object_name) is None:
            reader.issue(
                "invalid_metadata_object",
                f"{path}.object",
                "Ожидалась поддержанная ссылка вида Обработка.ИмяОбъекта.",
            )
        return MetadataObjectType(object_name)
    if item.get("kind") != "value_table":
        return _composite_or_value_type(reader, value, path)
    reader.exact_keys(item, path, required=frozenset({"kind", "columns"}))
    columns = tuple(
        _value_table_column(reader, raw, f"{path}.columns[{index}]")
        for index, raw in enumerate(
            reader.array(item.get("columns"), f"{path}.columns")
        )
    )
    _duplicates(
        reader,
        [
            (column.name, f"{path}.columns[{index}].name")
            for index, column in enumerate(columns)
        ],
        code="duplicate_table_column_name",
        message="Имя колонки таблицы значений повторяется.",
    )
    return ValueTableType(columns)


def _composite_or_value_type(
    reader: _Reader, value: object, path: str
) -> ValueType | CompositeType:
    item = reader.object(value, path)
    if item.get("kind") != "composite":
        return _value_type(reader, value, path)
    reader.exact_keys(item, path, required=frozenset({"kind", "variants"}))
    variants = tuple(
        _value_type(reader, raw, f"{path}.variants[{index}]")
        for index, raw in enumerate(
            reader.array(item.get("variants"), f"{path}.variants")
        )
    )
    if len(variants) < 2:
        reader.issue(
            "composite_type_too_small",
            f"{path}.variants",
            "Составной тип должен содержать не менее 2 вариантов.",
        )
    _duplicates(
        reader,
        [
            (
                (
                    variant.kind,
                    variant.object
                    if isinstance(variant, MetadataReferenceType)
                    else None,
                ),
                f"{path}.variants[{index}]",
            )
            for index, variant in enumerate(variants)
        ],
        code="duplicate_composite_type_variant",
        message="Вариант составного типа повторяется.",
    )
    return CompositeType(variants)


def _attribute(reader: _Reader, value: object, path: str) -> FormAttribute:
    item = reader.object(value, path)
    reader.exact_keys(
        item,
        path,
        required=frozenset({"name", "type"}),
        optional=frozenset({"title", "main"}),
    )
    return FormAttribute(
        name=reader.string(item.get("name"), f"{path}.name", identifier=True),
        type=_attribute_type(reader, item.get("type"), f"{path}.type"),
        title=_optional_localized(reader, item, path),
        main=(
            reader.boolean(item["main"], f"{path}.main")
            if "main" in item
            else False
        ),
    )


def _choice_item(reader: _Reader, value: object, path: str) -> ChoiceListItem:
    item = reader.object(value, path)
    reader.exact_keys(
        item, path, required=frozenset({"value", "presentation"})
    )
    return ChoiceListItem(
        value=reader.string(item.get("value"), f"{path}.value"),
        presentation=_localized(
            reader, item.get("presentation"), f"{path}.presentation"
        ),
    )


def _input_field(
    reader: _Reader, item: Mapping[str, object], path: str
) -> InputField:
    reader.exact_keys(
        item,
        path,
        required=frozenset({"kind", "name", "data_path"}),
        optional=frozenset(
            {
                "title",
                "multiline",
                "read_only",
                "list_choice_mode",
                "choice_list",
                "horizontal_stretch",
                "vertical_stretch",
            }
        ),
    )
    choices = tuple(
        _choice_item(reader, raw, f"{path}.choice_list[{index}]")
        for index, raw in enumerate(
            reader.array(item.get("choice_list"), f"{path}.choice_list")
            if "choice_list" in item
            else []
        )
    )
    _duplicates(
        reader,
        [
            (choice.value, f"{path}.choice_list[{index}].value")
            for index, choice in enumerate(choices)
        ],
        code="duplicate_choice_value",
        message="Значение списка выбора повторяется.",
    )
    list_mode = (
        reader.boolean(item["list_choice_mode"], f"{path}.list_choice_mode")
        if "list_choice_mode" in item
        else False
    )
    if choices and not list_mode:
        reader.issue(
            "choice_list_requires_list_mode",
            f"{path}.choice_list",
            "ChoiceList требует list_choice_mode=true.",
        )
    return InputField(
        name=reader.string(item.get("name"), f"{path}.name", identifier=True),
        data_path=reader.string(
            item.get("data_path"), f"{path}.data_path", data_path=True
        ),
        title=_optional_localized(reader, item, path),
        multiline=(
            reader.boolean(item["multiline"], f"{path}.multiline")
            if "multiline" in item
            else False
        ),
        read_only=(
            reader.boolean(item["read_only"], f"{path}.read_only")
            if "read_only" in item
            else False
        ),
        list_choice_mode=list_mode,
        choice_list=choices,
        horizontal_stretch=_optional_boolean(
            reader, item, "horizontal_stretch", path
        ),
        vertical_stretch=_optional_boolean(
            reader, item, "vertical_stretch", path
        ),
    )


def _check_box_field(
    reader: _Reader, item: Mapping[str, object], path: str
) -> CheckBoxField:
    reader.exact_keys(
        item,
        path,
        required=frozenset({"kind", "name", "data_path"}),
        optional=frozenset({"title", "read_only"}),
    )
    return CheckBoxField(
        name=reader.string(item.get("name"), f"{path}.name", identifier=True),
        data_path=reader.string(
            item.get("data_path"), f"{path}.data_path", data_path=True
        ),
        title=_optional_localized(reader, item, path),
        read_only=(
            reader.boolean(item["read_only"], f"{path}.read_only")
            if "read_only" in item
            else False
        ),
    )


def _label_decoration(
    reader: _Reader, item: Mapping[str, object], path: str
) -> LabelDecoration:
    reader.exact_keys(
        item,
        path,
        required=frozenset({"kind", "name"}),
        optional=frozenset(
            {
                "title",
                "hyperlink",
                "horizontal_stretch",
                "vertical_stretch",
            }
        ),
    )
    return LabelDecoration(
        name=reader.string(item.get("name"), f"{path}.name", identifier=True),
        title=_optional_localized(reader, item, path),
        hyperlink=(
            reader.boolean(item["hyperlink"], f"{path}.hyperlink")
            if "hyperlink" in item
            else False
        ),
        horizontal_stretch=_optional_boolean(
            reader, item, "horizontal_stretch", path
        ),
        vertical_stretch=_optional_boolean(
            reader, item, "vertical_stretch", path
        ),
    )


def _label_field(
    reader: _Reader, item: Mapping[str, object], path: str
) -> LabelField:
    reader.exact_keys(
        item,
        path,
        required=frozenset({"kind", "name", "data_path"}),
        optional=frozenset(
            {
                "title",
                "hyperlink",
                "read_only",
                "horizontal_stretch",
                "vertical_stretch",
            }
        ),
    )
    return LabelField(
        name=reader.string(item.get("name"), f"{path}.name", identifier=True),
        data_path=reader.string(
            item.get("data_path"), f"{path}.data_path", data_path=True
        ),
        title=_optional_localized(reader, item, path),
        hyperlink=(
            reader.boolean(item["hyperlink"], f"{path}.hyperlink")
            if "hyperlink" in item
            else False
        ),
        read_only=(
            reader.boolean(item["read_only"], f"{path}.read_only")
            if "read_only" in item
            else False
        ),
        horizontal_stretch=_optional_boolean(
            reader, item, "horizontal_stretch", path
        ),
        vertical_stretch=_optional_boolean(
            reader, item, "vertical_stretch", path
        ),
    )


def _radio_button_field(
    reader: _Reader, item: Mapping[str, object], path: str
) -> RadioButtonField:
    reader.exact_keys(
        item,
        path,
        required=frozenset({"kind", "name", "data_path", "choice_list"}),
        optional=frozenset(
            {"title", "radio_button_type", "columns_count", "read_only"}
        ),
    )
    choices = tuple(
        _choice_item(reader, raw, f"{path}.choice_list[{index}]")
        for index, raw in enumerate(
            reader.array(item.get("choice_list"), f"{path}.choice_list")
        )
    )
    if len(choices) < 2:
        reader.issue(
            "radio_button_choices_too_small",
            f"{path}.choice_list",
            "Переключателю нужны хотя бы два варианта.",
        )
    _duplicates(
        reader,
        [
            (choice.value, f"{path}.choice_list[{index}].value")
            for index, choice in enumerate(choices)
        ],
        code="duplicate_choice_value",
        message="Значение списка выбора повторяется.",
    )
    radio_type = item.get("radio_button_type", "auto")
    if radio_type not in {"auto", "tumbler", "radio_buttons"}:
        reader.issue(
            "invalid_radio_button_type",
            f"{path}.radio_button_type",
            "Неизвестный вид переключателя.",
        )
        radio_type = "auto"
    return RadioButtonField(
        name=reader.string(item.get("name"), f"{path}.name", identifier=True),
        data_path=reader.string(
            item.get("data_path"), f"{path}.data_path", data_path=True
        ),
        choice_list=choices,
        title=_optional_localized(reader, item, path),
        radio_button_type=radio_type,
        columns_count=(
            reader.integer(
                item["columns_count"],
                f"{path}.columns_count",
                minimum=1,
                maximum=100,
            )
            if "columns_count" in item
            else None
        ),
        read_only=(
            reader.boolean(item["read_only"], f"{path}.read_only")
            if "read_only" in item
            else False
        ),
    )


def _button(reader: _Reader, item: Mapping[str, object], path: str) -> Button:
    reader.exact_keys(
        item,
        path,
        required=frozenset({"kind", "name", "command"}),
        optional=frozenset(
            {"command_kind", "command_owner", "default", "title"}
        ),
    )
    command_kind = item.get("command_kind", "custom")
    if command_kind not in {"custom", "form_standard", "item_standard"}:
        reader.issue(
            "invalid_command_kind",
            f"{path}.command_kind",
            "Допустимы custom, form_standard и item_standard.",
        )
        command_kind = "custom"
    command_owner = None
    if "command_owner" in item:
        command_owner = reader.string(
            item.get("command_owner"), f"{path}.command_owner", identifier=True
        )
    if command_kind == "item_standard" and "command_owner" not in item:
        reader.issue(
            "missing_key",
            f"{path}.command_owner",
            "Для item_standard обязателен владелец команды.",
        )
    elif command_kind != "item_standard" and "command_owner" in item:
        reader.issue(
            "unexpected_command_owner",
            f"{path}.command_owner",
            "Владелец допустим только для item_standard.",
        )
    return Button(
        name=reader.string(item.get("name"), f"{path}.name", identifier=True),
        command=reader.string(
            item.get("command"), f"{path}.command", identifier=True
        ),
        command_kind=command_kind,
        command_owner=command_owner,
        default=(
            reader.boolean(item["default"], f"{path}.default")
            if "default" in item
            else False
        ),
        title=_optional_localized(reader, item, path),
    )


def _table(reader: _Reader, item: Mapping[str, object], path: str) -> Table:
    reader.exact_keys(
        item,
        path,
        required=frozenset({"kind", "name", "data_path", "columns"}),
        optional=frozenset(
            {
                "title",
                "read_only",
                "horizontal_stretch",
                "vertical_stretch",
            }
        ),
    )
    columns: list[InputField | LabelField] = []
    for index, raw in enumerate(
        reader.array(item.get("columns"), f"{path}.columns")
    ):
        column_path = f"{path}.columns[{index}]"
        column = reader.object(raw, column_path)
        if column.get("kind") not in {"input_field", "label_field"}:
            reader.issue(
                "unsupported_table_column_kind",
                f"{column_path}.kind",
                "Колонка таблицы должна быть input_field либо label_field.",
            )
            continue
        if column.get("kind") == "label_field":
            columns.append(_label_field(reader, column, column_path))
        else:
            columns.append(_input_field(reader, column, column_path))
    return Table(
        name=reader.string(item.get("name"), f"{path}.name", identifier=True),
        data_path=reader.string(
            item.get("data_path"), f"{path}.data_path", data_path=True
        ),
        columns=tuple(columns),
        title=_optional_localized(reader, item, path),
        read_only=(
            reader.boolean(item["read_only"], f"{path}.read_only")
            if "read_only" in item
            else False
        ),
        horizontal_stretch=_optional_boolean(
            reader, item, "horizontal_stretch", path
        ),
        vertical_stretch=_optional_boolean(
            reader, item, "vertical_stretch", path
        ),
    )


def _page(reader: _Reader, value: object, path: str) -> Page:
    item = reader.object(value, path)
    reader.exact_keys(
        item, path, required=frozenset({"name", "title", "children"})
    )
    return Page(
        name=reader.string(item.get("name"), f"{path}.name", identifier=True),
        title=_localized(reader, item.get("title"), f"{path}.title"),
        children=tuple(
            _element(reader, raw, f"{path}.children[{index}]")
            for index, raw in enumerate(
                reader.array(item.get("children"), f"{path}.children")
            )
        ),
    )


def _pages(reader: _Reader, item: Mapping[str, object], path: str) -> Pages:
    reader.exact_keys(
        item,
        path,
        required=frozenset(
            {"kind", "name", "title", "representation", "pages"}
        ),
        optional=frozenset({"horizontal_stretch", "vertical_stretch"}),
    )
    representation = item.get("representation")
    if representation != "tabs_on_top":
        reader.issue(
            "unsupported_pages_representation",
            f"{path}.representation",
            "Базовый compiler поддерживает tabs_on_top.",
        )
        representation = "tabs_on_top"
    return Pages(
        name=reader.string(item.get("name"), f"{path}.name", identifier=True),
        title=_localized(reader, item.get("title"), f"{path}.title"),
        representation=representation,
        pages=tuple(
            _page(reader, raw, f"{path}.pages[{index}]")
            for index, raw in enumerate(
                reader.array(item.get("pages"), f"{path}.pages")
            )
        ),
        horizontal_stretch=_optional_boolean(
            reader, item, "horizontal_stretch", path
        ),
        vertical_stretch=_optional_boolean(
            reader, item, "vertical_stretch", path
        ),
    )


def _group(
    reader: _Reader, item: Mapping[str, object], path: str
) -> UsualGroup:
    reader.exact_keys(
        item,
        path,
        required=frozenset({"kind", "name", "title", "children"}),
        optional=frozenset(
            {
                "orientation",
                "representation",
                "show_title",
                "horizontal_stretch",
                "vertical_stretch",
            }
        ),
    )
    orientation = item.get("orientation", "vertical")
    if orientation not in {"vertical", "horizontal", "always_horizontal"}:
        reader.issue(
            "invalid_group_orientation",
            f"{path}.orientation",
            "Допустимы vertical, horizontal и always_horizontal.",
        )
        orientation = "vertical"
    representation = item.get("representation", "normal_separation")
    if representation not in {
        "none",
        "normal_separation",
        "strong_separation",
    }:
        reader.issue(
            "invalid_group_representation",
            f"{path}.representation",
            "Недопустимое представление группы.",
        )
        representation = "normal_separation"
    return UsualGroup(
        name=reader.string(item.get("name"), f"{path}.name", identifier=True),
        title=_localized(reader, item.get("title"), f"{path}.title"),
        children=tuple(
            _element(reader, raw, f"{path}.children[{index}]")
            for index, raw in enumerate(
                reader.array(item.get("children"), f"{path}.children")
            )
        ),
        orientation=orientation,
        representation=representation,
        show_title=(
            reader.boolean(item["show_title"], f"{path}.show_title")
            if "show_title" in item
            else True
        ),
        horizontal_stretch=_optional_boolean(
            reader, item, "horizontal_stretch", path
        ),
        vertical_stretch=_optional_boolean(
            reader, item, "vertical_stretch", path
        ),
    )


def _element(reader: _Reader, value: object, path: str) -> Element:
    item = reader.object(value, path)
    kind = item.get("kind")
    if kind == "input_field":
        return _input_field(reader, item, path)
    if kind == "check_box_field":
        return _check_box_field(reader, item, path)
    if kind == "label_decoration":
        return _label_decoration(reader, item, path)
    if kind == "label_field":
        return _label_field(reader, item, path)
    if kind == "radio_button_field":
        return _radio_button_field(reader, item, path)
    if kind == "button":
        return _button(reader, item, path)
    if kind == "table":
        return _table(reader, item, path)
    if kind == "pages":
        return _pages(reader, item, path)
    if kind == "usual_group":
        return _group(reader, item, path)
    reader.issue(
        "unsupported_element_kind",
        f"{path}.kind",
        "Элемент не входит в поддержанный базовый слой Forms.",
    )
    return InputField("НедопустимыйЭлемент", "НедопустимыйРеквизит")


def _command(reader: _Reader, value: object, path: str) -> FormCommand:
    item = reader.object(value, path)
    reader.exact_keys(
        item, path, required=frozenset({"name", "title", "action"})
    )
    return FormCommand(
        name=reader.string(item.get("name"), f"{path}.name", identifier=True),
        title=_localized(reader, item.get("title"), f"{path}.title"),
        action=reader.string(
            item.get("action"), f"{path}.action", identifier=True
        ),
    )


def _event(reader: _Reader, value: object, path: str) -> FormEvent:
    item = reader.object(value, path)
    reader.exact_keys(
        item,
        path,
        required=frozenset({"event", "handler"}),
        optional=frozenset({"owner"}),
    )
    return FormEvent(
        owner=(
            reader.string(item.get("owner"), f"{path}.owner", identifier=True)
            if "owner" in item
            else None
        ),
        event=reader.string(item.get("event"), f"{path}.event"),
        handler=reader.string(
            item.get("handler"), f"{path}.handler", identifier=True
        ),
    )


def _walk_elements(
    elements: tuple[Element, ...],
    base_path: str,
    *,
    table: Table | None = None,
) -> Iterator[tuple[Element, str, Table | None]]:
    for index, element in enumerate(elements):
        path = f"{base_path}[{index}]"
        yield element, path, table
        if isinstance(element, UsualGroup):
            yield from _walk_elements(
                element.children, f"{path}.children"
            )
        elif isinstance(element, Pages):
            for page_index, page in enumerate(element.pages):
                yield from _walk_elements(
                    page.children,
                    f"{path}.pages[{page_index}].children",
                )
        elif isinstance(element, Table):
            yield from _walk_elements(
                element.columns, f"{path}.columns", table=element
            )


def parse_managed_form_spec(payload: object) -> ManagedForm:
    """Проверить и нормализовать поддержанную спецификацию."""

    reader = _Reader()
    root = reader.object(payload, "$")
    reader.exact_keys(
        root,
        "$",
        required=frozenset(
            {
                "schema_version",
                "form_name",
                "format_version",
                "title",
                "attributes",
                "elements",
                "commands",
                "events",
            }
        ),
        optional=frozenset({"platform_version"}),
    )
    if (
        type(root.get("schema_version")) is not int
        or root.get("schema_version") != SPECIFICATION_VERSION
    ):
        reader.issue(
            "unsupported_schema_version",
            "$.schema_version",
            "Поддерживается только schema_version=1.",
        )
    if root.get("format_version") != SUPPORTED_FORMAT_VERSION:
        reader.issue(
            "unsupported_format_version",
            "$.format_version",
            "Compiler поддерживает только формат 2.16.",
        )

    platform_version: str | None = None
    profile = platform_profile(None)
    if "platform_version" in root:
        raw_platform_version = root.get("platform_version")
        if not isinstance(raw_platform_version, str) or (
            normalized_platform_version(raw_platform_version) is None
        ):
            reader.issue(
                "invalid_platform_version",
                "$.platform_version",
                "Версия платформы должна иметь вид 8.3.23 или 8.3.23.1997.",
            )
            profile = None
        else:
            platform_version = raw_platform_version
            profile = platform_profile(platform_version)
            if profile is None:  # pragma: no cover - формат уже проверен выше
                reader.issue(
                    "invalid_platform_version",
                    "$.platform_version",
                    "Не удалось выбрать профиль для корректной версии.",
                )
    event_profile = profile.event_profile if profile is not None else "modern"

    form_name = reader.string(
        root.get("form_name"), "$.form_name", identifier=True
    )
    title = _localized(reader, root.get("title"), "$.title")
    attributes = tuple(
        _attribute(reader, value, f"$.attributes[{index}]")
        for index, value in enumerate(
            reader.array(root.get("attributes"), "$.attributes")
        )
    )
    elements = tuple(
        _element(reader, value, f"$.elements[{index}]")
        for index, value in enumerate(
            reader.array(root.get("elements"), "$.elements")
        )
    )
    commands = tuple(
        _command(reader, value, f"$.commands[{index}]")
        for index, value in enumerate(
            reader.array(root.get("commands"), "$.commands", allow_empty=True)
        )
    )
    events = tuple(
        _event(reader, value, f"$.events[{index}]")
        for index, value in enumerate(
            reader.array(root.get("events"), "$.events")
        )
    )

    _duplicates(
        reader,
        [
            (item.name, f"$.attributes[{index}].name")
            for index, item in enumerate(attributes)
        ],
        code="duplicate_attribute_name",
        message="Имя реквизита повторяется.",
    )
    _duplicates(
        reader,
        [
            (item.name, f"$.commands[{index}].name")
            for index, item in enumerate(commands)
        ],
        code="duplicate_command_name",
        message="Имя команды повторяется.",
    )
    walked = list(_walk_elements(elements, "$.elements"))
    _duplicates(
        reader,
        [(item.name, f"{path}.name") for item, path, _table in walked],
        code="duplicate_element_name",
        message="Имя элемента повторяется.",
    )
    if sum(item.main for item in attributes) > 1:
        reader.issue(
            "multiple_main_attributes",
            "$.attributes",
            "Главным может быть не более одного реквизита.",
        )

    attribute_by_name = {item.name: item for item in attributes}
    command_names = {item.name for item in commands}
    element_by_name = {item.name: item for item, _path, _table in walked}
    main_object = next(
        (
            attribute.type
            for attribute in attributes
            if attribute.main and isinstance(attribute.type, MetadataObjectType)
        ),
        None,
    )
    document_main_object = (
        main_object is not None
        and main_object.object.split(".", 1)[0] == "Документ"
    )
    for element, path, table in walked:
        if isinstance(
            element, (InputField, CheckBoxField, LabelField, RadioButtonField)
        ):
            if table is None:
                root_name, separator, _nested_path = element.data_path.partition(
                    "."
                )
                attribute = attribute_by_name.get(root_name)
                if attribute is None:
                    reader.issue(
                        "unresolved_data_path",
                        f"{path}.data_path",
                        "DataPath не разрешается в реквизит формы.",
                    )
                elif separator and not isinstance(
                    attribute.type, (MetadataObjectType, DynamicListType)
                ):
                    reader.issue(
                        "unresolved_data_path",
                        f"{path}.data_path",
                        "Вложенный DataPath допустим только для объектного "
                        "реквизита или DynamicList.",
                    )
                elif isinstance(element, CheckBoxField) and not isinstance(
                    attribute.type, BooleanType
                ) and not (
                    separator
                    and isinstance(
                        attribute.type, (MetadataObjectType, DynamicListType)
                    )
                ):
                    reader.issue(
                        "check_box_requires_boolean",
                        f"{path}.data_path",
                        "CheckBoxField требует boolean-реквизит.",
                    )
                elif (
                    isinstance(element, InputField)
                    and element.choice_list
                    and not isinstance(attribute.type, StringType)
                    and not (
                        separator
                        and isinstance(
                            attribute.type, (MetadataObjectType, DynamicListType)
                        )
                    )
                ):
                    reader.issue(
                        "choice_list_requires_string",
                        f"{path}.choice_list",
                        "Строковый ChoiceList требует строковый реквизит.",
                    )
                elif isinstance(element, RadioButtonField) and not isinstance(
                    attribute.type, StringType
                ):
                    reader.issue(
                        "radio_button_requires_string",
                        f"{path}.data_path",
                        "Строковый переключатель требует строковый реквизит.",
                    )
            else:
                table_attribute = attribute_by_name.get(table.data_path)
                prefix = table.data_path + "."
                column_name = element.data_path.removeprefix(prefix)
                declared = (
                    {
                        column.name
                        for column in table_attribute.type.columns
                    }
                    if table_attribute is not None
                    and isinstance(table_attribute.type, ValueTableType)
                    else set()
                )
                dynamic_list = (
                    table_attribute is not None
                    and isinstance(table_attribute.type, DynamicListType)
                )
                if (
                    not element.data_path.startswith(prefix)
                    or (not dynamic_list and column_name not in declared)
                ):
                    reader.issue(
                        "unresolved_table_column",
                        f"{path}.data_path",
                        "DataPath не разрешается в колонку таблицы.",
                    )
        elif isinstance(element, Table):
            attribute = attribute_by_name.get(element.data_path)
            if attribute is None or not isinstance(
                attribute.type, (ValueTableType, DynamicListType)
            ):
                reader.issue(
                    "table_requires_value_table",
                    f"{path}.data_path",
                    "Таблица должна ссылаться на реквизит value_table или dynamic_list.",
                )
        elif isinstance(element, Button):
            if element.command_kind == "custom":
                if element.command not in command_names:
                    reader.issue(
                        "unresolved_command",
                        f"{path}.command",
                        "Кнопка ссылается на неизвестную команду.",
                    )
            elif element.command_kind == "form_standard":
                if not standard_command_supported("form", element.command):
                    reader.issue(
                        "unsupported_standard_command",
                        f"{path}.command",
                        "Стандартная команда формы не входит в закрытый каталог.",
                    )
                elif (
                    element.command in DOCUMENT_FORM_STANDARD_COMMANDS
                    and not document_main_object
                ):
                    reader.issue(
                        "document_standard_command_requires_document_main_object",
                        f"{path}.command",
                        "Команда проведения требует главный объект Документ.*.",
                    )
                elif (
                    element.command in OBJECT_FORM_STANDARD_COMMANDS
                    and main_object is None
                ):
                    reader.issue(
                        "object_standard_command_requires_main_object",
                        f"{path}.command",
                        "Команда записи требует главный реквизит metadata_object.",
                    )
            else:
                owner = element_by_name.get(element.command_owner or "")
                if owner is None or owner.kind != "table":
                    reader.issue(
                        "unsupported_standard_command_owner",
                        f"{path}.command_owner",
                        "Поддержан владелец стандартной команды вида table.",
                    )
                elif not standard_command_supported(owner.kind, element.command):
                    reader.issue(
                        "unsupported_standard_command",
                        f"{path}.command",
                        "Стандартная команда элемента не входит в закрытый каталог.",
                    )

    event_signatures: dict[str, set[tuple[str, tuple[str, ...]]]] = {}
    for index, event in enumerate(events):
        path = f"$.events[{index}]"
        if event.owner is None:
            owner_kind = "form"
        else:
            owner = element_by_name.get(event.owner)
            if owner is None:
                reader.issue(
                    "unresolved_event_owner",
                    f"{path}.owner",
                    "Владелец события не разрешается в элемент формы.",
                )
                continue
            owner_kind = owner.kind
        signature = event_signature(
            owner_kind,
            event.event,
            profile=event_profile,
        )
        if signature is None:
            reader.issue(
                "unsupported_owner_event",
                f"{path}.event",
                "Событие не поддерживается для указанного владельца.",
            )
            continue
        if (
            event.owner is None
            and event.event in OBJECT_FORM_EVENTS
            and main_object is None
        ):
            reader.issue(
                "object_event_requires_main_object",
                f"{path}.event",
                "Событие жизненного цикла требует главный реквизит metadata_object.",
            )
            continue
        event_signatures.setdefault(event.handler.casefold(), set()).add(
            (signature.directive.casefold(), signature.parameters)
        )

    for index, command in enumerate(commands):
        signatures = event_signatures.setdefault(command.action.casefold(), set())
        signatures.add(("наклиенте", ("Команда",)))
        if len(signatures) > 1:
            reader.issue(
                "handler_signature_conflict",
                f"$.commands[{index}].action",
                "Один обработчик нельзя сгенерировать с сигнатурами события и команды.",
            )
    for index, event in enumerate(events):
        if len(event_signatures.get(event.handler.casefold(), ())) > 1:
            reader.issue(
                "handler_signature_conflict",
                f"$.events[{index}].handler",
                "Один обработчик нельзя сгенерировать с разными сигнатурами событий.",
            )
    _duplicates(
        reader,
        [
            ((item.owner, item.event), f"$.events[{index}].event")
            for index, item in enumerate(events)
        ],
        code="duplicate_event",
        message="Привязка события к владельцу повторяется.",
    )

    if reader.diagnostics:
        raise FormsContractError(tuple(reader.diagnostics))
    owner_order = {None: 0}
    owner_order.update(
        {element.name: index for index, (element, _path, _table) in enumerate(walked, 1)}
    )
    events = tuple(
        event
        for _index, event in sorted(
            enumerate(events), key=lambda item: (owner_order[item[1].owner], item[0])
        )
    )
    return ManagedForm(
        1,
        form_name,
        "2.16",
        platform_version,
        event_profile,
        title,
        attributes,
        elements,
        commands,
        events,
    )


def _localized_spec(value: LocalizedText) -> dict[str, str]:
    return {"ru": value.ru}


def _type_to_spec(value: AttributeType) -> dict[str, object]:
    if isinstance(value, StringType):
        return {"kind": "string", "length": value.length}
    if isinstance(value, BooleanType):
        return {"kind": "boolean"}
    if isinstance(value, NumberType):
        return {
            "kind": "number",
            "digits": value.digits,
            "fraction_digits": value.fraction_digits,
            "allowed_sign": value.allowed_sign,
        }
    if isinstance(value, DateType):
        return {"kind": "date", "fractions": value.fractions}
    if isinstance(value, MetadataReferenceType):
        return {"kind": "metadata_reference", "object": value.object}
    if isinstance(value, CompositeType):
        return {
            "kind": "composite",
            "variants": [_type_to_spec(variant) for variant in value.variants],
        }
    if isinstance(value, MetadataObjectType):
        return {"kind": "metadata_object", "object": value.object}
    if isinstance(value, DynamicListType):
        return {
            "kind": "dynamic_list",
            "main_table": value.main_table,
            "dynamic_data_read": value.dynamic_data_read,
        }
    return {
        "kind": "value_table",
        "columns": [
            {
                "name": column.name,
                "type": _type_to_spec(column.type),
                **(
                    {"title": _localized_spec(column.title)}
                    if column.title is not None
                    else {}
                ),
            }
            for column in value.columns
        ],
    }


def _element_to_spec(element: Element) -> dict[str, object]:
    if isinstance(element, InputField):
        item: dict[str, object] = {
            "kind": "input_field",
            "name": element.name,
            "data_path": element.data_path,
        }
        if element.multiline:
            item["multiline"] = True
        if element.read_only:
            item["read_only"] = True
        if element.list_choice_mode:
            item["list_choice_mode"] = True
        if element.choice_list:
            item["choice_list"] = [
                {
                    "value": choice.value,
                    "presentation": _localized_spec(
                        choice.presentation
                    ),
                }
                for choice in element.choice_list
            ]
        if element.horizontal_stretch is not None:
            item["horizontal_stretch"] = element.horizontal_stretch
        if element.vertical_stretch is not None:
            item["vertical_stretch"] = element.vertical_stretch
    elif isinstance(element, CheckBoxField):
        item = {
            "kind": "check_box_field",
            "name": element.name,
            "data_path": element.data_path,
        }
        if element.read_only:
            item["read_only"] = True
    elif isinstance(element, LabelDecoration):
        item = {
            "kind": "label_decoration",
            "name": element.name,
        }
        if element.hyperlink:
            item["hyperlink"] = True
        if element.horizontal_stretch is not None:
            item["horizontal_stretch"] = element.horizontal_stretch
        if element.vertical_stretch is not None:
            item["vertical_stretch"] = element.vertical_stretch
    elif isinstance(element, LabelField):
        item = {
            "kind": "label_field",
            "name": element.name,
            "data_path": element.data_path,
        }
        if element.hyperlink:
            item["hyperlink"] = True
        if element.read_only:
            item["read_only"] = True
        if element.horizontal_stretch is not None:
            item["horizontal_stretch"] = element.horizontal_stretch
        if element.vertical_stretch is not None:
            item["vertical_stretch"] = element.vertical_stretch
    elif isinstance(element, RadioButtonField):
        item = {
            "kind": "radio_button_field",
            "name": element.name,
            "data_path": element.data_path,
            "choice_list": [
                {
                    "value": choice.value,
                    "presentation": _localized_spec(choice.presentation),
                }
                for choice in element.choice_list
            ],
        }
        if element.radio_button_type != "auto":
            item["radio_button_type"] = element.radio_button_type
        if element.columns_count is not None:
            item["columns_count"] = element.columns_count
        if element.read_only:
            item["read_only"] = True
    elif isinstance(element, Button):
        item = {
            "kind": "button",
            "name": element.name,
            "command": element.command,
        }
        if element.command_kind != "custom":
            item["command_kind"] = element.command_kind
        if element.command_owner is not None:
            item["command_owner"] = element.command_owner
        if element.default:
            item["default"] = True
    elif isinstance(element, Table):
        item = {
            "kind": "table",
            "name": element.name,
            "data_path": element.data_path,
            "columns": [
                _element_to_spec(column) for column in element.columns
            ],
        }
        if element.read_only:
            item["read_only"] = True
        if element.horizontal_stretch is not None:
            item["horizontal_stretch"] = element.horizontal_stretch
        if element.vertical_stretch is not None:
            item["vertical_stretch"] = element.vertical_stretch
    elif isinstance(element, Pages):
        item = {
            "kind": "pages",
            "name": element.name,
            "title": _localized_spec(element.title),
            "representation": element.representation,
            "pages": [
                {
                    "name": page.name,
                    "title": _localized_spec(page.title),
                    "children": [
                        _element_to_spec(child)
                        for child in page.children
                    ],
                }
                for page in element.pages
            ],
        }
        if element.horizontal_stretch is not None:
            item["horizontal_stretch"] = element.horizontal_stretch
        if element.vertical_stretch is not None:
            item["vertical_stretch"] = element.vertical_stretch
    else:
        item = {
            "kind": "usual_group",
            "name": element.name,
            "title": _localized_spec(element.title),
            "children": [
                _element_to_spec(child) for child in element.children
            ],
        }
        if element.orientation != "vertical":
            item["orientation"] = element.orientation
        if element.representation != "normal_separation":
            item["representation"] = element.representation
        if not element.show_title:
            item["show_title"] = False
        if element.horizontal_stretch is not None:
            item["horizontal_stretch"] = element.horizontal_stretch
        if element.vertical_stretch is not None:
            item["vertical_stretch"] = element.vertical_stretch
    if (
        not isinstance(element, (Pages, UsualGroup))
        and element.title is not None
    ):
        item["title"] = _localized_spec(element.title)
    return item


def managed_form_to_spec(form: ManagedForm) -> dict[str, object]:
    """Вернуть каноническую публичную спецификацию без внутренних ID."""

    attributes: list[dict[str, object]] = []
    for attribute in form.attributes:
        item: dict[str, object] = {
            "name": attribute.name,
            "type": _type_to_spec(attribute.type),
        }
        if attribute.title is not None:
            item["title"] = _localized_spec(attribute.title)
        if attribute.main:
            item["main"] = True
        attributes.append(item)
    return {
        "schema_version": form.schema_version,
        "form_name": form.form_name,
        "format_version": form.format_version,
        **(
            {"platform_version": form.platform_version}
            if form.platform_version is not None
            else {}
        ),
        "title": _localized_spec(form.title),
        "attributes": attributes,
        "elements": [
            _element_to_spec(element) for element in form.elements
        ],
        "commands": [
            {
                "name": command.name,
                "title": _localized_spec(command.title),
                "action": command.action,
            }
            for command in form.commands
        ],
        "events": [
            {
                **({"owner": event.owner} if event.owner is not None else {}),
                "event": event.event,
                "handler": event.handler,
            }
            for event in form.events
        ],
    }


__all__ = [
    "BSL_RESERVED_KEYWORDS",
    "BooleanType",
    "BooleanTypeSpec",
    "Button",
    "ButtonSpec",
    "CheckBoxField",
    "CheckBoxFieldSpec",
    "CompositeType",
    "CompositeTypeSpec",
    "ChoiceListItem",
    "ChoiceListItemSpec",
    "DateType",
    "DateTypeSpec",
    "DynamicListType",
    "DynamicListTypeSpec",
    "Element",
    "ElementSpec",
    "FormAttribute",
    "FormAttributeSpec",
    "FormCommand",
    "FormCommandSpec",
    "FormEvent",
    "FormEventSpec",
    "FormsContractError",
    "GroupChild",
    "GroupChildSpec",
    "InputField",
    "InputFieldSpec",
    "LabelDecoration",
    "LabelDecorationSpec",
    "LabelField",
    "LabelFieldSpec",
    "RadioButtonField",
    "RadioButtonFieldSpec",
    "LocalizedText",
    "LocalizedTextSpec",
    "ManagedForm",
    "ManagedFormSpec",
    "MetadataReferenceType",
    "MetadataReferenceTypeSpec",
    "NumberType",
    "NumberTypeSpec",
    "Page",
    "PageSpec",
    "Pages",
    "PagesSpec",
    "SPECIFICATION_VERSION",
    "SUPPORTED_FORMAT_VERSION",
    "StringType",
    "StringTypeSpec",
    "Table",
    "TableSpec",
    "UsualGroup",
    "UsualGroupSpec",
    "ValueTableColumn",
    "ValueTableColumnSpec",
    "ValueTableType",
    "ValueTableTypeSpec",
    "ValueType",
    "ValueTypeSpec",
    "is_reserved_bsl_keyword",
    "managed_form_to_spec",
    "parse_managed_form_spec",
]
