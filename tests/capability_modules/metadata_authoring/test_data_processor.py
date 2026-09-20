from __future__ import annotations

from copy import deepcopy

import pytest
from jsonschema import Draft202012Validator

from mcp1c.capability_modules.forms.compiler import compile_managed_form
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


def _form_xml(owner: str = "Обработка.Импорт") -> tuple[str, str]:
    result = compile_managed_form(
        {
            "schema_version": 2,
            "context": {"owner": owner, "role": "object"},
            "form_name": "Форма",
            "format_version": "2.20",
            "title": {"ru": "Импорт"},
            "attributes": [
                {
                    "name": "Объект",
                    "type": {"kind": "metadata_object", "object": owner},
                    "main": True,
                },
                {
                    "name": "Параметр",
                    "type": {"kind": "string", "length": 100},
                },
            ],
            "elements": [
                {
                    "kind": "input_field",
                    "name": "Параметр",
                    "data_path": "Параметр",
                }
            ],
            "commands": [],
            "events": [],
        }
    )
    return result.artifacts[0].content, result.artifacts[1].content


def _specification() -> dict[str, object]:
    form_xml, module_bsl = _form_xml()
    return {
        "schema_version": 1,
        "object_ref": "Обработка.Импорт",
        "format_version": "2.20",
        "identity": "74000000-0000-0000-0000-000000000001",
        "synonym": "Импорт",
        "attributes": [],
        "forms": [
            {
                "name": "Форма",
                "synonym": "Форма",
                "role": "object",
                "default": True,
                "form_xml": form_xml,
                "module_bsl": module_bsl,
            }
        ],
    }


def _with_namespaced_command_container(content: str) -> str:
    namespace = "http://v8.1c.ru/8.3/xcf/logform"
    return content.replace(
        f'xmlns="{namespace}"',
        f'xmlns="{namespace}" xmlns:z="{namespace}"',
        1,
    ).replace(
        '<AutoCommandBar name="ФормаКоманднаяПанель" id="-1"/>',
        (
            '<z:AutoCommandBar name="ФормаКоманднаяПанель" id="-1">'
            "<z:Autofill>true</z:Autofill>"
            "</z:AutoCommandBar>"
        ),
        1,
    )


def test_data_processor_compile_and_exact_bundle_check():
    specification = _specification()
    assert list(
        Draft202012Validator(METADATA_SPECIFICATION_SCHEMA).iter_errors(
            specification
        )
    ) == []

    compiled = compile_metadata_object(specification)
    artifacts = {item["path"]: item["content"] for item in compiled["artifacts"]}
    descriptor = artifacts["DataProcessors/Импорт.xml"]

    assert 'name="DataProcessorObject.Импорт" category="Object"' in descriptor
    assert 'name="DataProcessorManager.Импорт" category="Manager"' in descriptor
    assert "DataProcessorRef.Импорт" not in descriptor
    assert (
        "<DefaultForm>DataProcessor.Импорт.Form.Форма</DefaultForm>"
        in descriptor
    )
    assert "DefaultObjectForm" not in descriptor
    assert set(artifacts) == {
        "DataProcessors/Импорт.xml",
        "DataProcessors/Импорт/Forms/Форма.xml",
        "DataProcessors/Импорт/Forms/Форма/Ext/Form.xml",
        "DataProcessors/Импорт/Forms/Форма/Ext/Form/Module.bsl",
    }

    checked = check_metadata_artifacts(
        compiled["object_ref"], compiled["format_version"], artifacts
    )
    assert checked["status"] == "passed"


@pytest.mark.parametrize(
    ("mutate", "code"),
    [
        (lambda value: value.update({"forms": []}), "missing_data_processor_form"),
        (
            lambda value: value["forms"][0].update({"role": "list"}),
            "unsupported_owner_role",
        ),
        (
            lambda value: value.update({"commands": []}),
            "unknown_field",
        ),
        (
            lambda value: value.update({"object_ref": "ВнешняяОбработка.Импорт"}),
            "unsupported_object_ref",
        ),
        (
            lambda value: value["attributes"].append(
                {
                    "name": "Параметр",
                    "synonym": "Параметр",
                    "type": {"kind": "string", "length": 10},
                }
            ),
            "unsupported_data_processor_attributes",
        ),
    ],
)
def test_data_processor_schema_and_runtime_fail_closed(mutate, code):
    specification = deepcopy(_specification())
    mutate(specification)

    assert list(
        Draft202012Validator(METADATA_SPECIFICATION_SCHEMA).iter_errors(
            specification
        )
    )
    with pytest.raises(MetadataAuthoringContractError) as caught:
        compile_metadata_object(specification)
    assert caught.value.diagnostics[0]["code"] == code


def test_data_processor_compiler_rejects_foreign_form_owner():
    specification = _specification()
    specification["forms"][0]["form_xml"] = specification["forms"][0][
        "form_xml"
    ].replace(
        "cfg:DataProcessorObject.Импорт",
        "cfg:DataProcessorObject.Чужая",
    )

    with pytest.raises(MetadataAuthoringContractError) as caught:
        compile_metadata_object(specification)

    assert caught.value.diagnostics[0]["code"] == "form_owner_mismatch"


@pytest.mark.parametrize(
    ("mutate", "code"),
    [
        (
            lambda form: form.update(
                {
                    "form_xml": form["form_xml"].replace(
                        'name="Объект"', 'name="Данные"', 1
                    )
                }
            ),
            "invalid_data_processor_main_attribute",
        ),
        (
            lambda form: form.update(
                {"module_bsl": form["module_bsl"] + "\r\nПроцедура Выполнить()\r\nКонецПроцедуры\r\n"}
            ),
            "unsupported_data_processor_module_bsl",
        ),
        (
            lambda form: form.update(
                {
                    "form_xml": form["form_xml"].replace(
                        "</Form>", "<Commands><Command/></Commands></Form>", 1
                    )
                }
            ),
            "unsupported_data_processor_form_behavior",
        ),
        (
            lambda form: form.update(
                {"form_xml": _with_namespaced_command_container(form["form_xml"])}
            ),
            "unsupported_data_processor_form_behavior",
        ),
        (
            lambda form: form.update(
                {
                    "form_xml": form["form_xml"].replace(
                        "</ChildItems>",
                        (
                            '<Button name="Записать" id="999">'
                            "<CommandName>Form.StandardCommand.Write</CommandName>"
                            "</Button></ChildItems>"
                        ),
                        1,
                    )
                }
            ),
            "unsupported_data_processor_form_behavior",
        ),
    ],
)
def test_data_processor_compiler_rejects_out_of_scope_form_behavior(mutate, code):
    specification = _specification()
    mutate(specification["forms"][0])

    with pytest.raises(MetadataAuthoringContractError) as caught:
        compile_metadata_object(specification)

    assert caught.value.diagnostics[0]["code"] == code


@pytest.mark.parametrize(
    ("path", "mutate", "code"),
    [
        (
            "DataProcessors/Импорт/Forms/Форма/Ext/Form.xml",
            lambda content: content.replace('name="Объект"', 'name="Данные"', 1),
            "invalid_data_processor_main_attribute",
        ),
        (
            "DataProcessors/Импорт/Forms/Форма/Ext/Form.xml",
            lambda content: content.replace(
                "</Form>", "<Events><Event/></Events></Form>", 1
            ),
            "unsupported_data_processor_form_behavior",
        ),
        (
            "DataProcessors/Импорт/Forms/Форма/Ext/Form.xml",
            _with_namespaced_command_container,
            "unsupported_data_processor_form_behavior",
        ),
        (
            "DataProcessors/Импорт/Forms/Форма/Ext/Form.xml",
            lambda content: content.replace(
                "</ChildItems>",
                (
                    '<Button name="Записать" id="999">'
                    "<CommandName>Form.StandardCommand.Write</CommandName>"
                    "</Button></ChildItems>"
                ),
                1,
            ),
            "unsupported_data_processor_form_behavior",
        ),
        (
            "DataProcessors/Импорт/Forms/Форма/Ext/Form/Module.bsl",
            lambda content: content + "\r\nПроцедура Выполнить()\r\nКонецПроцедуры\r\n",
            "unsupported_data_processor_module_bsl",
        ),
    ],
)
def test_data_processor_checker_rejects_out_of_scope_form_behavior(path, mutate, code):
    compiled = compile_metadata_object(_specification())
    artifacts = {item["path"]: item["content"] for item in compiled["artifacts"]}
    artifacts[path] = mutate(artifacts[path])

    checked = check_metadata_artifacts(
        compiled["object_ref"], compiled["format_version"], artifacts
    )

    assert checked["status"] == "failed"
    assert code in {item["code"] for item in checked["diagnostics"]}


@pytest.mark.parametrize(
    ("path", "replace_from", "replace_to", "code"),
    [
        (
            "DataProcessors/Импорт/Forms/Форма/Ext/Form.xml",
            "cfg:DataProcessorObject.Импорт",
            "cfg:DataProcessorObject.Чужая",
            "form_owner_mismatch",
        ),
        (
            "DataProcessors/Импорт.xml",
            "<Form>Форма</Form>",
            "<Template>Форма</Template>",
            "unsupported_data_processor_structure",
        ),
        (
            "DataProcessors/Импорт.xml",
            "<AuxiliaryForm/>",
            "<AuxiliaryForm>DataProcessor.Импорт.Form.Форма</AuxiliaryForm>",
            "unsupported_auxiliary_form",
        ),
        (
            "DataProcessors/Импорт.xml",
            "<AuxiliaryForm/>",
            "<DefaultObjectForm/><AuxiliaryForm/>",
            "unsupported_default_form_property",
        ),
    ],
)
def test_data_processor_checker_rejects_structural_drift(
    path, replace_from, replace_to, code
):
    compiled = compile_metadata_object(_specification())
    artifacts = {item["path"]: item["content"] for item in compiled["artifacts"]}
    artifacts[path] = artifacts[path].replace(replace_from, replace_to, 1)

    checked = check_metadata_artifacts(
        compiled["object_ref"], compiled["format_version"], artifacts
    )

    assert checked["status"] == "failed"
    assert code in {item["code"] for item in checked["diagnostics"]}


def test_data_processor_checker_rejects_extra_and_unsafe_artifacts():
    compiled = compile_metadata_object(_specification())
    artifacts = {item["path"]: item["content"] for item in compiled["artifacts"]}
    extra = deepcopy(artifacts)
    extra["DataProcessors/Импорт/Ext/ObjectModule.bsl"] = ""
    unsafe = deepcopy(artifacts)
    unsafe["../Импорт.xml"] = "<x/>"

    extra_result = check_metadata_artifacts(
        compiled["object_ref"], compiled["format_version"], extra
    )
    unsafe_result = check_metadata_artifacts(
        compiled["object_ref"], compiled["format_version"], unsafe
    )

    assert "unexpected_artifact" in {
        item["code"] for item in extra_result["diagnostics"]
    }
    assert "unsafe_artifact_path" in {
        item["code"] for item in unsafe_result["diagnostics"]
    }


def test_data_processor_checker_requires_declared_form():
    compiled = compile_metadata_object(_specification())
    artifacts = {item["path"]: item["content"] for item in compiled["artifacts"]}
    descriptor_path = "DataProcessors/Импорт.xml"
    artifacts[descriptor_path] = artifacts[descriptor_path].replace(
        "<Form>Форма</Form>", ""
    )
    artifacts = {
        path: content
        for path, content in artifacts.items()
        if "/Forms/" not in path
    }

    checked = check_metadata_artifacts(
        compiled["object_ref"], compiled["format_version"], artifacts
    )

    assert checked["status"] == "failed"
    assert "missing_data_processor_form" in {
        item["code"] for item in checked["diagnostics"]
    }


@pytest.mark.parametrize(
    ("mutate", "code"),
    [
        (
            lambda value: value["forms"].append(deepcopy(value["forms"][0])),
            "invalid_data_processor_form_count",
        ),
        (
            lambda value: value["forms"][0].update({"default": False}),
            "missing_default_data_processor_form",
        ),
    ],
)
def test_data_processor_requires_one_default_main_form(mutate, code):
    specification = deepcopy(_specification())
    mutate(specification)

    assert list(
        Draft202012Validator(METADATA_SPECIFICATION_SCHEMA).iter_errors(
            specification
        )
    )
    with pytest.raises(MetadataAuthoringContractError) as caught:
        compile_metadata_object(specification)
    assert caught.value.diagnostics[0]["code"] == code


def test_data_processor_checker_requires_default_form():
    compiled = compile_metadata_object(_specification())
    artifacts = {item["path"]: item["content"] for item in compiled["artifacts"]}
    descriptor_path = "DataProcessors/Импорт.xml"
    artifacts[descriptor_path] = artifacts[descriptor_path].replace(
        "<DefaultForm>DataProcessor.Импорт.Form.Форма</DefaultForm>",
        "<DefaultForm/>",
    )

    checked = check_metadata_artifacts(
        compiled["object_ref"], compiled["format_version"], artifacts
    )

    assert checked["status"] == "failed"
    assert "missing_default_data_processor_form" in {
        item["code"] for item in checked["diagnostics"]
    }


def test_data_processor_checker_rejects_duplicate_default_form():
    compiled = compile_metadata_object(_specification())
    artifacts = {item["path"]: item["content"] for item in compiled["artifacts"]}
    descriptor_path = "DataProcessors/Импорт.xml"
    marker = "<DefaultForm>DataProcessor.Импорт.Form.Форма</DefaultForm>"
    artifacts[descriptor_path] = artifacts[descriptor_path].replace(
        marker, marker + marker, 1
    )

    checked = check_metadata_artifacts(
        compiled["object_ref"], compiled["format_version"], artifacts
    )

    assert checked["status"] == "failed"
    assert "invalid_default_data_processor_form_count" in {
        item["code"] for item in checked["diagnostics"]
    }
