from __future__ import annotations

from copy import deepcopy

import pytest
from jsonschema import Draft202012Validator

from mcp1c.capability_modules.metadata_authoring.compiler import (
    MetadataAuthoringContractError,
    compile_metadata_object,
)
from mcp1c.capability_modules.metadata_authoring.schema import (
    METADATA_SPECIFICATION_SCHEMA,
)


def _catalog() -> dict[str, object]:
    return {
        "schema_version": 1,
        "object_ref": "Справочник.Тестовый",
        "format_version": "2.20",
        "identity": "40000000-0000-0000-0000-000000000001",
        "synonym": "Тестовый",
        "code_length": 9,
        "description_length": 150,
        "attributes": [
            {
                "name": "Артикул",
                "synonym": "Артикул",
                "type": {"kind": "string", "length": 50},
            }
        ],
        "forms": [],
    }


def _register() -> dict[str, object]:
    return {
        "schema_version": 1,
        "object_ref": "РегистрСведений.Тестовый",
        "format_version": "2.20",
        "identity": "50000000-0000-0000-0000-000000000001",
        "synonym": "Тестовый",
        "periodicity": "nonperiodical",
        "dimensions": [
            {
                "name": "Владелец",
                "synonym": "Владелец",
                "main_filter": True,
                "type": {
                    "kind": "catalog_ref",
                    "object": "Справочник.Тестовый",
                },
            }
        ],
        "resources": [
            {
                "name": "Значение",
                "synonym": "Значение",
                "type": {"kind": "number", "digits": 10, "fraction_digits": 2},
            }
        ],
        "attributes": [
            {
                "name": "Дата",
                "synonym": "Дата",
                "type": {"kind": "date", "fractions": "date_time"},
            },
            {
                "name": "Активен",
                "synonym": "Активен",
                "type": {"kind": "boolean"},
            },
        ],
        "forms": [],
    }


def _object_schemas(value: object):
    if isinstance(value, dict):
        if value.get("type") == "object":
            yield value
        for nested in value.values():
            yield from _object_schemas(nested)
    elif isinstance(value, list):
        for nested in value:
            yield from _object_schemas(nested)


def test_public_schema_закрывает_все_объекты_и_не_расширяет_границу():
    assert all(
        item.get("additionalProperties") is False
        for item in _object_schemas(METADATA_SPECIFICATION_SCHEMA)
    )
    encoded = repr(METADATA_SPECIFICATION_SCHEMA)
    assert "configuration_xml" not in encoded
    assert "configuration_registration" not in encoded


@pytest.mark.parametrize("factory", [_catalog, _register])
def test_public_schema_принимает_каждую_runtime_ветку(factory):
    Draft202012Validator.check_schema(METADATA_SPECIFICATION_SCHEMA)
    specification = factory()

    errors = list(Draft202012Validator(METADATA_SPECIFICATION_SCHEMA).iter_errors(
        specification
    ))

    assert errors == []
    assert compile_metadata_object(specification)["status"] == "compiled"


@pytest.mark.parametrize(
    ("mutate", "runtime_code"),
    [
        (
            lambda value: value.update(
                {"configuration_xml": "<Configuration/>"}
            ),
            "unknown_field",
        ),
        (lambda value: value.update({"code_length": 51}), "invalid_value"),
        (
            lambda value: value.update({"object_ref": "Документ.Тестовый"}),
            "unsupported_object_ref",
        ),
    ],
)
def test_public_schema_и_runtime_совместно_отклоняют_закрытые_случаи(
    mutate,
    runtime_code,
):
    specification = deepcopy(_catalog())
    mutate(specification)

    assert list(
        Draft202012Validator(METADATA_SPECIFICATION_SCHEMA).iter_errors(specification)
    )
    with pytest.raises(MetadataAuthoringContractError) as caught:
        compile_metadata_object(specification)
    assert caught.value.diagnostics[0]["code"] == runtime_code
