"""Каноническая запись и восстановление базового структурного слоя."""

from __future__ import annotations

from dataclasses import fields, is_dataclass
from enum import Enum
from typing import Mapping

from .defined_type_contract import resolve_defined_type
from .intake_v2 import LayerSourceProfile
from .layer_validation import (
    GenerationRuntimeError,
    _boolean,
    _exact_keys,
    _mapping,
    _optional_int,
    _text,
    _text_list,
)
from .model import Configuration, Field, MetadataObject, TabularPart


def _order(value: str) -> tuple[str, str]:
    return value.casefold(), value


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


def _field_data(value: Field, *, include_balance: bool = False) -> dict[str, object]:
    result = {
        "name": value.name,
        "synonym": value.synonym,
        "comment": value.comment,
        "indexing": value.indexing,
        "types": sorted(value.types, key=_order),
        "string_length": value.string_length,
        "string_allowed_length": value.string_allowed_length,
        "digits": value.digits,
        "fraction_digits": value.fraction_digits,
        "date_parts": value.date_parts,
    }
    if include_balance:
        result["balance"] = value.balance
    return result


def base_layer_data(base: Configuration) -> dict[str, object]:
    """Вернуть канонические данные только базового структурного слоя.

    Читает слой `configuration_from_base_layer`; новое поле
    здесь без поля там отклонит каждое поколение. Согласованность закрепляет
    `tests/test_intake_v2_layer_roundtrip.py`.
    """
    if not isinstance(base, Configuration):
        raise TypeError("base должен быть Configuration")
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
                    (
                        _field_data(item)
                        for item in value.attributes
                        if not item.standard
                    ),
                    key=lambda item: _order(str(item["name"])),
                ),
                "dimensions": sorted(
                    (_field_data(item, include_balance=True) for item in value.dimensions),
                    key=lambda item: _order(str(item["name"])),
                ),
                "resources": sorted(
                    (_field_data(item, include_balance=True) for item in value.resources),
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
        "platform": base.platform,
        "compatibility_mode": base.compatibility_mode,
        "exported_at": base.exported_at,
        "exporter_version": base.exporter_version,
        "schema_version": base.schema_version,
        "source_format": base.source_format,
        "truncated": base.truncated,
        "predefined_available": base.predefined_available,
        "warnings": list(base.warnings),
        "objects": objects,
    }


_FIELD_KEYS = {
    "name",
    "synonym",
    "comment",
    "indexing",
    "types",
    "string_length",
    "digits",
    "fraction_digits",
    "date_parts",
    "string_allowed_length",
}
_BALANCE_FIELD_KEYS = _FIELD_KEYS | {"balance"}


def _field(value: object, label: str) -> Field:
    raw = _mapping(value, label)
    if frozenset(raw) not in {
        frozenset(_FIELD_KEYS),
        frozenset(_BALANCE_FIELD_KEYS),
    }:
        raise GenerationRuntimeError(f"{label} содержит неверный набор полей")
    return Field(
        name=_text(raw["name"], f"{label}.name", required=True),
        synonym=_text(raw["synonym"], f"{label}.synonym"),
        comment=_text(raw["comment"], f"{label}.comment"),
        indexing=_text(raw["indexing"], f"{label}.indexing"),
        types=_text_list(raw["types"], f"{label}.types"),
        string_length=_optional_int(raw["string_length"], f"{label}.string_length"),
        string_allowed_length=_text(
            raw["string_allowed_length"],
            f"{label}.string_allowed_length",
        ),
        digits=_optional_int(raw["digits"], f"{label}.digits"),
        fraction_digits=_optional_int(
            raw["fraction_digits"], f"{label}.fraction_digits"
        ),
        date_parts=_text(raw["date_parts"], f"{label}.date_parts"),
        balance=(
            _boolean(raw["balance"], f"{label}.balance")
            if raw.get("balance") is not None
            else None
        ),
    )


def _fields(value: object, label: str) -> list[Field]:
    if not isinstance(value, list):
        raise GenerationRuntimeError(f"{label} должен быть массивом")
    result = [_field(item, f"{label}[{index}]") for index, item in enumerate(value)]
    names = [item.name.casefold() for item in result]
    if len(names) != len(set(names)):
        raise GenerationRuntimeError(f"{label} дублирует имя поля")
    return result


def _tabular_parts(value: object, label: str) -> list[TabularPart]:
    if not isinstance(value, list):
        raise GenerationRuntimeError(f"{label} должен быть массивом")
    result: list[TabularPart] = []
    for index, item in enumerate(value):
        item_label = f"{label}[{index}]"
        raw = _mapping(item, item_label)
        _exact_keys(raw, {"name", "synonym", "attributes"}, item_label)
        result.append(
            TabularPart(
                name=_text(raw["name"], f"{item_label}.name", required=True),
                synonym=_text(raw["synonym"], f"{item_label}.synonym"),
                attributes=_fields(
                    raw["attributes"], f"{item_label}.attributes"
                ),
            )
        )
    names = [item.name.casefold() for item in result]
    if len(names) != len(set(names)):
        raise GenerationRuntimeError(f"{label} дублирует табличную часть")
    return result


_OBJECT_KEYS = {
    "full_name",
    "kind",
    "name",
    "synonym",
    "comment",
    "props",
    "attributes",
    "dimensions",
    "resources",
    "tabular_parts",
    "movements",
    "based_on",
    "owners",
    "predefined",
    "enum_values",
    "value_type",
}


def _metadata_object(value: object, label: str) -> MetadataObject:
    raw = _mapping(value, label)
    _exact_keys(raw, _OBJECT_KEYS, label)
    props = _mapping(raw["props"], f"{label}.props")
    enum_raw = raw["enum_values"]
    if not isinstance(enum_raw, list):
        raise GenerationRuntimeError(f"{label}.enum_values должен быть массивом")
    enum_values: list[tuple[str, str]] = []
    for index, item in enumerate(enum_raw):
        if (
            not isinstance(item, list)
            or len(item) != 2
            or not all(isinstance(part, str) for part in item)
        ):
            raise GenerationRuntimeError(
                f"{label}.enum_values[{index}] должен быть парой строк"
            )
        enum_values.append((item[0], item[1]))
    kind = _text(raw["kind"], f"{label}.kind", required=True)
    name = _text(raw["name"], f"{label}.name", required=True)
    full_name = _text(raw["full_name"], f"{label}.full_name", required=True)
    if full_name != f"{kind}.{name}":
        raise GenerationRuntimeError(f"{label}.full_name не совпадает с kind/name")
    value_type = raw["value_type"]
    return MetadataObject(
        full_name=full_name,
        kind=kind,
        name=name,
        synonym=_text(raw["synonym"], f"{label}.synonym"),
        comment=_text(raw["comment"], f"{label}.comment"),
        props=dict(props),
        attributes=_fields(raw["attributes"], f"{label}.attributes"),
        dimensions=_fields(raw["dimensions"], f"{label}.dimensions"),
        resources=_fields(raw["resources"], f"{label}.resources"),
        tabular_parts=_tabular_parts(
            raw["tabular_parts"], f"{label}.tabular_parts"
        ),
        movements=_text_list(raw["movements"], f"{label}.movements"),
        based_on=_text_list(raw["based_on"], f"{label}.based_on"),
        owners=_text_list(raw["owners"], f"{label}.owners"),
        predefined=_text_list(raw["predefined"], f"{label}.predefined"),
        enum_values=enum_values,
        value_type=(
            None
            if value_type is None
            else _field(value_type, f"{label}.value_type")
        ),
    )


def configuration_from_base_layer(
    semantic: object, *, source_profile: LayerSourceProfile | None = None,
) -> Configuration:
    """Восстановить формат-независимую модель из canonical base layer.

    Принимается полный современный формат, который пишет
    `base_layer_data`; согласованность закрепляет
    `tests/test_intake_v2_layer_roundtrip.py`. Профиль Source B очищает
    недоказанную фактическую версию платформы, сохраняя режим совместимости
    и сведения о предопределённых элементах.
    """
    raw = _mapping(semantic, "base_structure")
    current_keys = {
        "name",
        "synonym",
        "version",
        "vendor",
        "schema_version",
        "objects",
        "platform",
        "exported_at",
        "exporter_version",
        "source_format",
        "truncated",
        "predefined_available",
        "warnings",
        "compatibility_mode",
    }
    _exact_keys(raw, current_keys, "base_structure")
    objects_raw = raw["objects"]
    if not isinstance(objects_raw, list):
        raise GenerationRuntimeError("base_structure.objects должен быть массивом")
    objects: dict[str, MetadataObject] = {}
    casefold_names: set[str] = set()
    for index, value in enumerate(objects_raw):
        obj = _metadata_object(value, f"base_structure.objects[{index}]")
        folded = obj.full_name.casefold()
        if folded in casefold_names:
            raise GenerationRuntimeError("base_structure дублирует объект")
        casefold_names.add(folded)
        objects[obj.full_name] = obj
    source_format = _text(
        raw["source_format"], "base_structure.source_format"
    )
    platform = _text(raw["platform"], "base_structure.platform")
    compatibility = _text(
        raw["compatibility_mode"], "base_structure.compatibility_mode"
    )
    predefined = _boolean(
        raw["predefined_available"], "base_structure.predefined_available"
    )
    is_source_b = (
        source_profile is LayerSourceProfile.SOURCE_B
        or (source_profile is None and source_format == "source-b")
    )
    if is_source_b:
        # Выгрузка содержит режим совместимости, а не фактическую платформу.
        platform = ""
    configuration = Configuration(
        name=_text(raw["name"], "base_structure.name", required=True),
        synonym=_text(raw["synonym"], "base_structure.synonym"),
        version=_text(raw["version"], "base_structure.version"),
        vendor=_text(raw["vendor"], "base_structure.vendor"),
        platform=platform,
        compatibility_mode=compatibility,
        exported_at=_text(
            raw["exported_at"], "base_structure.exported_at"
        ),
        exporter_version=_text(
            raw["exporter_version"], "base_structure.exporter_version"
        ),
        schema_version=_text(
            raw["schema_version"], "base_structure.schema_version", required=True
        ),
        source_format=source_format,
        truncated=_boolean(
            raw["truncated"], "base_structure.truncated"
        ),
        predefined_available=predefined,
        warnings=_text_list(
            raw["warnings"], "base_structure.warnings"
        ),
        objects=objects,
    )
    _classify_defined_type_references(configuration)
    return configuration


def _classify_defined_type_references(configuration: Configuration) -> None:
    """Аннотировать ссылки на DefinedType после загрузки всего набора объектов."""
    definitions = {
        obj.name: tuple(obj.value_type.types)
        for obj in configuration.objects.values()
        if obj.kind == "ОпределяемыйТип" and obj.value_type is not None
    }
    for obj in configuration.objects.values():
        for _, field in obj.all_fields():
            for reference in field.types:
                if not (reference.startswith("cfg:") or reference.startswith("ОпределяемыйТип.")):
                    continue
                resolution = resolve_defined_type(reference, definitions)
                field.defined_type_state = resolution.state.value
                field.defined_type_reference = reference
                field.defined_type_members = resolution.members
                break


__all__ = ["base_layer_data", "configuration_from_base_layer"]
