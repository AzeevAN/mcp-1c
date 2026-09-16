from __future__ import annotations

import pytest

from mcp1c.capability_modules.metadata_authoring.checker import (
    check_metadata_artifacts,
)
from mcp1c.capability_modules.metadata_authoring.compiler import (
    MetadataAuthoringContractError,
    compile_metadata_object,
)


def _catalog_specification() -> dict[str, object]:
    return {
        "schema_version": 1,
        "object_ref": "Справочник.ТестовыйСправочник",
        "format_version": "2.20",
        "identity": "40000000-0000-0000-0000-000000000001",
        "synonym": "Тестовый справочник",
        "code_length": 9,
        "description_length": 150,
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
        "format_version": "2.20",
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
    assert set(first) == {
        "status", "schema_version", "object_ref", "format_version", "artifacts",
        "diagnostics",
    }
    assert first["format_version"] == "2.20"
    assert [item["path"] for item in first["artifacts"]] == [
        "Catalogs/ТестовыйСправочник.xml"
    ]
    descriptor = first["artifacts"][0]["content"]
    assert "<CodeLength>9</CodeLength>" in descriptor
    assert "<DescriptionLength>150</DescriptionLength>" in descriptor
    assert descriptor.index("StandardAttribute.Description") < descriptor.index(
        "StandardAttribute.Code"
    )
    for category in ("Object", "Ref", "Selection", "List", "Manager"):
        assert f'category="{category}"' in descriptor
        assert f'name="Catalog{category}.ТестовыйСправочник"' in descriptor
    checked = check_metadata_artifacts(
        "Справочник.ТестовыйСправочник", "2.20", first["artifacts"]
    )
    assert checked["status"] == "passed"


def test_compiler_result_напрямую_переходит_в_checker_без_записи():
    compiled = compile_metadata_object(_catalog_specification())
    original_artifacts = [dict(item) for item in compiled["artifacts"]]

    checked = check_metadata_artifacts(
        compiled["object_ref"],
        compiled["format_version"],
        compiled["artifacts"],
    )

    assert checked["status"] == "passed"
    assert set(checked["coverage"].values()) == {"passed"}
    assert compiled["artifacts"] == original_artifacts
    assert "Configuration.xml" not in {
        item["path"] for item in compiled["artifacts"]
    }


@pytest.mark.parametrize("value", [None, "", "2", "v2.20", "2.x"])
def test_compiler_требует_корректную_format_version(value):
    specification = _catalog_specification()
    if value is None:
        specification.pop("format_version")
    else:
        specification["format_version"] = value

    with pytest.raises(MetadataAuthoringContractError) as caught:
        compile_metadata_object(specification)

    assert {item["code"] for item in caught.value.diagnostics} <= {
        "missing_required_field", "invalid_value", "invalid_format_version"
    }


def test_compiler_использует_format_version_во_всех_descriptor():
    specification = _register_specification()
    specification["format_version"] = "2.20.1"
    specification["forms"][0]["form_xml"] = specification["forms"][0][
        "form_xml"
    ].replace('version="2.20"', 'version="2.20.1"')

    result = compile_metadata_object(specification)

    assert result["format_version"] == "2.20.1"
    for artifact in result["artifacts"]:
        if artifact["path"].endswith(".xml") and not artifact["path"].endswith(
            "/Ext/Form.xml"
        ):
            assert 'version="2.20.1"' in artifact["content"]


def test_compiler_отклоняет_form_xml_с_чужой_версией():
    specification = _register_specification()
    specification["forms"][0]["form_xml"] = specification["forms"][0][
        "form_xml"
    ].replace('version="2.20"', 'version="2.16"')

    with pytest.raises(MetadataAuthoringContractError) as caught:
        compile_metadata_object(specification)

    assert ("form_format_version_mismatch", "$specification.forms[0].form_xml") in {
        (item["code"], item["path"]) for item in caught.value.diagnostics
    }


def test_checker_отклоняет_configuration_xml_как_внешний_артефакт():
    compiled = compile_metadata_object(_catalog_specification())
    artifacts = {
        item["path"]: item["content"] for item in compiled["artifacts"]
    }
    artifacts["Configuration.xml"] = "<MetaDataObject/>"

    checked = check_metadata_artifacts(
        compiled["object_ref"],
        compiled["format_version"],
        artifacts,
    )

    assert checked["status"] == "failed"
    assert checked["diagnostics"][0]["code"] == (
        "external_configuration_not_supported"
    )


@pytest.mark.parametrize(
    ("code_length", "description_length", "presentation", "input_fields"),
    [
        (0, 150, "AsDescription", ["Description"]),
        (9, 0, "AsCode", ["Code"]),
        (0, 0, "AsCode", []),
        (9, 150, "AsDescription", ["Description", "Code"]),
    ],
)
def test_catalog_compiler_учитывает_доступность_стандартных_реквизитов(
    code_length,
    description_length,
    presentation,
    input_fields,
):
    specification = _catalog_specification()
    specification["code_length"] = code_length
    specification["description_length"] = description_length

    result = compile_metadata_object(specification)

    descriptor = result["artifacts"][0]["content"]
    assert f"<CodeLength>{code_length}</CodeLength>" in descriptor
    assert f"<DescriptionLength>{description_length}</DescriptionLength>" in descriptor
    assert f"<DefaultPresentation>{presentation}</DefaultPresentation>" in descriptor
    input_by_string = descriptor.split("<InputByString>", 1)[1].split(
        "</InputByString>", 1
    )[0]
    assert [
        field
        for field in ("Description", "Code")
        if f"StandardAttribute.{field}" in input_by_string
    ] == input_fields


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("code_length", -1),
        ("code_length", 51),
        ("description_length", -1),
        ("description_length", 151),
        ("description_length", True),
    ],
)
def test_catalog_compiler_отклоняет_некорректные_длины(field, value):
    specification = _catalog_specification()
    specification[field] = value

    with pytest.raises(MetadataAuthoringContractError) as caught:
        compile_metadata_object(specification)

    assert ("invalid_value", f"$specification.{field}") in {
        (item["code"], item["path"]) for item in caught.value.diagnostics
    }


@pytest.mark.parametrize("field", ["code_length", "description_length"])
def test_catalog_compiler_требует_явную_длину_стандартного_поля(field):
    specification = _catalog_specification()
    specification.pop(field)

    with pytest.raises(MetadataAuthoringContractError) as caught:
        compile_metadata_object(specification)

    assert ("missing_required_field", f"$specification.{field}") in {
        (item["code"], item["path"]) for item in caught.value.diagnostics
    }


def test_catalog_compiler_отклоняет_основную_форму_без_стандартных_полей():
    specification = _catalog_specification()
    specification["forms"] = [
        {
            "name": "ФормаЭлемента",
            "synonym": "Форма элемента",
            "default": True,
            "form_xml": '<Form xmlns="http://v8.1c.ru/8.3/xcf/logform" version="2.20"/>',
            "module_bsl": "",
        }
    ]

    with pytest.raises(MetadataAuthoringContractError) as caught:
        compile_metadata_object(specification)

    assert [item["code"] for item in caught.value.diagnostics] == [
        "required_standard_field_missing",
        "required_standard_field_missing",
    ]
    assert "Объект.Наименование" in caught.value.diagnostics[0]["message"]
    assert "Объект.Код" in caught.value.diagnostics[1]["message"]


def test_catalog_compiler_принимает_стандартные_поля_основной_формы():
    specification = _catalog_specification()
    specification["forms"] = [
        {
            "name": "ФормаЭлемента",
            "synonym": "Форма элемента",
            "default": True,
            "form_xml": (
                '<Form xmlns="http://v8.1c.ru/8.3/xcf/logform" version="2.20">'
                "<ChildItems>"
                "<InputField><DataPath>Объект.Наименование</DataPath></InputField>"
                "<InputField><DataPath>Объект.Код</DataPath></InputField>"
                "</ChildItems></Form>"
            ),
            "module_bsl": "",
        }
    ]

    result = compile_metadata_object(specification)

    assert result["status"] == "compiled"
    assert result["diagnostics"] == []


def test_catalog_compiler_отклоняет_тени_реквизитов_объекта_в_form_xml():
    specification = _catalog_specification()
    specification["forms"] = [
        {
            "name": "ФормаЭлемента",
            "synonym": "Форма элемента",
            "default": True,
            "form_xml": (
                '<Form xmlns="http://v8.1c.ru/8.3/xcf/logform" version="2.20">'
                "<ChildItems><InputField><DataPath>Объект.Артикул</DataPath>"
                "</InputField></ChildItems><Attributes>"
                '<Attribute name="Объект"><MainAttribute>true'
                "</MainAttribute></Attribute>"
                '<Attribute name="Артикул"/>'
                "</Attributes></Form>"
            ),
            "module_bsl": "",
        }
    ]

    with pytest.raises(MetadataAuthoringContractError) as caught:
        compile_metadata_object(specification)

    assert "shadowed_main_object_attribute" in {
        item["code"] for item in caught.value.diagnostics
    }


def test_register_compiler_упаковывает_forms_и_проходит_checker():
    result = compile_metadata_object(_register_specification())

    assert result["status"] == "compiled"
    artifacts = {item["path"]: item["content"] for item in result["artifacts"]}
    assert set(artifacts) == {
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
        result["object_ref"],
        result["format_version"],
        result["artifacts"],
    )
    assert checked["status"] == "passed"


def test_compiler_не_создаёт_bundle_за_пределами_checker():
    specification = _catalog_specification()
    specification["forms"] = [
        {
            "name": "ФормаЭлемента",
            "synonym": "Форма элемента",
            "default": True,
            "form_xml": '<Form xmlns="http://v8.1c.ru/8.3/xcf/logform" version="2.20"/>',
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
