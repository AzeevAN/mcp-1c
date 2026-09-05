"""Проверяемая runtime-проекция неизменяемого generation bundle.

Модуль ничего не публикует и не знает о ``Registry``. Он преобразует
канонические слои в существующую модель конфигурации и единый каталог
локаторов кода/форм. Тяжёлые индексы можно построить до атомарного swap.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping

from . import index_cache
from .intake_v2 import (
    GenerationManifest,
    LayerKind,
    LayerManifest,
    LayerSourceProfile,
    LayerState,
    SourceKind,
)
from .intake_v2_extensions import ExtensionResolutionError, ExtensionStructure
from .intake_v2_registry import (
    BundleStoreError,
    LayerMember,
    LayerPayload,
    load_layer_payload,
)
from .model import (
    Configuration,
    Field,
    MetadataObject,
    ObjectRelation,
    TabularPart,
)
from .module_catalog import (
    CandidateOutcome,
    CatalogCoverage,
    CatalogEntry,
    CatalogProblem,
    FormSource,
    ModuleCatalog,
)
from .module_content import (
    ContentReadError,
    LocatorIdentity,
    ModuleLocator,
    read_bsl,
)
from .role_access import LoadedRoleAccess, load_role_access
from .standard_attributes import (
    StandardAttributeError,
    materialize_standard_attributes,
)


class GenerationRuntimeError(ValueError):
    """Слои поколения нельзя безопасно представить действующему runtime."""


@dataclass(frozen=True, slots=True)
class NativeGenerationRuntime:
    configuration: Configuration
    base_sha256: str
    structure_sha256: str
    catalog: ModuleCatalog | None
    code_sha256: str
    code_items_total: int
    locator_generation: int
    roles: LoadedRoleAccess | None
    extension_structure: ExtensionStructure | None = None


def _mapping(value: object, label: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping) or not all(
        isinstance(key, str) for key in value
    ):
        raise GenerationRuntimeError(f"{label} должен быть объектом")
    return value


def _exact_keys(
    value: Mapping[str, object], expected: set[str], label: str
) -> None:
    if set(value) != expected:
        raise GenerationRuntimeError(f"{label} содержит неверный набор полей")


def _text(value: object, label: str, *, required: bool = False) -> str:
    if not isinstance(value, str) or (required and not value):
        raise GenerationRuntimeError(f"{label} должен быть строкой")
    return value


def _optional_int(value: object, label: str) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise GenerationRuntimeError(
            f"{label} должен быть целым числом не меньше нуля или null"
        )
    return value


def _boolean(value: object, label: str) -> bool:
    if type(value) is not bool:
        raise GenerationRuntimeError(f"{label} должен быть bool")
    return value


def _text_list(value: object, label: str) -> list[str]:
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise GenerationRuntimeError(f"{label} должен быть массивом строк")
    return list(value)


_LEGACY_FIELD_KEYS = {
    "name",
    "synonym",
    "comment",
    "indexing",
    "types",
    "string_length",
    "digits",
    "fraction_digits",
    "date_parts",
}
_FIELD_KEYS = _LEGACY_FIELD_KEYS | {"string_allowed_length"}


def _field(value: object, label: str) -> Field:
    raw = _mapping(value, label)
    if frozenset(raw) not in {
        frozenset(_LEGACY_FIELD_KEYS),
        frozenset(_FIELD_KEYS),
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
            raw.get("string_allowed_length", ""),
            f"{label}.string_allowed_length",
        ),
        digits=_optional_int(raw["digits"], f"{label}.digits"),
        fraction_digits=_optional_int(
            raw["fraction_digits"], f"{label}.fraction_digits"
        ),
        date_parts=_text(raw["date_parts"], f"{label}.date_parts"),
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
    """Восстановить формат-независимую модель из canonical base layer."""
    raw = _mapping(semantic, "base_structure")
    legacy_keys = {
        "name",
        "synonym",
        "version",
        "vendor",
        "schema_version",
        "objects",
    }
    current_keys = legacy_keys | {
        "platform",
        "exported_at",
        "exporter_version",
        "source_format",
        "truncated",
        "predefined_available",
        "warnings",
    }
    if frozenset(raw) not in {
        frozenset(legacy_keys), frozenset(current_keys),
        frozenset(current_keys | {"compatibility_mode"}),
    }:
        raise GenerationRuntimeError(
            "base_structure содержит неверный набор полей"
        )
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
        raw.get("source_format", "source-b"), "base_structure.source_format"
    )
    platform = _text(raw.get("platform", ""), "base_structure.platform")
    compatibility = _text(
        raw.get("compatibility_mode", ""), "base_structure.compatibility_mode"
    )
    predefined = _boolean(
        raw.get("predefined_available", True), "base_structure.predefined_available"
    )
    is_source_b = (
        source_profile is LayerSourceProfile.SOURCE_B
        or (source_profile is None and source_format == "source-b")
    )
    if is_source_b:
        # До parser v4 B записывал CompatibilityMode в platform и наследовал
        # True для недоступных предопределённых. Исправляем только проекцию:
        # защищённые хешами payload/manifest на диске остаются неизменными.
        if "compatibility_mode" not in raw:
            compatibility = platform
        platform = ""
        predefined = False
    return Configuration(
        name=_text(raw["name"], "base_structure.name", required=True),
        synonym=_text(raw["synonym"], "base_structure.synonym"),
        version=_text(raw["version"], "base_structure.version"),
        vendor=_text(raw["vendor"], "base_structure.vendor"),
        platform=platform,
        compatibility_mode=compatibility,
        exported_at=_text(
            raw.get("exported_at", ""), "base_structure.exported_at"
        ),
        exporter_version=_text(
            raw.get("exporter_version", ""), "base_structure.exporter_version"
        ),
        schema_version=_text(
            raw["schema_version"], "base_structure.schema_version", required=True
        ),
        source_format=source_format,
        truncated=_boolean(
            raw.get("truncated", False), "base_structure.truncated"
        ),
        predefined_available=predefined,
        warnings=_text_list(
            raw.get("warnings", []), "base_structure.warnings"
        ),
        objects=objects,
    )


_EXTENDED_OBJECT_KEYS = {
    "full_name",
    "kind",
    "name",
    "synonym",
    "comment",
    "code_address",
    "base_object",
    "payload",
    "forms",
    "relations",
}

_TYPE_DESCRIPTION_KEYS = {
    "types",
    "string_length",
    "string_allowed_length",
    "digits",
    "fraction_digits",
    "number_allowed_sign",
    "date_parts",
}


def _json_value(value: object, label: str) -> object:
    """Скопировать только JSON-значение с текстовыми ключами."""
    if value is None or isinstance(value, (str, bool, int, float)):
        return value
    if isinstance(value, list):
        return [
            _json_value(item, f"{label}[{index}]")
            for index, item in enumerate(value)
        ]
    if isinstance(value, Mapping) and all(
        isinstance(key, str) for key in value
    ):
        return {
            key: _json_value(item, f"{label}.{key}")
            for key, item in value.items()
        }
    raise GenerationRuntimeError(f"{label} содержит не-JSON значение")


def _type_description_field(
    value: object,
    label: str,
    *,
    name: str = "ТипЗначения",
    synonym: str = "",
    comment: str = "",
    indexing: str = "",
) -> Field:
    raw = _mapping(value, label)
    _exact_keys(raw, _TYPE_DESCRIPTION_KEYS, label)
    return Field(
        name=name,
        synonym=synonym,
        comment=comment,
        indexing=indexing,
        types=_text_list(raw["types"], f"{label}.types"),
        string_length=_optional_int(
            raw["string_length"], f"{label}.string_length"
        ),
        digits=_optional_int(raw["digits"], f"{label}.digits"),
        fraction_digits=_optional_int(
            raw["fraction_digits"], f"{label}.fraction_digits"
        ),
        date_parts=_text(raw["date_parts"], f"{label}.date_parts"),
    )


def _extended_relations(value: object, label: str) -> list[ObjectRelation]:
    if not isinstance(value, list):
        raise GenerationRuntimeError(f"{label} должен быть массивом")
    result: list[ObjectRelation] = []
    for index, item in enumerate(value):
        item_label = f"{label}[{index}]"
        raw = _mapping(item, item_label)
        _exact_keys(raw, {"kind", "target", "state", "properties"}, item_label)
        properties_raw = raw["properties"]
        if not isinstance(properties_raw, list):
            raise GenerationRuntimeError(
                f"{item_label}.properties должен быть массивом"
            )
        properties: list[tuple[str, str]] = []
        for property_index, pair in enumerate(properties_raw):
            if (
                not isinstance(pair, list)
                or len(pair) != 2
                or not all(isinstance(part, str) for part in pair)
            ):
                raise GenerationRuntimeError(
                    f"{item_label}.properties[{property_index}] должен быть парой строк"
                )
            properties.append((pair[0], pair[1]))
        state = _text(raw["state"], f"{item_label}.state", required=True)
        if state not in {"resolved", "unresolved"}:
            raise GenerationRuntimeError(
                f"{item_label}.state должен быть resolved или unresolved"
            )
        result.append(
            ObjectRelation(
                kind=_text(raw["kind"], f"{item_label}.kind", required=True),
                target=_text(
                    raw["target"], f"{item_label}.target", required=True
                ),
                state=state,
                properties=tuple(properties),
            )
        )
    return result


def _journal_fields(
    payload: Mapping[str, object],
    fields_by_address: Mapping[str, Field],
    label: str,
) -> list[Field]:
    """Представить стандартные реквизиты и графы журнала одной карточкой."""
    result: list[Field] = []
    standard = payload.get("standard_attributes", [])
    columns = payload.get("columns", [])
    if not isinstance(standard, list) or not isinstance(columns, list):
        raise GenerationRuntimeError(
            f"{label}.standard_attributes/columns должны быть массивами"
        )
    # StandardAttributes определяет состав и подписи, но не содержит типов.
    # Единая проекция после сведения слоёв добавит русские query-имена и
    # выведет типы из документов. Здесь остаются только пользовательские графы.
    for index, item in enumerate(columns):
        item_label = f"{label}.columns[{index}]"
        raw = _mapping(item, item_label)
        references = raw.get("references", [])
        if not isinstance(references, list):
            raise GenerationRuntimeError(
                f"{item_label}.references должен быть массивом"
            )
        types: list[str] = []
        for reference_index, reference in enumerate(references):
            reference_label = f"{item_label}.references[{reference_index}]"
            reference_raw = _mapping(reference, reference_label)
            target = _text(
                reference_raw.get("target"),
                f"{reference_label}.target",
                required=True,
            )
            field = fields_by_address.get(target.casefold())
            if field is None:
                continue
            for type_name in field.types:
                if type_name not in types:
                    types.append(type_name)
        result.append(
            Field(
                name=_text(raw.get("name"), f"{item_label}.name", required=True),
                synonym=_text(raw.get("synonym", ""), f"{item_label}.synonym"),
                comment=_text(raw.get("comment", ""), f"{item_label}.comment"),
                indexing=_text(raw.get("indexing", ""), f"{item_label}.indexing"),
                types=types,
            )
        )
    folded = [item.name.casefold() for item in result]
    if len(folded) != len(set(folded)):
        raise GenerationRuntimeError(f"{label} дублирует поле журнала")
    return result


def _extended_props(
    kind: str,
    payload: Mapping[str, object],
) -> dict[str, object]:
    """Вывести полезные скалярные свойства, не дублируя вложенные коллекции."""
    result: dict[str, object] = {}
    for key, value in payload.items():
        if value in (None, "", []):
            continue
        if isinstance(value, (str, bool, int, float)):
            result[key] = value
        elif isinstance(value, list):
            if len(value) <= 20 and all(isinstance(item, str) for item in value):
                result[key] = value
            else:
                result[f"{key}_count"] = len(value)
    binding = payload.get("binding")
    if isinstance(binding, Mapping):
        raw = binding.get("raw")
        if isinstance(raw, str) and raw:
            result["handler" if kind == "ПодпискаНаСобытие" else "method"] = raw
    return result


def _compose_extended_structure(
    configuration: Configuration,
    semantic: object,
    *,
    require_base_overlays: bool,
) -> None:
    """Собрать единый каталог A/B без копирования base-object overlays."""
    raw = _mapping(semantic, "extended_structure")
    if set(raw) != {"extension_structure", "objects"}:
        raise GenerationRuntimeError(
            "extended_structure содержит неверный набор полей"
        )
    objects = raw["objects"]
    if not isinstance(objects, list):
        raise GenerationRuntimeError(
            "extended_structure.objects должен быть массивом"
        )
    folded = {name.casefold(): name for name in configuration.objects}
    fields_by_address = {
        f"{obj.full_name}.{path}".casefold(): field
        for obj in configuration.objects.values()
        for path, field in obj.all_fields()
    }
    seen: set[str] = set()
    for index, value in enumerate(objects):
        label = f"extended_structure.objects[{index}]"
        item = _mapping(value, label)
        _exact_keys(item, _EXTENDED_OBJECT_KEYS, label)
        kind = _text(item["kind"], f"{label}.kind", required=True)
        name = _text(item["name"], f"{label}.name", required=True)
        full_name = _text(
            item["full_name"], f"{label}.full_name", required=True
        )
        if full_name != f"{kind}.{name}":
            raise GenerationRuntimeError(
                f"{label}.full_name не совпадает с kind/name"
            )
        key = full_name.casefold()
        if key in seen:
            raise GenerationRuntimeError("extended_structure дублирует объект")
        seen.add(key)
        base_object = item["base_object"]
        if type(base_object) is not bool:
            raise GenerationRuntimeError(f"{label}.base_object должен быть bool")
        payload_value = item["payload"]
        if payload_value is None:
            payload: dict[str, object] = {}
        else:
            payload_raw = _mapping(payload_value, f"{label}.payload")
            copied_payload = _json_value(payload_raw, f"{label}.payload")
            assert isinstance(copied_payload, dict)
            payload = copied_payload
        forms = _text_list(item["forms"], f"{label}.forms")
        relations = _extended_relations(
            item["relations"], f"{label}.relations"
        )
        canonical = folded.get(key)
        if base_object:
            if canonical is None:
                if require_base_overlays:
                    raise GenerationRuntimeError(
                        "несинхронная пара Source A/Source B: в base_structure "
                        f"отсутствует объект {full_name}, для которого Source B "
                        "хранит extended overlay"
                    )
                # В одной полной Source B выгрузке может остаться модуль или
                # форма уже удалённого metadata-объекта. Это честный orphan
                # content, а не самостоятельный объект resolved-view.
                continue
            target = configuration.objects[canonical]
            if target.kind != kind:
                raise GenerationRuntimeError(
                    "несинхронная пара Source A/Source B: вид extended overlay "
                    f"не совпадает с base object {full_name}"
                )
        else:
            if canonical is not None:
                raise GenerationRuntimeError(
                    f"{label}: extended-only object конфликтует с base_structure"
                )
            target = MetadataObject(
                full_name=full_name,
                kind=kind,
                name=name,
                synonym=_text(item["synonym"], f"{label}.synonym"),
                comment=_text(item["comment"], f"{label}.comment"),
            )
            configuration.objects[full_name] = target
            folded[key] = full_name
        target.code_address = _text(
            item["code_address"], f"{label}.code_address"
        )
        target.forms = forms
        target.extended = payload
        target.relations = relations
        for prop, prop_value in _extended_props(kind, payload).items():
            target.props.setdefault(prop, prop_value)
        value_type = payload.get("value_type")
        if value_type is not None and target.value_type is None:
            value_type_raw = _mapping(
                value_type,
                f"{label}.payload.value_type",
            )
            target.value_type = _type_description_field(
                value_type_raw, f"{label}.payload.value_type"
            )
            for source_key, target_key in (
                (
                    "string_allowed_length",
                    "value_type_string_allowed_length",
                ),
                ("number_allowed_sign", "value_type_number_allowed_sign"),
            ):
                qualifier = _text(
                    value_type_raw[source_key],
                    f"{label}.payload.value_type.{source_key}",
                )
                if qualifier:
                    target.props.setdefault(target_key, qualifier)
        if kind == "ЖурналДокументов":
            target.attributes = _journal_fields(
                payload, fields_by_address, f"{label}.payload"
            )


def _structure_sha256(
    layers: Mapping[LayerKind, LayerManifest],
) -> str:
    digest = hashlib.sha256(b"mcp1c-resolved-structure-v1\0")
    for kind in (LayerKind.BASE_STRUCTURE, LayerKind.EXTENDED_STRUCTURE):
        layer = layers.get(kind)
        digest.update(kind.value.encode("ascii") + b"\0")
        if layer is None:
            digest.update(b"missing\0")
            continue
        digest.update(layer.state.value.encode("ascii") + b"\0")
        digest.update(layer.content_sha256.encode("ascii") + b"\0")
    return digest.hexdigest()


def _ready_payload(
    root: Path,
    layers: Mapping[LayerKind, LayerManifest],
    kind: LayerKind,
    *,
    required: bool,
) -> LayerPayload | None:
    layer = layers.get(kind)
    if layer is None or layer.state is not LayerState.READY:
        if required:
            raise GenerationRuntimeError(f"{kind.value}: обязательный слой не готов")
        return None
    if not layer.payload_sha256:
        raise GenerationRuntimeError(
            f"{kind.value}: runtime требует versioned layer envelope"
        )
    try:
        payload = load_layer_payload(root / layer.relative_path)
    except (OSError, ValueError, BundleStoreError) as error:
        raise GenerationRuntimeError(f"{kind.value}: слой не прочитан") from error
    if payload.kind is not kind:
        raise GenerationRuntimeError(f"{kind.value}: envelope другого слоя")
    return payload


def _code_members(
    payload: LayerPayload | None,
) -> dict[str, tuple[LayerMember | None, str]]:
    if payload is None:
        return {}
    modules = payload.semantic.get("modules")
    if not isinstance(modules, list):
        raise GenerationRuntimeError("code.modules должен быть массивом")
    members = {member.key: member for member in payload.members}
    opaque_raw = payload.semantic.get("opaque_modules", [])
    if not isinstance(opaque_raw, list) or not all(
        isinstance(value, str) and value for value in opaque_raw
    ):
        raise GenerationRuntimeError("code.opaque_modules должен быть массивом строк")
    if len(set(opaque_raw)) != len(opaque_raw):
        raise GenerationRuntimeError("code.opaque_modules содержит дубликаты")
    declared: dict[str, tuple[int, str, bool]] = {}
    for index, value in enumerate(modules):
        raw = _mapping(value, f"code.modules[{index}]")
        _exact_keys(
            raw,
            {"address", "size", "sha256", "compiled"},
            f"code.modules[{index}]",
        )
        address = _text(raw["address"], f"code.modules[{index}].address", required=True)
        size = raw["size"]
        digest = raw["sha256"]
        compiled = raw["compiled"]
        if isinstance(size, bool) or not isinstance(size, int) or size < 0:
            raise GenerationRuntimeError("code module size некорректен")
        if not isinstance(digest, str):
            raise GenerationRuntimeError("code module sha256 некорректен")
        if type(compiled) is not bool:
            raise GenerationRuntimeError("code module compiled некорректен")
        if address in declared:
            raise GenerationRuntimeError("code дублирует адрес модуля")
        declared[address] = (size, digest, compiled)
    if set(declared) != set(members):
        raise GenerationRuntimeError("code semantic и members расходятся")
    if set(opaque_raw) & set(declared):
        raise GenerationRuntimeError("code одновременно содержит тело и opaque-модуль")
    result: dict[str, tuple[LayerMember | None, str]] = {}
    for address, member in members.items():
        size, digest, compiled = declared[address]
        if (size, digest) != (member.size, member.sha256):
            raise GenerationRuntimeError("code semantic и member identity расходятся")
        expected_suffix = ".Module" if compiled else ".bsl"
        if not member.relative_path.endswith(expected_suffix):
            raise GenerationRuntimeError("code member не совпадает с compiled")
        result[address] = (member, "compiled" if compiled else "source")
    for address in opaque_raw:
        result[address] = (None, "opaque")
    return result


def _form_kind(source_path: str) -> tuple[str, bool]:
    if source_path.endswith("/Ext/Form.xml"):
        return "form_xml", False
    if source_path.endswith("/Ext/Form.bin"):
        return "form_bin", True
    if source_path.endswith(".Form.xml"):
        return "form_xml", False
    if source_path.endswith(".Form"):
        return "container", True
    if source_path.endswith(".xml"):
        return "descriptor", False
    raise GenerationRuntimeError("forms member имеет неизвестную раскладку")


def _form_members(
    payload: LayerPayload | None,
) -> dict[str, tuple[FormSource, ...]]:
    if payload is None:
        return {}
    semantic_forms = payload.semantic.get("forms")
    if not isinstance(semantic_forms, list):
        raise GenerationRuntimeError("forms.forms должен быть массивом")
    declared: set[str] = set()
    for index, value in enumerate(semantic_forms):
        raw = _mapping(value, f"forms.forms[{index}]")
        address = _text(
            raw.get("address"), f"forms.forms[{index}].address", required=True
        )
        if address in declared:
            raise GenerationRuntimeError("forms дублирует адрес формы")
        declared.add(address)
    grouped: dict[str, list[FormSource]] = {}
    for member in payload.members:
        address, separator, source_path = member.key.partition("|")
        if not separator or not address or not source_path:
            raise GenerationRuntimeError("forms member не хранит исходный путь")
        kind, container = _form_kind(source_path)
        locator = (
            ModuleLocator.container(member.relative_path, "form")
            if container
            else ModuleLocator.file(member.relative_path)
        )
        grouped.setdefault(address, []).append(FormSource(kind, locator))
    if set(grouped) != declared:
        raise GenerationRuntimeError("forms semantic и members расходятся")
    return {
        address: tuple(
            sorted(sources, key=lambda item: (item.kind, item.locator.to_state()))
        )
        for address, sources in grouped.items()
    }


def _code_identity(
    layers: Mapping[LayerKind, LayerManifest],
) -> tuple[str, int]:
    digest = hashlib.sha256(b"mcp1c-native-code-runtime-v1\0")
    for kind in (LayerKind.CODE, LayerKind.FORMS):
        layer = layers.get(kind)
        if layer is None:
            digest.update(kind.value.encode("ascii") + b"\0missing\0")
            continue
        digest.update(kind.value.encode("ascii") + b"\0")
        digest.update(layer.state.value.encode("ascii") + b"\0")
        digest.update((layer.payload_sha256 or layer.content_sha256).encode("ascii"))
        digest.update(b"\0")
    value = digest.hexdigest()
    generation = int(value[:15], 16) or 1
    return value, generation


def _catalog(
    root: Path,
    code: LayerPayload | None,
    forms: LayerPayload | None,
    identity: LocatorIdentity,
) -> ModuleCatalog | None:
    code_members = _code_members(code)
    form_members = _form_members(forms)
    if code is None and forms is None:
        return None
    resolved_code: dict[str, tuple[LayerMember | None, str]] = {}
    for address, (member, state) in code_members.items():
        if state != "source":
            resolved_code[address] = member, state
            continue
        assert member is not None
        try:
            text = read_bsl(
                root,
                address,
                ModuleLocator.file(member.relative_path),
            )
        except ContentReadError:
            resolved_code[address] = member, "unreadable"
        else:
            resolved_code[address] = (
                member,
                "empty" if not text.strip() else "source",
            )
    folded: dict[str, str] = {}
    for address in (*code_members, *form_members):
        previous = folded.setdefault(address.casefold(), address)
        if previous != address:
            raise GenerationRuntimeError("адреса runtime различаются только регистром")
    entries: dict[str, CatalogEntry] = {}
    for address in sorted(
        set(code_members) | set(form_members),
        key=lambda value: (value.casefold(), value),
    ):
        code_member = resolved_code.get(address)
        member = code_member[0] if code_member is not None else None
        code_state = code_member[1] if code_member is not None else "missing"
        compiled = code_state in {"compiled", "opaque"}
        entries[address] = CatalogEntry(
            address=address,
            # Физический вид тела уже несут locator.kind и compiled. Здесь
            # важно сохранить происхождение: ordinal-путь generation нельзя
            # повторно адресовать как legacy flat/tree при чтении warm-кэша.
            module_kind="generation",
            locator=(
                (
                    ModuleLocator.compiled(member.relative_path)
                    if compiled
                    else ModuleLocator.file(member.relative_path)
                )
                if member is not None and code_state != "unreadable"
                else None
            ),
            is_form=address in form_members,
            compiled=compiled,
            form_sources=form_members.get(address, ()),
            diagnostics=(
                ("unreadable_body",)
                if code_state == "unreadable"
                else ()
            ),
            conflict=False,
            address_collision=False,
            sort_key=(address.casefold(), address),
            opaque=code_state == "opaque",
        )
    outcomes: list[CandidateOutcome] = []
    ordinal = 0
    compiled_total = 0
    empty_total = 0
    unreadable_total = 0
    problems: list[CatalogProblem] = []
    for address in sorted(resolved_code, key=lambda value: (value.casefold(), value)):
        ordinal += 1
        _member, code_state = resolved_code[address]
        if code_state in {"compiled", "opaque"}:
            category = "compiled"
        elif code_state == "unreadable":
            category = "unreadable_body"
        elif code_state == "empty":
            category = "empty"
        else:
            category = "indexed"
        compiled_total += category == "compiled"
        empty_total += category == "empty"
        unreadable_total += category == "unreadable_body"
        outcomes.append(CandidateOutcome(ordinal, category, address))
        if category == "unreadable_body":
            problems.append(
                CatalogProblem(
                    "unreadable_body",
                    address,
                    ordinal,
                    "тело модуля не прочитано",
                )
            )
    for address, sources in sorted(
        form_members.items(), key=lambda item: (item[0].casefold(), item[0])
    ):
        for _source in sources:
            ordinal += 1
            outcomes.append(CandidateOutcome(ordinal, "indexed", address))
    return ModuleCatalog(
        identity=identity,
        entries=entries,
        outcomes=tuple(outcomes),
        problems=tuple(problems),
        coverage=CatalogCoverage(
            total_candidates=ordinal,
            indexed=ordinal - compiled_total - empty_total - unreadable_total,
            empty=empty_total,
            compiled=compiled_total,
            unreadable_body=unreadable_total,
        ),
    )


def build_generation_runtime(
    root: str | Path,
    manifest: GenerationManifest,
    *,
    role_cache_path: str | Path | None = None,
) -> NativeGenerationRuntime:
    """Собрать чистую runtime-проекцию до публикации поколения."""
    if not isinstance(manifest, GenerationManifest):
        raise TypeError("manifest должен быть GenerationManifest")
    root = Path(root)
    layers = {layer.kind: layer for layer in manifest.layers}
    base_layer = layers.get(LayerKind.BASE_STRUCTURE)
    if base_layer is None or base_layer.state is not LayerState.READY:
        raise GenerationRuntimeError("base_structure: обязательный слой не готов")
    base = _ready_payload(
        root,
        layers,
        LayerKind.BASE_STRUCTURE,
        required=True,
    )
    assert base is not None
    configuration = configuration_from_base_layer(
        base.semantic,
        source_profile=base_layer.provenance.profile if base_layer.provenance else None,
    )
    if (
        base_layer.provenance is not None
        and base_layer.provenance.profile is LayerSourceProfile.SCHEMA_V1
    ):
        # Canonical base одинаков для синхронных Source A и Source B, поэтому
        # происхождение живёт в манифесте слоя. Resolver расширений использует
        # его, чтобы отличать уже видимую schema-v1 проекцию собственного
        # объекта от настоящего конфликта двух native-объектов.
        configuration.source_format = "schema-v1"
    expected_name = (
        manifest.identity.configuration_name
        if manifest.identity.source_kind is SourceKind.CONFIGURATION
        else manifest.identity.extension_name
    )
    if not expected_name or configuration.name != expected_name:
        raise GenerationRuntimeError(
            "base_structure.name не совпадает с generation identity"
        )
    extended = _ready_payload(
        root,
        layers,
        LayerKind.EXTENDED_STRUCTURE,
        required=manifest.identity.source_kind is SourceKind.EXTENSION,
    )
    extension_structure = None
    if manifest.identity.source_kind is SourceKind.CONFIGURATION:
        if extended is not None:
            _compose_extended_structure(
                configuration,
                extended.semantic,
                require_base_overlays=(
                    base_layer.provenance is not None
                    and base_layer.provenance.profile
                    is LayerSourceProfile.SCHEMA_V1
                ),
            )
    else:
        assert extended is not None
        try:
            extension_structure = ExtensionStructure.from_layer_dict(
                extended.semantic.get("extension_structure"),
                configuration,
                parent_configuration=manifest.identity.parent_configuration,
            )
        except ExtensionResolutionError as error:
            raise GenerationRuntimeError(
                f"extended_structure: {error}"
            ) from error
    try:
        materialize_standard_attributes(configuration)
    except StandardAttributeError as error:
        raise GenerationRuntimeError(
            f"стандартные реквизиты: {error}"
        ) from error
    code = _ready_payload(root, layers, LayerKind.CODE, required=False)
    forms = _ready_payload(root, layers, LayerKind.FORMS, required=False)
    source_id = (
        f"{configuration.name}:modules"
        if manifest.identity.source_kind is SourceKind.CONFIGURATION
        else (
            f"{manifest.identity.parent_configuration}:ext:"
            f"{index_cache.safe_name(manifest.identity.extension_name)}"
        )
    )
    code_sha256, locator_generation = _code_identity(layers)
    catalog = _catalog(
        root,
        code,
        forms,
        LocatorIdentity(source_id, code_sha256, locator_generation),
    )
    roles = load_role_access(root, manifest, role_cache_path)
    return NativeGenerationRuntime(
        configuration=configuration,
        base_sha256=base_layer.content_sha256,
        structure_sha256=_structure_sha256(layers),
        catalog=catalog,
        code_sha256=code_sha256,
        code_items_total=(
            layers[LayerKind.CODE].items_total
            if LayerKind.CODE in layers
            else 0
        ),
        locator_generation=locator_generation,
        roles=roles,
        extension_structure=extension_structure,
    )


__all__ = [
    "GenerationRuntimeError",
    "NativeGenerationRuntime",
    "build_generation_runtime",
    "configuration_from_base_layer",
]
