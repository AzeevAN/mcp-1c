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
        '<ChildItems><InputField><DataPath>Объект.Number</DataPath></InputField>'
        '<InputField><DataPath>Объект.Date</DataPath></InputField></ChildItems>'
        '<Attributes><Attribute name="Объект" id="1"><Type>'
        '<v8:Type>cfg:DocumentObject.Ролевой</v8:Type></Type>'
        '<MainAttribute>true</MainAttribute></Attribute></Attributes></Form>'
    )
    return form


def _document_collection_form(role: str) -> dict[str, object]:
    form = _form("ФормаСписка" if role == "list" else "ФормаВыбора", role, default=True)
    form["form_xml"] = str(form["form_xml"]).replace(
        "/>\n" if str(form["form_xml"]).endswith("/>\n") else "/>",
        "><ChildItems><InputField><DataPath>Список.Date</DataPath></InputField>"
        "<InputField><DataPath>Список.Number</DataPath></InputField></ChildItems></Form>",
    )
    return form


def _register_form(name: str, role: str | None, *, default: bool) -> dict[str, object]:
    form = _form(name, role, default=default)
    if role in {None, "record"}:
        data_path = "Запись.Значение"
    elif role == "list":
        data_path = "Список.Значение"
    else:
        return form
    form["form_xml"] = str(form["form_xml"]).replace(
        "/>",
        f"><ChildItems><InputField><DataPath>{data_path}</DataPath>"
        "</InputField></ChildItems></Form>",
    )
    return form


def _closed_profile_form(
    owner_kind: str, role: str, *, name: str, default: bool = True
) -> dict[str, object]:
    form = _form(name, role, default=default)
    if role in {"list", "choice"}:
        choice_properties = (
            "<WindowOpeningMode>LockOwnerWindow</WindowOpeningMode>"
            "<ChoiceMode>true</ChoiceMode>"
            if role == "choice"
            else ""
        )
        main_name = "Список"
        main_type = "cfg:DynamicList"
        saved_data = ""
    elif owner_kind == "РегистрСведений" and role == "record":
        choice_properties = ""
        main_name = "Запись"
        main_type = "cfg:InformationRegisterRecordManager.Ролевой"
        saved_data = "<SavedData>true</SavedData>"
    elif role == "object" and owner_kind in {"Справочник", "Документ"}:
        choice_properties = ""
        main_name = "Объект"
        xml_kind = "CatalogObject" if owner_kind == "Справочник" else "DocumentObject"
        main_type = f"cfg:{xml_kind}.Ролевой"
        saved_data = ""
    else:
        raise AssertionError((owner_kind, role))
    if owner_kind == "Документ" and role in {"object", "list", "choice"}:
        prefix = "Объект" if role == "object" else "Список"
        data_paths = [f"{prefix}.Date", f"{prefix}.Number"]
    elif role == "object":
        data_paths = []
    elif role == "record":
        data_paths = ["Запись.Значение"]
    else:
        data_paths = ["Список.Значение"]
    child_items = "".join(
        f"<InputField><DataPath>{data_path}</DataPath></InputField>"
        for data_path in data_paths
    )
    form["form_xml"] = (
        '<Form xmlns="http://v8.1c.ru/8.3/xcf/logform" '
        'xmlns:cfg="http://v8.1c.ru/8.1/data/enterprise/current-config" '
        'xmlns:v8="http://v8.1c.ru/8.1/data/core" version="2.20">'
        f"{choice_properties}<ChildItems>{child_items}</ChildItems><Attributes>"
        f'<Attribute name="{main_name}" id="1"><Type><v8:Type>{main_type}</v8:Type>'
        f"</Type><MainAttribute>true</MainAttribute>{saved_data}</Attribute>"
        "</Attributes></Form>"
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
    assert len(compiled["artifacts"]) == 7
    assert check_metadata_artifacts(
        compiled["object_ref"], compiled["format_version"], compiled["artifacts"]
    )["status"] == "passed"


def test_legacy_register_form_without_role_remains_record():
    specification = _register()
    specification["forms"] = [_register_form("ФормаЗаписи", None, default=True)]

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
        _register_form("ФормаЗаписи", "record", default=True),
        _register_form("ФормаСписка", "list", default=True),
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
    assert len(compiled["artifacts"]) == 7
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
        _document_collection_form("list"),
        _document_collection_form("choice"),
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
    if specification["object_ref"].startswith("Документ."):
        specification["forms"] = [
            _document_collection_form("list"),
            _document_collection_form("choice"),
        ]
    else:
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


@pytest.mark.parametrize("role", ["object", "list", "choice"])
def test_represented_catalog_role_requires_default(role):
    specification = _catalog()
    specification["forms"] = [_form("Форма", role, default=False)]

    with pytest.raises(MetadataAuthoringContractError) as caught:
        compile_metadata_object(specification)

    assert caught.value.diagnostics == [{
        "status": "failed",
        "code": "missing_default_form_role",
        "path": "$specification.forms[0].default",
        "message": f"Для представленной роли `{role}` требуется ровно одна форма с default=true.",
    }]


@pytest.mark.parametrize(
    ("factory", "role"),
    [(_document, "list"), (_register, "record"), (_register, "list")],
)
def test_represented_document_or_register_role_requires_default(factory, role):
    specification = factory()
    specification["forms"] = [_form("Форма", role, default=False)]

    with pytest.raises(MetadataAuthoringContractError) as caught:
        compile_metadata_object(specification)

    assert caught.value.diagnostics[0]["code"] == "missing_default_form_role"
    assert caught.value.diagnostics[0]["path"] == "$specification.forms[0].default"


def test_additional_nondefault_form_of_same_role_is_allowed():
    specification = _catalog()
    specification["forms"] = [
        _form("ФормаСписка", "list", default=True),
        _form("ФормаСпискаДополнительная", "list", default=False),
    ]

    compiled = compile_metadata_object(specification)
    descriptor = compiled["artifacts"][0]["content"]

    assert descriptor.count("<DefaultListForm>") == 1
    assert "Catalog.Ролевой.Form.ФормаСписка</DefaultListForm>" in descriptor
    assert "ФормаСпискаДополнительная</DefaultListForm>" not in descriptor
    assert check_metadata_artifacts(
        compiled["object_ref"], compiled["format_version"], compiled["artifacts"]
    )["status"] == "passed"


def test_absent_role_default_property_may_remain_empty():
    specification = _catalog()
    specification["forms"] = [_form("ФормаСписка", "list", default=True)]

    compiled = compile_metadata_object(specification)
    descriptor = compiled["artifacts"][0]["content"]

    assert "<DefaultObjectForm></DefaultObjectForm>" in descriptor
    assert "<DefaultChoiceForm></DefaultChoiceForm>" in descriptor
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
    choice_properties = (
        "<WindowOpeningMode>LockOwnerWindow</WindowOpeningMode>"
        "<ChoiceMode>true</ChoiceMode>"
        if role == "choice"
        else ""
    )
    form["form_xml"] = (
        '<Form xmlns="http://v8.1c.ru/8.3/xcf/logform" '
        'xmlns:cfg="http://v8.1c.ru/8.1/data/enterprise/current-config" '
        'xmlns:v8="http://v8.1c.ru/8.1/data/core" version="2.20">'
        f"{choice_properties}"
        '<Attributes><Attribute name="Список" id="1"><Type>'
        '<v8:Type>cfg:DynamicList</v8:Type></Type>'
        '<MainAttribute>true</MainAttribute></Attribute></Attributes>'
        '<ChildItems><InputField><DataPath>Список.Date</DataPath></InputField>'
        '<InputField><DataPath>Список.Number</DataPath></InputField>'
        '</ChildItems></Form>'
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


@pytest.mark.parametrize(
    ("factory", "owner_kind", "role", "property_name", "form_name"),
    [
        (_catalog, "Справочник", "object", "DefaultObjectForm", "ФормаОбъекта"),
        (_catalog, "Справочник", "list", "DefaultListForm", "ФормаСписка"),
        (_catalog, "Справочник", "choice", "DefaultChoiceForm", "ФормаВыбора"),
        (_document, "Документ", "object", "DefaultObjectForm", "ФормаОбъекта"),
        (_document, "Документ", "list", "DefaultListForm", "ФормаСписка"),
        (_document, "Документ", "choice", "DefaultChoiceForm", "ФормаВыбора"),
        (_register, "РегистрСведений", "record", "DefaultRecordForm", "ФормаЗаписи"),
        (_register, "РегистрСведений", "list", "DefaultListForm", "ФормаСписка"),
    ],
)
def test_checker_rejects_empty_default_for_structurally_supported_role(
    factory, owner_kind, role, property_name, form_name
):
    specification = factory()
    specification["forms"] = [
        _closed_profile_form(owner_kind, role, name=form_name)
    ]
    compiled = compile_metadata_object(specification)
    artifacts = deepcopy(compiled["artifacts"])
    descriptor = artifacts[0]["content"]
    start = descriptor.index(f"<{property_name}>") + len(property_name) + 2
    end = descriptor.index(f"</{property_name}>", start)
    artifacts[0]["content"] = descriptor[:start] + descriptor[end:]

    checked = check_metadata_artifacts(
        compiled["object_ref"], compiled["format_version"], artifacts
    )

    assert checked["status"] == "failed"
    assert any(
        item["code"] == "missing_default_form_role"
        and item["path"].endswith(f":{property_name}")
        for item in checked["diagnostics"]
    )


def test_metadata_authoring_has_no_direct_forms_dependency():
    root = Path(__file__).resolve().parents[3]
    package = root / "src/mcp1c/capability_modules/metadata_authoring"

    source = "\n".join(path.read_text(encoding="utf-8") for path in package.glob("*.py"))

    assert "capability_modules.forms" not in source
    assert "from ..forms" not in source
