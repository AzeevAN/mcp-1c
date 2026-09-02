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
from .model import Configuration, Field, MetadataObject, TabularPart
from .module_catalog import (
    CandidateOutcome,
    CatalogCoverage,
    CatalogEntry,
    FormSource,
    ModuleCatalog,
)
from .module_content import LocatorIdentity, ModuleLocator
from .role_access import LoadedRoleAccess, load_role_access


class GenerationRuntimeError(ValueError):
    """Слои поколения нельзя безопасно представить действующему runtime."""


@dataclass(frozen=True, slots=True)
class NativeGenerationRuntime:
    configuration: Configuration
    base_sha256: str
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


def _text_list(value: object, label: str) -> list[str]:
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise GenerationRuntimeError(f"{label} должен быть массивом строк")
    return list(value)


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
}


def _field(value: object, label: str) -> Field:
    raw = _mapping(value, label)
    _exact_keys(raw, _FIELD_KEYS, label)
    return Field(
        name=_text(raw["name"], f"{label}.name", required=True),
        synonym=_text(raw["synonym"], f"{label}.synonym"),
        comment=_text(raw["comment"], f"{label}.comment"),
        indexing=_text(raw["indexing"], f"{label}.indexing"),
        types=_text_list(raw["types"], f"{label}.types"),
        string_length=_optional_int(raw["string_length"], f"{label}.string_length"),
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


def configuration_from_base_layer(semantic: object) -> Configuration:
    """Восстановить формат-независимую модель из canonical base layer."""
    raw = _mapping(semantic, "base_structure")
    _exact_keys(
        raw,
        {"name", "synonym", "version", "vendor", "schema_version", "objects"},
        "base_structure",
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
    return Configuration(
        name=_text(raw["name"], "base_structure.name", required=True),
        synonym=_text(raw["synonym"], "base_structure.synonym"),
        version=_text(raw["version"], "base_structure.version"),
        vendor=_text(raw["vendor"], "base_structure.vendor"),
        schema_version=_text(
            raw["schema_version"], "base_structure.schema_version", required=True
        ),
        source_format="source-b",
        objects=objects,
    )


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
) -> dict[str, tuple[LayerMember, bool]]:
    if payload is None:
        return {}
    modules = payload.semantic.get("modules")
    if not isinstance(modules, list):
        raise GenerationRuntimeError("code.modules должен быть массивом")
    members = {member.key: member for member in payload.members}
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
    result: dict[str, tuple[LayerMember, bool]] = {}
    for address, member in members.items():
        size, digest, compiled = declared[address]
        if (size, digest) != (member.size, member.sha256):
            raise GenerationRuntimeError("code semantic и member identity расходятся")
        expected_suffix = ".Module" if compiled else ".bsl"
        if not member.relative_path.endswith(expected_suffix):
            raise GenerationRuntimeError("code member не совпадает с compiled")
        result[address] = (member, compiled)
    return result


def _form_kind(source_path: str) -> tuple[str, bool]:
    if source_path.endswith("/Ext/Form.xml"):
        return "form_xml", False
    if source_path.endswith("/Ext/Form.bin"):
        return "form_bin", True
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
    code: LayerPayload | None,
    forms: LayerPayload | None,
    identity: LocatorIdentity,
) -> ModuleCatalog | None:
    code_members = _code_members(code)
    form_members = _form_members(forms)
    if code is None and forms is None:
        return None
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
        code_member = code_members.get(address)
        member = code_member[0] if code_member is not None else None
        compiled = code_member[1] if code_member is not None else False
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
                if member is not None
                else None
            ),
            is_form=address in form_members,
            compiled=compiled,
            form_sources=form_members.get(address, ()),
            diagnostics=(),
            conflict=False,
            address_collision=False,
            sort_key=(address.casefold(), address),
        )
    outcomes: list[CandidateOutcome] = []
    ordinal = 0
    compiled_total = 0
    for address in sorted(code_members, key=lambda value: (value.casefold(), value)):
        ordinal += 1
        category = "compiled" if code_members[address][1] else "indexed"
        compiled_total += category == "compiled"
        outcomes.append(CandidateOutcome(ordinal, category, address))
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
        problems=(),
        coverage=CatalogCoverage(
            total_candidates=ordinal,
            indexed=ordinal - compiled_total,
            compiled=compiled_total,
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
    configuration = configuration_from_base_layer(base.semantic)
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
    if manifest.identity.source_kind is SourceKind.EXTENSION:
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
        code,
        forms,
        LocatorIdentity(source_id, code_sha256, locator_generation),
    )
    roles = load_role_access(root, manifest, role_cache_path)
    return NativeGenerationRuntime(
        configuration=configuration,
        base_sha256=base_layer.content_sha256,
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
