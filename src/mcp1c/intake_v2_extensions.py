"""Чистый resolver структуры native-расширения против одного поколения базы.

Snapshot расширения уже содержит собственные объекты и borrowed overlays.
Поэтому смена базы читает только эти сохранённые адреса: исходный ZIP и второй
постоянный граф для проверки не нужны.
"""

from __future__ import annotations

import copy
from dataclasses import dataclass
from enum import Enum
from types import MappingProxyType
from typing import Mapping

from .model import Configuration, Field, MetadataObject, TabularPart


class ExtensionResolutionError(ValueError):
    """Extension snapshot нельзя доказуемо связать с выбранной базой."""


class ExtensionRelationState(str, Enum):
    RESOLVED = "resolved"
    TARGET_MISSING = "target_missing"


@dataclass(frozen=True, slots=True)
class ExtensionRelation:
    extension: str
    target: str
    state: ExtensionRelationState


def _objects(
    value: Mapping[str, MetadataObject], label: str
) -> Mapping[str, MetadataObject]:
    if not isinstance(value, Mapping):
        raise ExtensionResolutionError(f"{label} должен быть mapping")
    result: dict[str, MetadataObject] = {}
    folded: set[str] = set()
    for key, item in value.items():
        if not isinstance(key, str) or not key or not isinstance(item, MetadataObject):
            raise ExtensionResolutionError(f"{label} содержит неверный объект")
        if key != item.full_name:
            raise ExtensionResolutionError(f"{label}: ключ не совпадает с full_name")
        normalized = key.casefold()
        if normalized in folded:
            raise ExtensionResolutionError(
                f"{label}: адреса различаются только регистром"
            )
        folded.add(normalized)
        result[key] = item
    return MappingProxyType(
        dict(sorted(result.items(), key=lambda item: (item[0].casefold(), item[0])))
    )


@dataclass(frozen=True, slots=True)
class ExtensionStructure:
    """Сохранённая структура одного расширения без вывода о его активности."""

    name: str
    parent_configuration: str
    own_objects: Mapping[str, MetadataObject]
    borrowed_overlays: Mapping[str, MetadataObject]
    borrowed_field_targets: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.name, str) or not self.name:
            raise ExtensionResolutionError("имя расширения обязательно")
        if not isinstance(self.parent_configuration, str):
            raise ExtensionResolutionError("родитель расширения должен быть строкой")
        own = _objects(self.own_objects, "own_objects")
        borrowed = _objects(self.borrowed_overlays, "borrowed_overlays")
        if {key.casefold() for key in own} & {
            key.casefold() for key in borrowed
        }:
            raise ExtensionResolutionError(
                "объект не может быть собственным и borrowed одновременно"
            )
        object.__setattr__(self, "own_objects", own)
        object.__setattr__(self, "borrowed_overlays", borrowed)
        fields = self.borrowed_field_targets
        if (
            not isinstance(fields, tuple)
            or not all(isinstance(item, str) and item for item in fields)
            or len({item.casefold() for item in fields}) != len(fields)
        ):
            raise ExtensionResolutionError(
                "borrowed_field_targets должен быть tuple уникальных адресов"
            )
        roots = tuple(f"{name}.".casefold() for name in borrowed)
        if any(not item.casefold().startswith(roots) for item in fields):
            raise ExtensionResolutionError(
                "borrowed field не принадлежит borrowed object"
            )
        object.__setattr__(
            self,
            "borrowed_field_targets",
            tuple(sorted(fields, key=lambda item: (item.casefold(), item))),
        )

    def to_layer_dict(self) -> dict[str, object]:
        """Слой хранит адреса, а тела объектов уже лежат в base_structure."""
        return {
            "name": self.name,
            "own_objects": list(self.own_objects),
            "borrowed_targets": list(self.borrowed_overlays),
            "borrowed_field_targets": list(self.borrowed_field_targets),
        }

    @classmethod
    def from_layer_dict(
        cls,
        value: object,
        configuration: Configuration,
        *,
        parent_configuration: str,
    ) -> "ExtensionStructure":
        if not isinstance(value, dict) or set(value) != {
            "name",
            "own_objects",
            "borrowed_targets",
            "borrowed_field_targets",
        }:
            raise ExtensionResolutionError(
                "extended_structure не содержит extension contract"
            )
        name = value["name"]
        own_names = value["own_objects"]
        borrowed_names = value["borrowed_targets"]
        borrowed_field_targets = value["borrowed_field_targets"]
        if (
            not isinstance(name, str)
            or not isinstance(own_names, list)
            or not isinstance(borrowed_names, list)
            or not all(isinstance(item, str) for item in own_names + borrowed_names)
            or not isinstance(borrowed_field_targets, list)
            or not all(isinstance(item, str) for item in borrowed_field_targets)
            or len(own_names) != len(set(own_names))
            or len(borrowed_names) != len(set(borrowed_names))
        ):
            raise ExtensionResolutionError("extension contract повреждён")
        if name != configuration.name:
            raise ExtensionResolutionError(
                "имя extension contract не совпадает с base_structure"
            )
        objects = configuration.objects
        missing = (set(own_names) | set(borrowed_names)) - set(objects)
        extra = set(objects) - (set(own_names) | set(borrowed_names))
        if missing or extra:
            raise ExtensionResolutionError(
                "extension contract не покрывает объекты base_structure"
            )
        return cls(
            name=name,
            parent_configuration=parent_configuration,
            own_objects={key: objects[key] for key in own_names},
            borrowed_overlays={key: objects[key] for key in borrowed_names},
            borrowed_field_targets=tuple(borrowed_field_targets),
        )


@dataclass(frozen=True, slots=True)
class ExtensionResolution:
    configuration: Configuration
    relations: tuple[ExtensionRelation, ...]


@dataclass(frozen=True, slots=True)
class BaseIdentityIndex:
    """Один transient index кандидата базы на весь проход расширений."""

    objects: Mapping[str, str]
    fields: Mapping[str, frozenset[str]]

    @classmethod
    def build(cls, base: Configuration) -> "BaseIdentityIndex":
        objects: dict[str, str] = {}
        fields: dict[str, frozenset[str]] = {}
        for name, obj in base.objects.items():
            folded = name.casefold()
            objects[folded] = name
            fields[folded] = frozenset(
                path.casefold() for path, _field in obj.all_fields()
            )
        return cls(
            MappingProxyType(objects),
            MappingProxyType(fields),
        )


def _validate_parent(base: Configuration, extension: ExtensionStructure) -> None:
    if not extension.parent_configuration or (
        extension.parent_configuration != base.name
    ):
        raise ExtensionResolutionError(
            f"родитель расширения {extension.name} не совпадает с базой {base.name}"
        )


def resolve_extension_relations(
    base: Configuration,
    extension: ExtensionStructure,
) -> tuple[ExtensionRelation, ...]:
    """Проверить только hard edges за O(base identities + borrowed edges)."""
    if not isinstance(base, Configuration) or not isinstance(
        extension, ExtensionStructure
    ):
        raise TypeError("resolver требует Configuration и ExtensionStructure")
    _validate_parent(base, extension)
    return resolve_extension_relations_against(
        BaseIdentityIndex.build(base),
        extension,
    )


def resolve_extension_relations_against(
    base: BaseIdentityIndex,
    extension: ExtensionStructure,
) -> tuple[ExtensionRelation, ...]:
    if not isinstance(base, BaseIdentityIndex) or not isinstance(
        extension, ExtensionStructure
    ):
        raise TypeError("resolver требует BaseIdentityIndex и ExtensionStructure")
    roots = {
        target.casefold(): target
        for target in extension.borrowed_overlays
    }

    def exists(target: str) -> bool:
        folded = target.casefold()
        if folded in roots:
            return folded in base.objects
        owner = next(
            (root for root in roots if folded.startswith(root + ".")),
            "",
        )
        if not owner or owner not in base.objects:
            return False
        return folded[len(owner) + 1 :] in base.fields.get(owner, frozenset())

    return tuple(
        ExtensionRelation(
            extension.name,
            target,
            (
                ExtensionRelationState.RESOLVED
                if exists(target)
                else ExtensionRelationState.TARGET_MISSING
            ),
        )
        for target in (
            *extension.borrowed_overlays,
            *extension.borrowed_field_targets,
        )
    )


def resolve_extension_relation_map(
    base: Configuration,
    extensions: Mapping[str, ExtensionStructure],
) -> Mapping[str, tuple[ExtensionRelation, ...]]:
    """Проверить несколько слоёв с одним построением base identities."""
    if not isinstance(base, Configuration) or not isinstance(extensions, Mapping):
        raise TypeError("resolver требует Configuration и mapping расширений")
    if not extensions:
        return MappingProxyType({})
    index = BaseIdentityIndex.build(base)
    result: dict[str, tuple[ExtensionRelation, ...]] = {}
    for key, extension in extensions.items():
        if not isinstance(key, str) or not isinstance(extension, ExtensionStructure):
            raise TypeError("mapping расширений содержит неверную запись")
        _validate_parent(base, extension)
        result[key] = resolve_extension_relations_against(index, extension)
    return MappingProxyType(result)


def _append_fields(
    target: list[Field],
    overlay: list[Field],
    *,
    owner: str,
    borrowed: frozenset[str],
) -> None:
    known = {item.name.casefold() for item in target}
    for item in overlay:
        field_target = f"{owner}.{item.name}".casefold()
        if field_target in borrowed:
            continue
        if item.name.casefold() not in known:
            target.append(copy.deepcopy(item))
            known.add(item.name.casefold())


def _merge_tabular_parts(
    target: list[TabularPart],
    overlay: list[TabularPart],
    *,
    owner: str,
    borrowed: frozenset[str],
) -> None:
    by_name = {item.name.casefold(): item for item in target}
    for item in overlay:
        current = by_name.get(item.name.casefold())
        if current is None:
            copied = copy.deepcopy(item)
            copied.attributes = [
                field
                for field in copied.attributes
                if f"{owner}.{copied.name}.{field.name}".casefold()
                not in borrowed
            ]
            target.append(copied)
            by_name[item.name.casefold()] = copied
        else:
            _append_fields(
                current.attributes,
                item.attributes,
                owner=f"{owner}.{current.name}",
                borrowed=borrowed,
            )


def _merge_overlay(
    target: MetadataObject,
    overlay: MetadataObject,
    borrowed: frozenset[str],
) -> None:
    if target.kind != overlay.kind or target.name.casefold() != overlay.name.casefold():
        raise ExtensionResolutionError("borrowed overlay не совпадает с целью")
    _append_fields(
        target.attributes,
        overlay.attributes,
        owner=target.full_name,
        borrowed=borrowed,
    )
    _append_fields(
        target.dimensions,
        overlay.dimensions,
        owner=target.full_name,
        borrowed=borrowed,
    )
    _append_fields(
        target.resources,
        overlay.resources,
        owner=target.full_name,
        borrowed=borrowed,
    )
    _merge_tabular_parts(
        target.tabular_parts,
        overlay.tabular_parts,
        owner=target.full_name,
        borrowed=borrowed,
    )
    for key, value in overlay.props.items():
        target.props.setdefault(key, copy.deepcopy(value))
    for attribute in ("movements", "based_on", "owners", "predefined"):
        current = getattr(target, attribute)
        known = {item.casefold() for item in current}
        current.extend(
            copy.deepcopy(item)
            for item in getattr(overlay, attribute)
            if item.casefold() not in known
        )
    enum_names = {name.casefold() for name, _title in target.enum_values}
    target.enum_values.extend(
        copy.deepcopy(item)
        for item in overlay.enum_values
        if item[0].casefold() not in enum_names
    )
    if (
        target.value_type is None
        and overlay.value_type is not None
        and f"{target.full_name}.ТипЗначения".casefold() not in borrowed
    ):
        target.value_type = copy.deepcopy(overlay.value_type)


def resolve_extension_structure(
    base: Configuration,
    extension: ExtensionStructure,
) -> ExtensionResolution:
    """Построить on-demand resolved view без изменения опубликованных слоёв."""
    relations = resolve_extension_relations(base, extension)
    borrowed = frozenset(
        item.casefold() for item in extension.borrowed_field_targets
    )
    configuration = copy.deepcopy(base)
    folded = {name.casefold(): name for name in configuration.objects}
    for name, item in extension.own_objects.items():
        canonical = folded.get(name.casefold())
        if canonical is not None and base.source_format not in {"json", "xml"}:
            raise ExtensionResolutionError(
                f"собственный объект расширения конфликтует с базой: {name}"
            )
        if canonical is not None:
            base_fields = {
                path.casefold()
                for path, _field in configuration.objects[canonical].all_fields()
            }
            extension_fields = {
                path.casefold() for path, _field in item.all_fields()
            }
            if base_fields != extension_fields:
                raise ExtensionResolutionError(
                    "собственный объект расширения и его проекция schema v1 "
                    f"содержат разный набор полей: {name}"
                )
            # Legacy schema v1 снимается из runtime-конфигурации и уже видит
            # активные собственные объекты расширений. При доказанном полном
            # совпадении адресов native-снимок расширения точнее: он сохраняет
            # происхождение и исходные XML-значения свойств.
            del configuration.objects[canonical]
        configuration.objects[name] = copy.deepcopy(item)
        folded[name.casefold()] = name
    relation_by_target = {item.target: item for item in relations}
    for target, overlay in extension.borrowed_overlays.items():
        relation = relation_by_target[target]
        if relation.state is ExtensionRelationState.TARGET_MISSING:
            continue
        canonical = folded[relation.target.casefold()]
        _merge_overlay(
            configuration.objects[canonical],
            overlay,
            borrowed,
        )
    return ExtensionResolution(configuration, relations)


__all__ = [
    "ExtensionRelation",
    "ExtensionRelationState",
    "ExtensionResolution",
    "ExtensionResolutionError",
    "ExtensionStructure",
    "BaseIdentityIndex",
    "resolve_extension_relation_map",
    "resolve_extension_relations",
    "resolve_extension_relations_against",
    "resolve_extension_structure",
]
