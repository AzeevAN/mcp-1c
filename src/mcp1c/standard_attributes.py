"""Единая runtime-проекция платформенных реквизитов 1С.

Source A и Source B сохраняют объявленные пользователем реквизиты и свойства
объекта. Системные поля выводятся платформой из этих свойств, поэтому здесь
они материализуются только в resolved-модели и не попадают обратно в слои.
"""

from __future__ import annotations

from collections.abc import Mapping

from .model import Configuration, Field, MetadataObject


class StandardAttributeError(ValueError):
    """Платформенные реквизиты нельзя вывести без противоречия."""


_JOURNAL_NAMES = {
    "Type": "Тип",
    "Ref": "Ссылка",
    "Number": "Номер",
    "Date": "Дата",
    "Posted": "Проведен",
    "DeletionMark": "ПометкаУдаления",
}


def _positive_int(value: object) -> int | None:
    return value if type(value) is int and value > 0 else None


def _allowed_length(value: object) -> str:
    if not isinstance(value, str):
        return ""
    folded = value.casefold()
    if folded in {"fixed", "фиксированная"}:
        return "Fixed"
    if folded in {"variable", "переменная"}:
        return "Variable"
    return ""


def _value_type(value: object) -> str:
    if not isinstance(value, str):
        return ""
    folded = value.casefold()
    if folded in {"string", "строка"}:
        return "Строка"
    if folded in {"number", "число"}:
        return "Число"
    return ""


def _reference(name: str, type_name: str) -> Field:
    return Field(name=name, types=[type_name], standard=True)


def _boolean(name: str) -> Field:
    return Field(name=name, types=["Булево"], standard=True)


def _platform_string(name: str) -> Field:
    return Field(
        name=name,
        types=["Строка"],
        string_length_known=False,
        standard=True,
    )


def _number_field(obj: MetadataObject) -> Field | None:
    length = _positive_int(obj.props.get("number_length"))
    numerator = obj.props.get("numerator")
    if numerator and obj.props.get("number_rules_resolved") is not True:
        # Старые generation/source A знают имя нумератора, но не доказывают,
        # что локальные свойства документа совпадают с его правилами. Поле
        # оставляем видимым, однако не придумываем тип и квалификаторы.
        return Field(name="Номер", standard=True) if length else None
    if length is None:
        return None
    type_name = _value_type(obj.props.get("number_type"))
    if type_name == "Строка":
        return Field(
            name="Номер",
            types=[type_name],
            string_length=length,
            string_allowed_length=_allowed_length(
                obj.props.get("number_allowed_length")
            ),
            standard=True,
        )
    if type_name == "Число":
        return Field(
            name="Номер",
            types=[type_name],
            digits=length,
            fraction_digits=0,
            standard=True,
        )
    return Field(name="Номер", standard=True)


def _document_fields(obj: MetadataObject) -> list[Field]:
    result = [_reference("Ссылка", obj.full_name)]
    number = _number_field(obj)
    if number is not None:
        result.append(number)
    result.extend(
        (
            Field(
                name="Дата",
                types=["Дата"],
                date_parts="DateTime",
                standard=True,
            ),
            _boolean("Проведен"),
            _boolean("ПометкаУдаления"),
        )
    )
    return result


def _catalog_fields(obj: MetadataObject) -> list[Field]:
    result = [_reference("Ссылка", obj.full_name)]
    code_length = _positive_int(obj.props.get("code_length"))
    if code_length is not None:
        code_type = _value_type(obj.props.get("code_type"))
        if code_type == "Строка":
            result.append(
                Field(
                    name="Код",
                    types=[code_type],
                    string_length=code_length,
                    string_allowed_length=_allowed_length(
                        obj.props.get("code_allowed_length")
                    ),
                    standard=True,
                )
            )
        elif code_type == "Число":
            result.append(
                Field(
                    name="Код",
                    types=[code_type],
                    digits=code_length,
                    fraction_digits=0,
                    standard=True,
                )
            )
        else:
            result.append(Field(name="Код", standard=True))
    description_length = _positive_int(obj.props.get("description_length"))
    if description_length is not None:
        result.append(
            Field(
                name="Наименование",
                types=["Строка"],
                string_length=description_length,
                string_allowed_length="Variable",
                standard=True,
            )
        )
    if obj.owners:
        result.append(Field(name="Владелец", types=list(obj.owners), standard=True))
    if obj.props.get("hierarchical") is True:
        result.extend(
            (
                _reference("Родитель", obj.full_name),
                _boolean("ЭтоГруппа"),
            )
        )
    result.extend(
        (
            _boolean("ПометкаУдаления"),
            _boolean("Предопределенный"),
            _platform_string("ИмяПредопределенныхДанных"),
        )
    )
    return result


def _standard_lookup(obj: MetadataObject) -> dict[str, Field]:
    return {item.name.casefold(): item for item in obj.attributes if item.standard}


def _journal_number(
    configuration: Configuration,
    registered_documents: list[str],
) -> Field:
    numbers: list[Field] = []
    complete = True
    for name in registered_documents:
        document = configuration.get(name)
        if document is None or document.kind != "Документ":
            complete = False
            continue
        number = _standard_lookup(document).get("номер")
        if number is not None:
            numbers.append(number)
    if not complete or any(not item.types for item in numbers):
        return Field(name="Номер", standard=True)

    types: list[str] = []
    for item in numbers:
        for type_name in item.types:
            if type_name not in types:
                types.append(type_name)
    string_fields = [item for item in numbers if "Строка" in item.types]
    number_fields = [item for item in numbers if "Число" in item.types]
    string_lengths = [
        item.string_length for item in string_fields if item.string_length is not None
    ]
    allowed = [_allowed_length(item.string_allowed_length) for item in string_fields]
    if string_fields and all(value == "Fixed" for value in allowed):
        string_allowed_length = "Fixed"
    elif string_fields and all(value for value in allowed):
        string_allowed_length = "Variable"
    else:
        string_allowed_length = ""
    digits = [item.digits for item in number_fields if item.digits is not None]
    return Field(
        name="Номер",
        types=types,
        string_length=max(string_lengths) if string_lengths else None,
        string_allowed_length=string_allowed_length,
        digits=max(digits) if digits else None,
        fraction_digits=0 if digits else None,
        standard=True,
    )


def _journal_fields(
    configuration: Configuration,
    obj: MetadataObject,
) -> list[Field]:
    raw_standard = obj.extended.get("standard_attributes", [])
    registered = obj.extended.get("registered_documents", [])
    if not isinstance(raw_standard, list) or not all(
        isinstance(item, Mapping) for item in raw_standard
    ):
        raise StandardAttributeError(
            f"{obj.full_name}: standard_attributes должны быть массивом объектов"
        )
    if not isinstance(registered, list) or not all(
        isinstance(item, str) for item in registered
    ):
        raise StandardAttributeError(
            f"{obj.full_name}: registered_documents должны быть массивом строк"
        )
    result: list[Field] = []
    for raw in raw_standard:
        native_name = raw.get("name")
        if not isinstance(native_name, str) or native_name not in _JOURNAL_NAMES:
            raise StandardAttributeError(
                f"{obj.full_name}: неизвестный системный реквизит журнала "
                f"{native_name!r}"
            )
        name = _JOURNAL_NAMES[native_name]
        if native_name == "Type":
            field = _platform_string(name)
        elif native_name == "Ref":
            field = Field(name=name, types=list(registered), standard=True)
        elif native_name == "Number":
            field = _journal_number(configuration, list(registered))
        elif native_name == "Date":
            field = Field(
                name=name,
                types=["Дата"],
                date_parts="DateTime",
                standard=True,
            )
        else:
            field = _boolean(name)
        synonym = raw.get("synonym", "")
        comment = raw.get("comment", "")
        field.synonym = synonym if isinstance(synonym, str) else ""
        field.comment = comment if isinstance(comment, str) else ""
        result.append(field)
    return result


def _replace_standard(obj: MetadataObject, standard: list[Field]) -> None:
    expected = {item.name.casefold(): item.name for item in standard}
    ordinary: list[Field] = []
    seen: set[str] = set()
    for item in obj.attributes:
        folded = item.name.casefold()
        if item.standard:
            continue
        if folded in expected:
            raise StandardAttributeError(
                f"{obj.full_name}: обычный реквизит `{item.name}` конфликтует "
                f"с системным `{expected[folded]}`"
            )
        if folded in seen:
            raise StandardAttributeError(
                f"{obj.full_name}: реквизит `{item.name}` дублируется"
            )
        seen.add(folded)
        ordinary.append(item)
    obj.attributes = [*standard, *ordinary]


def materialize_standard_attributes(configuration: Configuration) -> None:
    """Идемпотентно добавить системные поля в единую resolved-модель."""
    if not isinstance(configuration, Configuration):
        raise TypeError("configuration должна быть Configuration")
    for obj in configuration.objects.values():
        if obj.kind == "Документ":
            _replace_standard(obj, _document_fields(obj))
        elif obj.kind == "Справочник":
            _replace_standard(obj, _catalog_fields(obj))
    # Номер журнала выводится из уже материализованных документов.
    for obj in configuration.objects.values():
        if obj.kind == "ЖурналДокументов":
            _replace_standard(obj, _journal_fields(configuration, obj))


__all__ = [
    "StandardAttributeError",
    "materialize_standard_attributes",
]
