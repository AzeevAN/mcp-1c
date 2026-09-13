"""Тонкая read-only граница между Forms и согласованным Registry-контекстом."""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from dataclasses import dataclass, replace
from types import MappingProxyType
from typing import Protocol

from ...model import Configuration
from ...registry import RegistryError
from .diagnostics import CheckStatus, Diagnostic, FormsResult
from .version_catalog import normalized_platform_version


class RegistryResolver(Protocol):
    def resolve(
        self,
        name: str | None = None,
        *,
        require_configuration: bool = True,
        extension: str | None = None,
    ) -> object: ...


@dataclass(frozen=True, slots=True)
class FormsFieldSnapshot:
    types: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class FormsObjectSnapshot:
    fields: Mapping[str, FormsFieldSnapshot]


@dataclass(frozen=True, slots=True)
class FormsRegistrySnapshot:
    """Минимальный стабильный view опубликованного поколения Registry."""

    platform_version: str | None
    _configuration: Configuration

    def object(self, reference: str) -> FormsObjectSnapshot | None:
        item = self._configuration.get(reference)
        if item is None:
            return None
        fields = {
            path: FormsFieldSnapshot(tuple(field.types))
            for path, field in item.all_fields()
        }
        return FormsObjectSnapshot(MappingProxyType(fields))


@dataclass(frozen=True, slots=True)
class RegistryResolution:
    snapshot: FormsRegistrySnapshot | None
    diagnostics: tuple[Diagnostic, ...]
    status: CheckStatus


def resolve_registry_snapshot(
    registry: RegistryResolver | None,
    configuration: str | None,
) -> RegistryResolution:
    if registry is None:
        return RegistryResolution(
            None,
            (
                Diagnostic(
                    "configuration_links",
                    "not_checked",
                    "registry_snapshot_not_used",
                    "$",
                    "Forms не получал Registry-контекст.",
                ),
            ),
            "not_checked",
        )
    try:
        resolved = registry.resolve(configuration)
    except RegistryError:
        return RegistryResolution(
            None,
            (
                Diagnostic(
                    "configuration_links",
                    "warning",
                    "registry_context_unavailable",
                    "$configuration",
                    "Целевая конфигурация не выбрана или недоступна; ссылки не проверены.",
                ),
            ),
            "not_checked",
        )
    loaded = getattr(resolved, "configuration", None)
    config = getattr(loaded, "config", None)
    if not isinstance(config, Configuration):
        return RegistryResolution(
            None,
            (
                Diagnostic(
                    "configuration_links",
                    "warning",
                    "registry_context_unavailable",
                    "$configuration",
                    "Registry не вернул нормализованную конфигурацию; ссылки не проверены.",
                ),
            ),
            "not_checked",
        )
    platform_version = (
        config.platform
        if normalized_platform_version(config.platform) is not None
        else None
    )
    return RegistryResolution(
        FormsRegistrySnapshot(platform_version, config),
        (),
        "passed",
    )


def specification_with_registry_platform(
    specification: object,
    resolution: RegistryResolution,
) -> object:
    if not isinstance(specification, Mapping) or resolution.snapshot is None:
        return specification
    if "platform_version" in specification:
        return specification
    platform = resolution.snapshot.platform_version
    if platform is None:
        return specification
    return {**specification, "platform_version": platform}


def _walk_elements(
    elements: object,
    path: str = "$.elements",
) -> Iterator[tuple[Mapping[str, object], str]]:
    if not isinstance(elements, list):
        return
    for index, raw in enumerate(elements):
        if not isinstance(raw, Mapping):
            continue
        item_path = f"{path}[{index}]"
        yield raw, item_path
        kind = raw.get("kind")
        if kind == "usual_group":
            yield from _walk_elements(raw.get("children"), f"{item_path}.children")
        elif kind == "pages":
            pages = raw.get("pages")
            if isinstance(pages, list):
                for page_index, page in enumerate(pages):
                    if isinstance(page, Mapping):
                        yield from _walk_elements(
                            page.get("children"),
                            f"{item_path}.pages[{page_index}].children",
                        )


def _required_registry_type(element: Mapping[str, object]) -> str | None:
    if element.get("kind") == "check_box_field":
        return "Булево"
    if element.get("kind") == "input_field" and element.get("choice_list"):
        return "Строка"
    return None


def _walk_metadata_references(
    value_type: object, path: str
) -> Iterator[tuple[str, str]]:
    if not isinstance(value_type, Mapping):
        return
    kind = value_type.get("kind")
    if kind == "metadata_reference":
        reference = value_type.get("object")
        if isinstance(reference, str):
            yield reference, f"{path}.object"
    elif kind == "composite":
        variants = value_type.get("variants")
        if isinstance(variants, list):
            for index, variant in enumerate(variants):
                yield from _walk_metadata_references(
                    variant, f"{path}.variants[{index}]"
                )
    elif kind == "value_table":
        columns = value_type.get("columns")
        if isinstance(columns, list):
            for index, column in enumerate(columns):
                if isinstance(column, Mapping):
                    yield from _walk_metadata_references(
                        column.get("type"), f"{path}.columns[{index}].type"
                    )


def validate_registry_links(
    specification: Mapping[str, object] | None,
    resolution: RegistryResolution,
) -> RegistryResolution:
    snapshot = resolution.snapshot
    if snapshot is None or specification is None:
        return resolution

    diagnostics: list[Diagnostic] = []
    object_attributes: dict[str, FormsObjectSnapshot | None] = {}
    attributes = specification.get("attributes")
    if isinstance(attributes, list):
        for index, raw in enumerate(attributes):
            if not isinstance(raw, Mapping):
                continue
            value_type = raw.get("type")
            if not isinstance(value_type, Mapping):
                continue
            for reference, reference_path in _walk_metadata_references(
                value_type, f"$.attributes[{index}].type"
            ):
                if snapshot.object(reference) is None:
                    diagnostics.append(
                        Diagnostic(
                            "configuration_links",
                            "warning",
                            "metadata_reference_not_found",
                            reference_path,
                            "Ссылочный тип не найден в выбранном Registry snapshot; ссылка не подтверждена.",
                        )
                    )
            if value_type.get("kind") != "metadata_object":
                continue
            name = raw.get("name")
            reference = value_type.get("object")
            if not isinstance(name, str) or not isinstance(reference, str):
                continue
            item = snapshot.object(reference)
            object_attributes[name] = item
            if item is None:
                diagnostics.append(
                    Diagnostic(
                        "configuration_links",
                        "warning",
                        "metadata_object_not_found",
                        f"$.attributes[{index}].type.object",
                        "Объект не найден в выбранном Registry snapshot; ссылка не подтверждена.",
                    )
                )

    for element, path in _walk_elements(specification.get("elements")):
        data_path = element.get("data_path")
        if not isinstance(data_path, str) or "." not in data_path:
            continue
        root, field_path = data_path.split(".", 1)
        if root not in object_attributes:
            continue
        object_snapshot = object_attributes[root]
        if object_snapshot is None:
            continue
        field = object_snapshot.fields.get(field_path)
        if field is None:
            diagnostics.append(
                Diagnostic(
                    "configuration_links",
                    "warning",
                    "metadata_field_not_found",
                    f"{path}.data_path",
                    "Поле объекта не найдено в выбранном Registry snapshot.",
                )
            )
            continue
        required_type = _required_registry_type(element)
        if (
            required_type is not None
            and field.types
            and required_type not in field.types
        ):
            diagnostics.append(
                Diagnostic(
                    "configuration_links",
                    "failed",
                    "registry_field_type_conflict",
                    f"{path}.data_path",
                    "Тип поля в Registry несовместим с выбранным элементом формы.",
                )
            )
        elif required_type is not None and not field.types:
            diagnostics.append(
                Diagnostic(
                    "configuration_links",
                    "warning",
                    "registry_field_type_unknown",
                    f"{path}.data_path",
                    "Поле найдено, но Registry не содержит доказанного типа.",
                )
            )

    platform = specification.get("platform_version")
    registry_platform = snapshot.platform_version
    if (
        isinstance(platform, str)
        and registry_platform is not None
        and normalized_platform_version(platform)
        != normalized_platform_version(registry_platform)
    ):
        diagnostics.append(
            Diagnostic(
                "configuration_links",
                "failed",
                "platform_version_conflicts_with_registry",
                "$.platform_version",
                "Версия в спецификации не совпадает с платформой выбранного Registry snapshot.",
            )
        )

    if any(item.status == "failed" for item in diagnostics):
        status: CheckStatus = "failed"
    elif diagnostics:
        status = "warning"
    else:
        status = "passed"
        diagnostics.append(
            Diagnostic(
                "configuration_links",
                "passed",
                "registry_links_verified",
                "$",
                "Объектные ссылки и доступные типы проверены по Registry snapshot.",
            )
        )
    return RegistryResolution(snapshot, tuple(diagnostics), status)


def apply_registry_resolution(
    result: FormsResult,
    resolution: RegistryResolution,
) -> FormsResult:
    diagnostics = tuple(
        item
        for item in result.diagnostics
        if item.code != "registry_snapshot_not_used"
    ) + resolution.diagnostics
    coverage = replace(
        result.coverage,
        configuration_links=resolution.status,
    )
    if resolution.status == "failed":
        return replace(
            result,
            status="rejected",
            artifacts=(),
            diagnostics=diagnostics,
            coverage=coverage,
            instructions=(
                "Исправьте доказанный конфликт с выбранным Registry snapshot.",
            ),
        )
    return replace(result, diagnostics=diagnostics, coverage=coverage)


__all__ = [
    "RegistryResolver",
    "apply_registry_resolution",
    "resolve_registry_snapshot",
    "specification_with_registry_platform",
    "validate_registry_links",
]
