from __future__ import annotations

from copy import deepcopy

import pytest
from jsonschema import Draft202012Validator

from metadata_core.checker import check_metadata_artifacts
from metadata_core.compiler import (
    MetadataAuthoringContractError,
    compile_metadata_object,
)
from metadata_core.schema import (
    METADATA_SPECIFICATION_SCHEMA,
)


def _document(*, with_form: bool = False) -> dict[str, object]:
    forms: list[dict[str, object]] = []
    if with_form:
        forms.append(
            {
                "name": "ФормаДокумента",
                "synonym": "Форма документа",
                "default": True,
                "form_xml": (
                    '<Form xmlns="http://v8.1c.ru/8.3/xcf/logform" '
                    'xmlns:cfg="http://v8.1c.ru/8.1/data/enterprise/current-config" '
                    'version="2.20">'
                    '<ChildItems><InputField name="Номер" id="1">'
                    '<DataPath>Объект.Number</DataPath></InputField>'
                    '<InputField name="Дата" id="4">'
                    '<DataPath>Объект.Date</DataPath></InputField>'
                    '<InputField name="Основание" id="7">'
                    '<DataPath>Объект.Основание</DataPath></InputField></ChildItems>'
                    '<Attributes><Attribute name="Объект" id="1">'
                    '<Type><v8:Type xmlns:v8="http://v8.1c.ru/8.1/data/core">'
                    'cfg:DocumentObject.ТестовыйДокумент</v8:Type></Type>'
                    '<MainAttribute>true</MainAttribute>'
                    '</Attribute></Attributes></Form>'
                ),
                "module_bsl": "",
            }
        )
    return {
        "schema_version": 1,
        "object_ref": "Документ.ТестовыйДокумент",
        "format_version": "2.20",
        "identity": "60000000-0000-0000-0000-000000000001",
        "synonym": "Тестовый документ",
        "number_length": 11,
        "number_allowed_length": "Variable",
        "number_periodicity": "Nonperiodical",
        "check_unique": True,
        "autonumbering": True,
        "posting": "Deny",
        "real_time_posting": "Deny",
        "attributes": [
            {
                "name": "Основание",
                "synonym": "Основание",
                "type": {
                    "kind": "document_ref",
                    "object": "Документ.ДокументОснование",
                },
            }
        ],
        "forms": forms,
    }


@pytest.mark.parametrize("with_form", [False, True])
def test_document_schema_compiler_checker_green(with_form):
    specification = _document(with_form=with_form)
    errors = list(
        Draft202012Validator(METADATA_SPECIFICATION_SCHEMA).iter_errors(specification)
    )
    assert errors == []

    first = compile_metadata_object(specification)
    second = compile_metadata_object(specification)
    assert first == second
    artifacts = {item["path"]: item["content"] for item in first["artifacts"]}
    assert "Documents/ТестовыйДокумент.xml" in artifacts
    descriptor = artifacts["Documents/ТестовыйДокумент.xml"]
    for category in ("Object", "Ref", "Selection", "List", "Manager"):
        assert f'category="{category}"' in descriptor
        assert f'name="Document{category}.ТестовыйДокумент"' in descriptor
    for standard in ("Posted", "Ref", "DeletionMark", "Date", "Number"):
        assert f'<xr:StandardAttribute name="{standard}">' in descriptor
    assert "cfg:DocumentRef.ДокументОснование" in descriptor
    assert "Document.ТестовыйДокумент.StandardAttribute.Number" in descriptor
    assert "<Posting>Deny</Posting>" in descriptor
    assert "<RealTimePosting>Deny</RealTimePosting>" in descriptor
    expected_default = (
        "<DefaultObjectForm>Document.ТестовыйДокумент.Form.ФормаДокумента</DefaultObjectForm>"
        if with_form
        else "<DefaultObjectForm></DefaultObjectForm>"
    )
    assert expected_default in descriptor
    if with_form:
        assert (
            "Documents/ТестовыйДокумент/Forms/ФормаДокумента/Ext/Form/Module.bsl"
            not in artifacts
        )
    checked = check_metadata_artifacts(
        first["object_ref"], first["format_version"], first["artifacts"]
    )
    assert checked["status"] == "passed", checked["diagnostics"]


@pytest.mark.parametrize(
    "data_path",
    ["Объект.Number", "Объект.Date", "Объект.Основание"],
)
def test_document_default_object_form_requires_configurator_fields(data_path):
    specification = _document(with_form=True)
    specification["forms"][0]["form_xml"] = specification["forms"][0][
        "form_xml"
    ].replace(data_path, "Объект.Пропущено")

    with pytest.raises(MetadataAuthoringContractError) as caught:
        compile_metadata_object(specification)

    assert caught.value.diagnostics[0]["code"] == "required_standard_field_missing"
    assert data_path in caught.value.diagnostics[0]["message"]


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("number_length", 0),
        ("number_length", 51),
        ("number_allowed_length", "Unknown"),
        ("number_periodicity", "Month"),
        ("check_unique", "true"),
        ("autonumbering", 1),
        ("posting", "Allow"),
        ("real_time_posting", "Allow"),
    ],
)
def test_document_schema_and_runtime_fail_closed(field, value):
    specification = _document()
    specification[field] = value
    schema_errors = list(
        Draft202012Validator(METADATA_SPECIFICATION_SCHEMA).iter_errors(specification)
    )
    assert schema_errors
    with pytest.raises(MetadataAuthoringContractError):
        compile_metadata_object(specification)


@pytest.mark.parametrize(
    ("replace_from", "replace_to", "expected_code"),
    [
        ("<Posting>Deny</Posting>", "<Posting>Allow</Posting>", "unsupported_document_posting"),
        (
            "Document.ТестовыйДокумент.Form.ФормаДокумента",
            "Document.ЧужойДокумент.Form.ФормаДокумента",
            "unknown_default_form",
        ),
    ],
)
def test_document_checker_rejects_structural_drift(
    replace_from, replace_to, expected_code
):
    compiled = compile_metadata_object(_document(with_form=True))
    artifacts = {item["path"]: item["content"] for item in compiled["artifacts"]}
    path = next(path for path, text in artifacts.items() if replace_from in text)
    artifacts[path] = artifacts[path].replace(replace_from, replace_to, 1)

    checked = check_metadata_artifacts(
        compiled["object_ref"], compiled["format_version"], artifacts
    )

    assert expected_code in {item["code"] for item in checked["diagnostics"]}


def test_document_checker_rejects_foreign_document_object_owner():
    compiled = compile_metadata_object(_document(with_form=True))
    artifacts = {item["path"]: item["content"] for item in compiled["artifacts"]}
    path = "Documents/ТестовыйДокумент/Forms/ФормаДокумента/Ext/Form.xml"
    artifacts[path] = artifacts[path].replace(
        "cfg:DocumentObject.ТестовыйДокумент",
        "cfg:DocumentObject.ЧужойДокумент",
    )

    checked = check_metadata_artifacts(
        compiled["object_ref"], compiled["format_version"], artifacts
    )

    assert checked["status"] == "failed"
    assert "form_owner_mismatch" in {
        item["code"] for item in checked["diagnostics"]
    }


def test_document_checker_requires_owner_type_for_default_object_form():
    compiled = compile_metadata_object(_document(with_form=True))
    artifacts = {item["path"]: item["content"] for item in compiled["artifacts"]}
    path = "Documents/ТестовыйДокумент/Forms/ФормаДокумента/Ext/Form.xml"
    artifacts[path] = artifacts[path].replace(
        "cfg:DocumentObject.ТестовыйДокумент",
        "cfg:DynamicList",
    )

    checked = check_metadata_artifacts(
        compiled["object_ref"], compiled["format_version"], artifacts
    )

    assert checked["status"] == "failed"
    assert "form_owner_mismatch" in {
        item["code"] for item in checked["diagnostics"]
    }


def test_document_checker_rejects_wrong_form_path():
    compiled = compile_metadata_object(_document(with_form=True))
    artifacts = {item["path"]: item["content"] for item in compiled["artifacts"]}
    source = "Documents/ТестовыйДокумент/Forms/ФормаДокумента/Ext/Form.xml"
    artifacts["Documents/ТестовыйДокумент/Forms/Чужая/Ext/Form.xml"] = artifacts.pop(source)

    checked = check_metadata_artifacts(
        compiled["object_ref"], compiled["format_version"], artifacts
    )

    assert checked["status"] == "failed"
    assert {item["code"] for item in checked["diagnostics"]} >= {
        "missing_form_xml",
        "unexpected_artifact",
    }


def test_document_compiler_rejects_wrong_embedded_form_owner():
    specification = _document(with_form=True)
    specification["forms"][0]["form_xml"] = specification["forms"][0][
        "form_xml"
    ].replace(
        "cfg:DocumentObject.ТестовыйДокумент",
        "cfg:DocumentObject.ЧужойДокумент",
    )

    with pytest.raises(MetadataAuthoringContractError) as caught:
        compile_metadata_object(specification)

    assert caught.value.diagnostics[0]["code"] == "form_owner_mismatch"


def test_document_ref_does_not_widen_other_object_kinds():
    specification = _document()
    bad = deepcopy(specification)
    bad["attributes"][0]["type"]["object"] = "Отчет.Чужой"
    assert list(Draft202012Validator(METADATA_SPECIFICATION_SCHEMA).iter_errors(bad))
    with pytest.raises(MetadataAuthoringContractError) as caught:
        compile_metadata_object(bad)
    assert "invalid_value" in {item["code"] for item in caught.value.diagnostics}
