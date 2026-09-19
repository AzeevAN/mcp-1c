from __future__ import annotations

from copy import deepcopy
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator

from mcp1c.capability_modules.metadata_authoring.checker import (
    check_metadata_artifacts,
)
from mcp1c.capability_modules.metadata_authoring.compiler import (
    MetadataAuthoringContractError,
    compile_metadata_object,
)
from mcp1c.capability_modules.metadata_authoring.schema import (
    METADATA_SPECIFICATION_SCHEMA,
)


def _form(name: str, role: str | None, *, default: bool) -> dict[str, object]:
    item: dict[str, object] = {
        "name": name,
        "synonym": name,
        "default": default,
        "form_xml": (
            '<Form xmlns="http://v8.1c.ru/8.3/xcf/logform" '
            'xmlns:cfg="http://v8.1c.ru/8.1/data/enterprise/current-config" '
            'xmlns:v8="http://v8.1c.ru/8.1/data/core" version="2.20"/>'
        ),
        "module_bsl": "",
    }
    if role is not None:
        item["role"] = role
    return item


def _catalog() -> dict[str, object]:
    return {
        "schema_version": 1,
        "object_ref": "Справочник.Ролевой",
        "format_version": "2.20",
        "identity": "71000000-0000-0000-0000-000000000001",
        "synonym": "Ролевой",
        "code_length": 0,
        "description_length": 0,
        "attributes": [],
        "forms": [],
    }


def _document() -> dict[str, object]:
    return {
        "schema_version": 1,
        "object_ref": "Документ.Ролевой",
        "format_version": "2.20",
        "identity": "72000000-0000-0000-0000-000000000001",
        "synonym": "Ролевой",
        "number_length": 11,
        "number_allowed_length": "Variable",
        "number_periodicity": "Nonperiodical",
        "check_unique": True,
        "autonumbering": True,
        "posting": "Deny",
        "real_time_posting": "Deny",
        "attributes": [],
        "forms": [],
    }


def _register() -> dict[str, object]:
    return {
        "schema_version": 1,
        "object_ref": "РегистрСведений.Ролевой",
        "format_version": "2.20",
        "identity": "73000000-0000-0000-0000-000000000001",
        "synonym": "Ролевой",
        "periodicity": "nonperiodical",
        "dimensions": [],
        "resources": [
            {
                "name": "Значение",
                "synonym": "Значение",
                "type": {"kind": "boolean"},
            }
        ],
        "attributes": [],
        "forms": [],
    }


def _document_object_form(*, default: bool = True) -> dict[str, object]:
    form = _form("ФормаОбъекта", "object", default=default)
    form["form_xml"] = (
        '<Form xmlns="http://v8.1c.ru/8.3/xcf/logform" '
        'xmlns:cfg="http://v8.1c.ru/8.1/data/enterprise/current-config" '
        'xmlns:v8="http://v8.1c.ru/8.1/data/core" version="2.20">'
        '<Attributes><Attribute name="Объект" id="1"><Type>'
        '<v8:Type>cfg:DocumentObject.Ролевой</v8:Type></Type>'
        '<MainAttribute>true</MainAttribute></Attribute></Attributes></Form>'
    )
    return form


@pytest.mark.parametrize("factory", [_catalog, _document])
def test_legacy_form_without_role_remains_object(factory):
    specification = factory()
    legacy = _form("ФормаОбъекта", None, default=True)
    if specification["object_ref"].startswith("Документ."):
        legacy = _document_object_form()
        legacy.pop("role")
    specification["forms"] = [legacy]

    compiled = compile_metadata_object(specification)
    descriptor = compiled["artifacts"][0]["content"]

    assert "<DefaultObjectForm>" in descriptor
    assert ".Form.ФормаОбъекта</DefaultObjectForm>" in descriptor
    assert check_metadata_artifacts(
        compiled["object_ref"], compiled["format_version"], compiled["artifacts"]
    )["status"] == "passed"


def test_catalog_object_list_choice_defaults_compile_and_check():
    specification = _catalog()
    specification["forms"] = [
        _form("ФормаОбъекта", "object", default=True),
        _form("ФормаСписка", "list", default=True),
        _form("ФормаВыбора", "choice", default=True),
    ]

    assert list(
        Draft202012Validator(METADATA_SPECIFICATION_SCHEMA).iter_errors(specification)
    ) == []
    compiled = compile_metadata_object(specification)
    descriptor = compiled["artifacts"][0]["content"]

    for property_name, form_name in (
        ("DefaultObjectForm", "ФормаОбъекта"),
        ("DefaultListForm", "ФормаСписка"),
        ("DefaultChoiceForm", "ФормаВыбора"),
    ):
        assert (
            f"<{property_name}>Catalog.Ролевой.Form.{form_name}</{property_name}>"
            in descriptor
        )
    assert len(compiled["artifacts"]) == 10
    assert check_metadata_artifacts(
        compiled["object_ref"], compiled["format_version"], compiled["artifacts"]
    )["status"] == "passed"


def test_legacy_register_form_without_role_remains_record():
    specification = _register()
    specification["forms"] = [_form("ФормаЗаписи", None, default=True)]

    compiled = compile_metadata_object(specification)
    descriptor = compiled["artifacts"][0]["content"]

    assert (
        "<DefaultRecordForm>InformationRegister.Ролевой.Form.ФормаЗаписи"
        "</DefaultRecordForm>"
    ) in descriptor
    assert check_metadata_artifacts(
        compiled["object_ref"], compiled["format_version"], compiled["artifacts"]
    )["status"] == "passed"


def test_information_register_list_default_and_record_set_compile_and_check():
    specification = _register()
    specification["forms"] = [
        _form("ФормаЗаписи", "record", default=True),
        _form("ФормаСписка", "list", default=True),
        _form("ФормаНабораЗаписей", "record_set", default=False),
    ]

    assert list(
        Draft202012Validator(METADATA_SPECIFICATION_SCHEMA).iter_errors(
            specification
        )
    ) == []
    compiled = compile_metadata_object(specification)
    descriptor = compiled["artifacts"][0]["content"]

    assert (
        "<DefaultRecordForm>InformationRegister.Ролевой.Form.ФормаЗаписи"
        "</DefaultRecordForm>"
    ) in descriptor
    assert (
        "<DefaultListForm>InformationRegister.Ролевой.Form.ФормаСписка"
        "</DefaultListForm>"
    ) in descriptor
    assert "DefaultRecordSetForm" not in descriptor
    assert len(compiled["artifacts"]) == 10
    assert check_metadata_artifacts(
        compiled["object_ref"], compiled["format_version"], compiled["artifacts"]
    )["status"] == "passed"


def test_information_register_record_set_cannot_be_default():
    specification = _register()
    specification["forms"] = [
        _form("ФормаНабораЗаписей", "record_set", default=True)
    ]

    schema_errors = list(
        Draft202012Validator(METADATA_SPECIFICATION_SCHEMA).iter_errors(
            specification
        )
    )
    assert len(schema_errors) == 1
    assert any(
        error.validator == "const"
        and list(error.absolute_path)[-1:] == ["default"]
        for error in schema_errors[0].context
    )

    with pytest.raises(MetadataAuthoringContractError) as caught:
        compile_metadata_object(specification)

    assert caught.value.diagnostics[0]["code"] == "unsupported_default_form_role"


def test_checker_rejects_unproven_default_record_set_property():
    specification = _register()
    specification["forms"] = [
        _form("ФормаНабораЗаписей", "record_set", default=False)
    ]
    compiled = compile_metadata_object(specification)
    artifacts = deepcopy(compiled["artifacts"])
    artifacts[0]["content"] = artifacts[0]["content"].replace(
        "</DefaultRecordForm>",
        (
            "</DefaultRecordForm>"
            "<DefaultRecordSetForm>"
            "InformationRegister.Ролевой.Form.ФормаНабораЗаписей"
            "</DefaultRecordSetForm>"
        ),
    )

    report = check_metadata_artifacts(
        compiled["object_ref"], compiled["format_version"], artifacts
    )

    assert report["status"] == "failed"
    assert any(
        item["code"] == "unsupported_default_form_property"
        for item in report["diagnostics"]
    )


def test_document_object_list_choice_defaults_compile_and_check():
    specification = _document()
    specification["forms"] = [
        _document_object_form(),
        _form("ФормаСписка", "list", default=True),
        _form("ФормаВыбора", "choice", default=True),
    ]

    compiled = compile_metadata_object(specification)
    descriptor = compiled["artifacts"][0]["content"]

    assert "<DefaultObjectForm>Document.Ролевой.Form.ФормаОбъекта</DefaultObjectForm>" in descriptor
    assert "<DefaultListForm>Document.Ролевой.Form.ФормаСписка</DefaultListForm>" in descriptor
    assert "<DefaultChoiceForm>Document.Ролевой.Form.ФормаВыбора</DefaultChoiceForm>" in descriptor
    assert check_metadata_artifacts(
        compiled["object_ref"], compiled["format_version"], compiled["artifacts"]
    )["status"] == "passed"


@pytest.mark.parametrize("factory", [_catalog, _document])
def test_list_and_choice_without_object_are_allowed(factory):
    specification = factory()
    specification["forms"] = [
        _form("ФормаСписка", "list", default=True),
        _form("ФормаВыбора", "choice", default=True),
    ]

    compiled = compile_metadata_object(specification)
    descriptor = compiled["artifacts"][0]["content"]

    assert "<DefaultObjectForm></DefaultObjectForm>" in descriptor
    assert check_metadata_artifacts(
        compiled["object_ref"], compiled["format_version"], compiled["artifacts"]
    )["status"] == "passed"


def test_object_default_does_not_assign_nondefault_list_and_choice():
    specification = _catalog()
    specification["forms"] = [
        _form("ФормаОбъекта", "object", default=True),
        _form("ФормаСписка", "list", default=False),
        _form("ФормаВыбора", "choice", default=False),
    ]

    descriptor = compile_metadata_object(specification)["artifacts"][0]["content"]

    assert "<DefaultListForm></DefaultListForm>" in descriptor
    assert "<DefaultChoiceForm></DefaultChoiceForm>" in descriptor


def test_zero_defaults_are_allowed():
    specification = _catalog()
    specification["forms"] = [
        _form("ФормаОбъекта", "object", default=False),
        _form("ФормаСписка", "list", default=False),
        _form("ФормаВыбора", "choice", default=False),
    ]

    compiled = compile_metadata_object(specification)

    assert check_metadata_artifacts(
        compiled["object_ref"], compiled["format_version"], compiled["artifacts"]
    )["status"] == "passed"


def test_duplicate_default_for_same_role_is_rejected():
    specification = _catalog()
    specification["forms"] = [
        _form("ФормаСписка1", "list", default=True),
        _form("ФормаСписка2", "list", default=True),
    ]

    with pytest.raises(MetadataAuthoringContractError) as caught:
        compile_metadata_object(specification)

    assert caught.value.diagnostics[0]["code"] == "duplicate_default_form_role"


@pytest.mark.parametrize(
    ("factory", "role", "code"),
    [
        (_catalog, "record", "unsupported_owner_role"),
        (_document, "record", "unsupported_owner_role"),
        (_register, "object", "unsupported_owner_role"),
        (_register, "choice", "unsupported_owner_role"),
        (_catalog, "folder", "unsupported_form_role"),
    ],
)
def test_invalid_or_unsupported_roles_are_rejected(factory, role, code):
    specification = factory()
    specification["forms"] = [_form("Форма", role, default=False)]

    with pytest.raises(MetadataAuthoringContractError) as caught:
        compile_metadata_object(specification)

    assert caught.value.diagnostics[0]["code"] == code


@pytest.mark.parametrize("role", ["list", "choice"])
def test_document_nonobject_roles_do_not_require_document_object(role):
    specification = _document()
    form = _form("Форма", role, default=True)
    form["form_xml"] = (
        '<Form xmlns="http://v8.1c.ru/8.3/xcf/logform" '
        'xmlns:cfg="http://v8.1c.ru/8.1/data/enterprise/current-config" '
        'xmlns:v8="http://v8.1c.ru/8.1/data/core" version="2.20">'
        '<Attributes><Attribute name="Список" id="1"><Type>'
        '<v8:Type>cfg:DynamicList</v8:Type></Type>'
        '<MainAttribute>true</MainAttribute></Attribute></Attributes></Form>'
    )
    specification["forms"] = [form]

    compiled = compile_metadata_object(specification)

    assert check_metadata_artifacts(
        compiled["object_ref"], compiled["format_version"], compiled["artifacts"]
    )["status"] == "passed"


def test_foreign_document_object_is_rejected_only_for_object_role():
    specification = _document()
    form = _document_object_form()
    form["form_xml"] = str(form["form_xml"]).replace(
        "DocumentObject.Ролевой", "DocumentObject.Чужой"
    )
    specification["forms"] = [form]

    with pytest.raises(MetadataAuthoringContractError) as caught:
        compile_metadata_object(specification)

    assert caught.value.diagnostics[0]["code"] == "form_owner_mismatch"


def test_compiler_rejects_undeclared_qname_prefix_in_form_xml():
    specification = _catalog()
    form = _form("ФормаСписка", "list", default=False)
    form["form_xml"] = (
        '<Form xmlns="http://v8.1c.ru/8.3/xcf/logform" version="2.20">'
        "<Attributes><Attribute><Type>cfg:DynamicList</Type></Attribute>"
        "</Attributes></Form>"
    )
    specification["forms"] = [form]

    with pytest.raises(MetadataAuthoringContractError) as caught:
        compile_metadata_object(specification)

    assert caught.value.diagnostics[0]["code"] == "undeclared_qname_prefix"


@pytest.mark.parametrize(
    "replacement",
    [
        "Catalog.Ролевой.Form.Отсутствует",
        "Catalog.Чужой.Form.ФормаСписка",
        "Document.Ролевой.Form.ФормаСписка",
    ],
)
def test_checker_rejects_broken_or_nonowner_default_reference(replacement):
    specification = _catalog()
    specification["forms"] = [_form("ФормаСписка", "list", default=True)]
    compiled = compile_metadata_object(specification)
    artifacts = {item["path"]: item["content"] for item in compiled["artifacts"]}
    owner = "Catalogs/Ролевой.xml"
    artifacts[owner] = artifacts[owner].replace(
        "Catalog.Ролевой.Form.ФормаСписка", replacement
    )

    checked = check_metadata_artifacts(
        compiled["object_ref"], compiled["format_version"], artifacts
    )

    assert checked["status"] == "failed"
    assert "unknown_default_form" in {
        item["code"] for item in checked["diagnostics"]
    }


def test_metadata_authoring_has_no_direct_forms_dependency():
    root = Path(__file__).resolve().parents[3]
    package = root / "src/mcp1c/capability_modules/metadata_authoring"

    source = "\n".join(path.read_text(encoding="utf-8") for path in package.glob("*.py"))

    assert "capability_modules.forms" not in source
    assert "from ..forms" not in source
