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
    assert first["checker_handoff"] == {
        "tool": "check_metadata_artifacts",
        "arguments_from_result": {
            "object_ref": "object_ref",
            "artifacts": "artifacts",
            "configuration_registration": "configuration_registration",
        },
        "required_external_argument": "configuration_xml",
        "configuration_xml": (
            "Текст существующего Configuration.xml целевой конфигурации; "
            "сервер изменяет только копию в памяти."
        ),
    }
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
        "Справочник.ТестовыйСправочник", _with_configuration(first)
    )
    assert checked["status"] == "passed"


def test_compiler_result_напрямую_переходит_в_checker_без_записи():
    compiled = compile_metadata_object(_catalog_specification())
    configuration_xml = (
        f'<MetaDataObject xmlns="{MD}"><Configuration><ChildObjects>'
        "<Catalog>Существующий</Catalog>"
        "</ChildObjects></Configuration></MetaDataObject>"
    )
    original_artifacts = [dict(item) for item in compiled["artifacts"]]

    checked = check_metadata_artifacts(
        compiled["object_ref"],
        compiled["artifacts"],
        configuration_xml=configuration_xml,
        configuration_registration=compiled["configuration_registration"],
    )

    assert checked["status"] == "passed"
    assert set(checked["coverage"].values()) == {"passed"}
    assert compiled["artifacts"] == original_artifacts
    assert "ТестовыйСправочник" not in configuration_xml


def test_checker_отклоняет_неполную_пару_configuration_handoff():
    compiled = compile_metadata_object(_catalog_specification())

    checked = check_metadata_artifacts(
        compiled["object_ref"],
        compiled["artifacts"],
        configuration_xml=(
            f'<MetaDataObject xmlns="{MD}"><Configuration><ChildObjects/>'
            "</Configuration></MetaDataObject>"
        ),
    )

    assert checked["status"] == "failed"
    assert checked["diagnostics"][0]["code"] == (
        "incomplete_configuration_handoff"
    )


def test_checker_отклоняет_чужую_регистрацию():
    compiled = compile_metadata_object(_catalog_specification())
    registration = dict(compiled["configuration_registration"])
    registration["value"] = "ЧужойСправочник"

    checked = check_metadata_artifacts(
        compiled["object_ref"],
        compiled["artifacts"],
        configuration_xml=(
            f'<MetaDataObject xmlns="{MD}"><Configuration><ChildObjects/>'
            "</Configuration></MetaDataObject>"
        ),
        configuration_registration=registration,
    )

    assert checked["status"] == "failed"
    assert checked["diagnostics"][0]["code"] == (
        "configuration_registration_mismatch"
    )


def test_checker_отклоняет_битый_configuration_xml():
    compiled = compile_metadata_object(_catalog_specification())

    checked = check_metadata_artifacts(
        compiled["object_ref"],
        compiled["artifacts"],
        configuration_xml="<broken>",
        configuration_registration=compiled["configuration_registration"],
    )

    assert checked["status"] == "failed"
    assert "invalid_xml" in {
        item["code"] for item in checked["diagnostics"]
    }
    assert set(checked["coverage"].values()) == {"failed"}


def test_checker_отклоняет_два_источника_configuration():
    compiled = compile_metadata_object(_catalog_specification())
    artifacts = {
        item["path"]: item["content"] for item in compiled["artifacts"]
    }
    artifacts["Configuration.xml"] = (
        f'<MetaDataObject xmlns="{MD}"><Configuration><ChildObjects/>'
        "</Configuration></MetaDataObject>"
    )

    checked = check_metadata_artifacts(
        compiled["object_ref"],
        artifacts,
        configuration_xml=artifacts["Configuration.xml"],
        configuration_registration=compiled["configuration_registration"],
    )

    assert checked["status"] == "failed"
    assert checked["diagnostics"][0]["code"] == (
        "conflicting_configuration_sources"
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


def test_catalog_compiler_предупреждает_о_стандартных_полях_основной_формы():
    specification = _catalog_specification()
    specification["forms"] = [
        {
            "name": "ФормаЭлемента",
            "synonym": "Форма элемента",
            "default": True,
            "form_xml": '<Form xmlns="http://v8.1c.ru/8.3/xcf/logform"/>',
            "module_bsl": "",
        }
    ]

    result = compile_metadata_object(specification)

    assert [item["code"] for item in result["diagnostics"]] == [
        "recommended_standard_field_missing",
        "recommended_standard_field_missing",
    ]
    assert "Объект.Наименование" in result["diagnostics"][0]["message"]
    assert "Объект.Код" in result["diagnostics"][1]["message"]


def test_catalog_compiler_отклоняет_тени_реквизитов_объекта_в_form_xml():
    specification = _catalog_specification()
    specification["forms"] = [
        {
            "name": "ФормаЭлемента",
            "synonym": "Форма элемента",
            "default": True,
            "form_xml": (
                '<Form xmlns="http://v8.1c.ru/8.3/xcf/logform">'
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
        result["object_ref"],
        result["artifacts"],
        configuration_xml=(
            f'<MetaDataObject xmlns="{MD}"><Configuration><ChildObjects/>'
            "</Configuration></MetaDataObject>"
        ),
        configuration_registration=result["configuration_registration"],
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
