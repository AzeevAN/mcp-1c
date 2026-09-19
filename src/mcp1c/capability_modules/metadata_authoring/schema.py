"""Публичная JSON Schema входа Metadata Authoring без runtime-перехвата."""

from __future__ import annotations

from collections.abc import Callable
from copy import deepcopy
from dataclasses import dataclass
from typing import Annotated, TypeAlias


_NAME_PATTERN = r"^[A-Za-zА-Яа-яЁё_][A-Za-zА-Яа-яЁё0-9_]*$"
_FORMAT_VERSION_PATTERN = r"^\d+\.\d+(?:\.\d+)*$"


def _closed_object(
    properties: dict[str, object],
    required: tuple[str, ...],
    **constraints: object,
) -> dict[str, object]:
    return {
        "type": "object",
        "properties": properties,
        "required": list(required),
        "additionalProperties": False,
        **constraints,
    }


def _field_type_schema() -> dict[str, object]:
    return {
        "oneOf": [
            _closed_object(
                {
                    "kind": {"const": "string"},
                    "length": {"type": "integer", "minimum": 1, "maximum": 1024},
                    "allowed_length": {"const": "Variable"},
                },
                ("kind", "length"),
            ),
            _closed_object({"kind": {"const": "boolean"}}, ("kind",)),
            _closed_object(
                {
                    "kind": {"const": "number"},
                    "digits": {"type": "integer", "minimum": 1, "maximum": 32},
                    "fraction_digits": {
                        "type": "integer",
                        "minimum": 0,
                        "maximum": 32,
                    },
                    "allowed_sign": {"const": "Any"},
                },
                ("kind", "digits", "fraction_digits"),
            ),
            _closed_object(
                {
                    "kind": {"const": "date"},
                    "fractions": {"enum": ["date", "time", "date_time"]},
                },
                ("kind", "fractions"),
            ),
            _closed_object(
                {
                    "kind": {"const": "catalog_ref"},
                    "object": {
                        "type": "string",
                        "pattern": rf"^Справочник\.{_NAME_PATTERN[1:-1]}$",
                    },
                },
                ("kind", "object"),
            ),
            _closed_object(
                {
                    "kind": {"const": "document_ref"},
                    "object": {
                        "type": "string",
                        "pattern": rf"^Документ\.{_NAME_PATTERN[1:-1]}$",
                    },
                },
                ("kind", "object"),
            ),
        ]
    }


def _field_schema(*, dimension: bool = False) -> dict[str, object]:
    properties: dict[str, object] = {
        "name": {"type": "string", "pattern": _NAME_PATTERN},
        "synonym": {"type": "string", "minLength": 1},
        "type": _field_type_schema(),
    }
    if dimension:
        properties["main_filter"] = {"type": "boolean"}
    return _closed_object(properties, ("name", "synonym", "type"))


def _forms_schema(roles: tuple[str, ...]) -> dict[str, object]:
    return {
        "type": "array",
        "items": _closed_object(
            {
                "name": {"type": "string", "pattern": _NAME_PATTERN},
                "synonym": {"type": "string", "minLength": 1},
                "role": {"enum": list(roles)},
                "default": {"type": "boolean"},
                "form_xml": {"type": "string", "minLength": 1},
                "module_bsl": {"type": "string"},
            },
            ("name", "synonym", "default", "form_xml", "module_bsl"),
        ),
    }


def _common_properties(
    object_kind: str, form_roles: tuple[str, ...]
) -> dict[str, object]:
    return {
        "schema_version": {"const": 1},
        "object_ref": {
            "type": "string",
            "pattern": rf"^{object_kind}\.{_NAME_PATTERN[1:-1]}$",
        },
        "format_version": {"type": "string", "pattern": _FORMAT_VERSION_PATTERN},
        "identity": {"type": "string", "format": "uuid"},
        "synonym": {"type": "string", "minLength": 1},
        "attributes": {"type": "array", "items": _field_schema()},
        "forms": _forms_schema(form_roles),
    }


_COMMON_REQUIRED = (
    "schema_version",
    "object_ref",
    "format_version",
    "identity",
    "synonym",
    "attributes",
    "forms",
)


def _catalog_schema() -> dict[str, object]:
    properties = _common_properties("Справочник", ("object", "list", "choice"))
    properties.update(
        {
            "code_length": {"type": "integer", "minimum": 0, "maximum": 50},
            "description_length": {
                "type": "integer",
                "minimum": 0,
                "maximum": 150,
            },
        }
    )
    return _closed_object(
        properties,
        (*_COMMON_REQUIRED, "code_length", "description_length"),
        title="Справочник",
    )


def _information_register_schema() -> dict[str, object]:
    properties = _common_properties("РегистрСведений", ("record",))
    properties.update(
        {
            "periodicity": {"const": "nonperiodical"},
            "dimensions": {"type": "array", "items": _field_schema(dimension=True)},
            "resources": {"type": "array", "items": _field_schema()},
        }
    )
    return _closed_object(
        properties,
        (*_COMMON_REQUIRED, "periodicity", "dimensions", "resources"),
        title="РегистрСведений",
        anyOf=[
            {"properties": {name: {"minItems": 1}}, "required": [name]}
            for name in ("dimensions", "resources", "attributes")
        ],
    )


def _document_schema() -> dict[str, object]:
    properties = _common_properties("Документ", ("object", "list", "choice"))
    properties.update(
        {
            "number_length": {"type": "integer", "minimum": 1, "maximum": 50},
            "number_allowed_length": {"enum": ["Variable", "Fixed"]},
            "number_periodicity": {"enum": ["Nonperiodical", "Year"]},
            "check_unique": {"type": "boolean"},
            "autonumbering": {"type": "boolean"},
            "posting": {"const": "Deny"},
            "real_time_posting": {"const": "Deny"},
        }
    )
    return _closed_object(
        properties,
        (
            *_COMMON_REQUIRED,
            "number_length",
            "number_allowed_length",
            "number_periodicity",
            "check_unique",
            "autonumbering",
            "posting",
            "real_time_posting",
        ),
        title="Документ",
    )


METADATA_SPECIFICATION_SCHEMA: dict[str, object] = {
    "oneOf": [_catalog_schema(), _information_register_schema(), _document_schema()]
}


@dataclass(frozen=True, slots=True)
class _JsonSchemaFacade:
    """Публиковать полную схему, оставляя runtime-тип обычным словарём."""

    schema: dict[str, object]

    def __get_pydantic_json_schema__(
        self,
        core_schema: object,
        handler: Callable[[object], dict[str, object]],
    ) -> dict[str, object]:
        del core_schema, handler
        return deepcopy(self.schema)


MetadataSpecification: TypeAlias = Annotated[
    dict[str, object],
    _JsonSchemaFacade(METADATA_SPECIFICATION_SCHEMA),
]


__all__ = ["METADATA_SPECIFICATION_SCHEMA", "MetadataSpecification"]
