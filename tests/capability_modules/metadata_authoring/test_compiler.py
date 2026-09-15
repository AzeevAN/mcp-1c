from __future__ import annotations

import pytest

from mcp1c.capability_modules.metadata_authoring.checker import (
    check_metadata_artifacts,
)
from mcp1c.capability_modules.metadata_authoring.compiler import (
    MetadataAuthoringContractError,
    compile_metadata_object,
)


MD = "http://v8.1c.ru/8.3/MDClasses"


def _with_configuration(result: dict[str, object]) -> dict[str, str]:
    artifacts = {
        item["path"]: item["content"] for item in result["artifacts"]
    }
    registration = result["configuration_registration"]
    element = registration["element"]
    value = registration["value"]
    artifacts["Configuration.xml"] = (
        f'<MetaDataObject xmlns="{MD}"><Configuration><ChildObjects>'
        f"<{element}>{value}</{element}>"
        "</ChildObjects></Configuration></MetaDataObject>"
    )
    return artifacts


def _catalog_specification() -> dict[str, object]:
    return {
        "schema_version": 1,
        "object_ref": "Справочник.ТестовыйСправочник",
        "identity": "40000000-0000-0000-0000-000000000001",
        "synonym": "Тестовый справочник",
        "attributes": [
            {
                "name": "Артикул",
                "synonym": "Артикул",
                "type": {"kind": "string", "length": 50},
            },
            {
                "name": "Активен",
                "synonym": "Активен",
                "type": {"kind": "boolean"},
            },
        ],
        "forms": [],
    }


def _register_specification() -> dict[str, object]:
    return {
        "schema_version": 1,
        "object_ref": "РегистрСведений.ТестовыйРегистр",
        "identity": "50000000-0000-0000-0000-000000000001",
        "synonym": "Тестовый регистр",
        "periodicity": "nonperiodical",
        "dimensions": [
            {
                "name": "Справочник",
                "synonym": "Справочник",
                "main_filter": True,
                "type": {
                    "kind": "catalog_ref",
                    "object": "Справочник.ТестовыйСправочник",
                },
            }
        ],
        "resources": [
            {
                "name": "Значение",
                "synonym": "Значение",
                "type": {
                    "kind": "number",
                    "digits": 10,
                    "fraction_digits": 2,
                },
            }
        ],
        "attributes": [
            {
                "name": "ДатаАктуальности",
                "synonym": "Дата актуальности",
                "type": {"kind": "date", "fractions": "date_time"},
            }
        ],
        "forms": [
            {
                "name": "ФормаЗаписи",
                "synonym": "Запись регистра",
                "default": True,
                "form_xml": (
                    '<Form xmlns="http://v8.1c.ru/8.3/xcf/logform" '
                    'version="2.20"/>'
                ),
                "module_bsl": "",
            }
        ],
    }


def test_catalog_compiler_детерминированно_создаёт_полный_descriptor():
    first = compile_metadata_object(_catalog_specification())
    second = compile_metadata_object(_catalog_specification())

    assert first == second
    assert first["status"] == "compiled"
    assert first["configuration_registration"] == {
        "path": "Configuration.xml",
        "parent": "md:MetaDataObject/md:Configuration/md:ChildObjects",
        "element": "Catalog",
        "value": "ТестовыйСправочник",
        "xml": "<Catalog>ТестовыйСправочник</Catalog>",
    }
    assert [item["path"] for item in first["artifacts"]] == [
        "Catalogs/ТестовыйСправочник.xml"
    ]
    descriptor = first["artifacts"][0]["content"]
    for category in ("Object", "Ref", "Selection", "List", "Manager"):
        assert f'category="{category}"' in descriptor
        assert f'name="Catalog{category}.ТестовыйСправочник"' in descriptor
    checked = check_metadata_artifacts(
        "Справочник.ТестовыйСправочник", _with_configuration(first)
    )
    assert checked["status"] == "passed"


def test_register_compiler_упаковывает_forms_и_проходит_checker():
    result = compile_metadata_object(_register_specification())

    assert result["status"] == "compiled"
    artifacts = _with_configuration(result)
    assert set(artifacts) == {
        "Configuration.xml",
        "InformationRegisters/ТестовыйРегистр.xml",
        "InformationRegisters/ТестовыйРегистр/Forms/ФормаЗаписи.xml",
        "InformationRegisters/ТестовыйРегистр/Forms/ФормаЗаписи/Ext/Form.xml",
        "InformationRegisters/ТестовыйРегистр/Forms/ФормаЗаписи/Ext/Form/Module.bsl",
    }
    descriptor = artifacts["InformationRegisters/ТестовыйРегистр.xml"]
    assert "cfg:CatalogRef.ТестовыйСправочник" in descriptor
    assert "<InformationRegisterPeriodicity>Nonperiodical" in descriptor
    assert "<WriteMode>Independent" in descriptor
    checked = check_metadata_artifacts(
        "РегистрСведений.ТестовыйРегистр", artifacts
    )
    assert checked["status"] == "passed"


def test_compiler_не_создаёт_bundle_за_пределами_checker():
    specification = _catalog_specification()
    specification["forms"] = [
        {
            "name": "ФормаЭлемента",
            "synonym": "Форма элемента",
            "default": True,
            "form_xml": '<Form xmlns="http://v8.1c.ru/8.3/xcf/logform"/>',
            "module_bsl": "x" * (2 * 1024 * 1024 + 1),
        }
    ]

    with pytest.raises(MetadataAuthoringContractError) as caught:
        compile_metadata_object(specification)

    assert "artifact_too_large" in {
        item["code"] for item in caught.value.diagnostics
    }


@pytest.mark.parametrize(
    "change, code",
    [
        (lambda value: value.pop("identity"), "missing_required_field"),
        (lambda value: value.update(schema_version=2), "unsupported_schema_version"),
        (lambda value: value.update(unknown=True), "unknown_field"),
    ],
)
def test_compiler_отклоняет_неполный_или_неизвестный_контракт(change, code):
    specification = _catalog_specification()
    change(specification)

    with pytest.raises(MetadataAuthoringContractError) as caught:
        compile_metadata_object(specification)

    assert code in {item["code"] for item in caught.value.diagnostics}
