from __future__ import annotations

from copy import deepcopy

import pytest
from jsonschema import Draft202012Validator

from mcp1c.capability_modules.forms.compiler import compile_managed_form
from mcp1c.capability_modules.metadata_authoring.checker import check_metadata_artifacts
from mcp1c.capability_modules.metadata_authoring.compiler import (
    MetadataAuthoringContractError,
    compile_metadata_object,
)
from mcp1c.capability_modules.metadata_authoring.rules import get_metadata_authoring_rules
from mcp1c.capability_modules.metadata_authoring.schema import METADATA_SPECIFICATION_SCHEMA


def _form_artifacts(*, with_local_attribute: bool = False) -> tuple[str, str]:
    attributes = [{
        "name": "Отчет",
        "type": {"kind": "metadata_object", "object": "Отчет.Продажи"},
        "main": True,
    }]
    elements = [{"kind": "label_decoration", "name": "Заголовок", "title": {"ru": "Продажи"}}]
    if with_local_attribute:
        attributes.append({"name": "Параметр", "type": {"kind": "string", "length": 100}})
        elements.append({"kind": "input_field", "name": "Параметр", "data_path": "Параметр"})
    result = compile_managed_form({
        "schema_version": 2,
        "context": {"owner": "Отчет.Продажи", "role": "object"},
        "form_name": "ФормаОтчета",
        "format_version": "2.20",
        "title": {"ru": "Продажи"},
        "attributes": attributes,
        "elements": elements,
        "commands": [],
        "events": [],
    })
    return result.artifacts[0].content, result.artifacts[1].content


def _specification() -> dict[str, object]:
    form_xml, module_bsl = _form_artifacts()
    return {
        "schema_version": 1,
        "object_ref": "Отчет.Продажи",
        "format_version": "2.20",
        "identity": "90000000-0000-0000-0000-000000000001",
        "synonym": "Продажи",
        "attributes": [],
        "forms": [{
            "name": "ФормаОтчета",
            "synonym": "Форма отчета",
            "role": "object",
            "default": True,
            "form_xml": form_xml,
            "module_bsl": module_bsl,
        }],
    }


def _artifacts(compiled: dict[str, object]) -> dict[str, str]:
    return {item["path"]: item["content"] for item in compiled["artifacts"]}


def test_report_compile_and_exact_bundle_check():
    compiled = compile_metadata_object(_specification())
    artifacts = _artifacts(compiled)

    assert set(artifacts) == {
        "Reports/Продажи.xml",
        "Reports/Продажи/Forms/ФормаОтчета.xml",
        "Reports/Продажи/Forms/ФормаОтчета/Ext/Form.xml",
        "Reports/Продажи/Templates/ОсновнаяСхемаКомпоновкиДанных.xml",
        "Reports/Продажи/Templates/ОсновнаяСхемаКомпоновкиДанных/Ext/Template.xml",
    }
    descriptor = artifacts["Reports/Продажи.xml"]
    assert 'name="ReportObject.Продажи" category="Object"' in descriptor
    assert 'name="ReportManager.Продажи" category="Manager"' in descriptor
    assert "<DefaultForm>Report.Продажи.Form.ФормаОтчета</DefaultForm>" in descriptor
    assert "<UseStandardCommands>true</UseStandardCommands>" in descriptor
    assert (
        "<MainDataCompositionSchema>Report.Продажи.Template."
        "ОсновнаяСхемаКомпоновкиДанных</MainDataCompositionSchema>"
    ) in descriptor
    assert "<Template>ОсновнаяСхемаКомпоновкиДанных</Template>" in descriptor
    assert "<AuxiliarySettingsForm/>" in descriptor
    template = artifacts[
        "Reports/Продажи/Templates/"
        "ОсновнаяСхемаКомпоновкиДанных/Ext/Template.xml"
    ]
    assert "<dcsset:name>Основной</dcsset:name>" in template
    assert "<dcsset:settings" in template
    assert check_metadata_artifacts(
        compiled["object_ref"], compiled["format_version"], artifacts
    )["status"] == "passed"


def test_report_schema_and_rules_are_discoverable():
    assert list(
        Draft202012Validator(METADATA_SPECIFICATION_SCHEMA).iter_errors(
            _specification()
        )
    ) == []
    rules = get_metadata_authoring_rules("report")
    assert rules["object_ref"] == "Отчет.<Имя>"
    assert rules["generated_types"] == ["Object", "Manager"]
    assert rules["forms_profile"]["main_attribute"]["name"] == "Отчет"
    assert rules["use_standard_commands"] is True
    assert rules["data_composition_schema_profile"]["settings_variant"] == "Основной"
    assert rules["forms_profile"]["derived_by_forms_compiler"]["auto_command_bar"] == (
        "standard_platform_filled"
    )
    assert ".erf" in rules["scope"]


def test_report_metadata_accepts_local_non_main_form_attribute():
    specification = _specification()
    form_xml, module_bsl = _form_artifacts(with_local_attribute=True)
    specification["forms"][0]["form_xml"] = form_xml
    specification["forms"][0]["module_bsl"] = module_bsl
    compiled = compile_metadata_object(specification)
    assert check_metadata_artifacts(
        compiled["object_ref"], compiled["format_version"], compiled["artifacts"]
    )["status"] == "passed"


@pytest.mark.parametrize(
    ("mutation", "code"),
    [
        (lambda value: value.update({"attributes": [{"name": "Поле", "synonym": "Поле", "type": {"kind": "string", "length": 10}}]}), "unsupported_report_attributes"),
        (lambda value: value.update({"forms": []}), "invalid_report_form_count"),
        (lambda value: value["forms"].append(deepcopy(value["forms"][0])), "invalid_report_form_count"),
        (lambda value: value["forms"][0].update({"default": False}), "missing_default_report_form"),
        (lambda value: value["forms"][0].update({"role": "list"}), "unsupported_owner_role"),
        (lambda value: value["forms"][0].update({"module_bsl": "Процедура Сформировать()\nКонецПроцедуры"}), "unsupported_report_module_bsl"),
        (lambda value: value["forms"][0].update({"form_xml": value["forms"][0]["form_xml"].replace('<Attribute name="Отчет"', '<Attribute name="Объект"')}), "invalid_report_main_attribute"),
        (lambda value: value["forms"][0].update({"form_xml": value["forms"][0]["form_xml"].replace("cfg:ReportObject.Продажи", "cfg:ReportObject.Чужой")}), "form_owner_mismatch"),
        (lambda value: value["forms"][0].update({"form_xml": value["forms"][0]["form_xml"].replace("<MainAttribute>true</MainAttribute>", "<MainAttribute>true</MainAttribute><SavedData>true</SavedData>")}), "unsupported_report_form_behavior"),
    ],
)
def test_report_compile_fails_closed(mutation, code):
    specification = _specification()
    mutation(specification)
    with pytest.raises(MetadataAuthoringContractError) as caught:
        compile_metadata_object(specification)
    assert caught.value.diagnostics[0]["code"] == code


@pytest.mark.parametrize(
    ("path", "mutation", "code"),
    [
        ("Reports/Продажи.xml", lambda text: text.replace("Report.Продажи.Template.ОсновнаяСхемаКомпоновкиДанных", "Schema"), "invalid_report_data_composition_schema"),
        ("Reports/Продажи.xml", lambda text: text.replace("<UseStandardCommands>true</UseStandardCommands>", "<UseStandardCommands>false</UseStandardCommands>"), "invalid_report_standard_commands"),
        ("Reports/Продажи.xml", lambda text: text.replace("<DefaultVariantForm/>", "<DefaultVariantForm>CommonForm.Вариант</DefaultVariantForm>"), "unsupported_report_settings"),
        ("Reports/Продажи.xml", lambda text: text.replace("<AuxiliaryForm/>", "<AuxiliaryForm>Report.Продажи.Form.Дополнительная</AuxiliaryForm>"), "unsupported_auxiliary_form"),
        ("Reports/Продажи.xml", lambda text: text.replace("<DefaultForm>", "<DefaultObjectForm>Report.Продажи.Form.ФормаОтчета</DefaultObjectForm><DefaultForm>"), "unsupported_default_form_property"),
        ("Reports/Продажи.xml", lambda text: text.replace("<ChildObjects>", "<ChildObjects><Template>Макет</Template>"), "invalid_report_dcs_child"),
        ("Reports/Продажи/Templates/ОсновнаяСхемаКомпоновкиДанных/Ext/Template.xml", lambda text: text.replace("<dcsset:name>Основной</dcsset:name>", "<dcsset:name>Чужой</dcsset:name>"), "invalid_report_dcs_default_variant"),
        ("Reports/Продажи/Templates/ОсновнаяСхемаКомпоновкиДанных/Ext/Template.xml", lambda text: text.replace("/>\r\n\t</settingsVariant>", "><dcsset:selection/></dcsset:settings>\r\n\t</settingsVariant>"), "invalid_report_dcs_default_variant"),
        ("Reports/Продажи/Templates/ОсновнаяСхемаКомпоновкиДанных.xml", lambda text: text.replace("<TemplateType>DataCompositionSchema</TemplateType>", "<TemplateType>SpreadsheetDocument</TemplateType>"), "invalid_report_dcs_descriptor"),
        ("Reports/Продажи/Forms/ФормаОтчета/Ext/Form.xml", lambda text: text.replace("<MainAttribute>true</MainAttribute>", "<MainAttribute>true</MainAttribute><SavedData>true</SavedData>"), "unsupported_report_form_behavior"),
    ],
)
def test_report_checker_fails_closed(path, mutation, code):
    compiled = compile_metadata_object(_specification())
    artifacts = _artifacts(compiled)
    artifacts[path] = mutation(artifacts[path])
    checked = check_metadata_artifacts(compiled["object_ref"], compiled["format_version"], artifacts)
    assert code in {item["code"] for item in checked["diagnostics"]}


def test_report_checker_rejects_extra_and_unsafe_artifacts():
    compiled = compile_metadata_object(_specification())
    artifacts = _artifacts(compiled)
    extra = {**artifacts, "Reports/Продажи/Forms/Лишняя.xml": "x"}
    unsafe = {**artifacts, "Reports/Продажи/../secret.txt": "x"}
    assert "unexpected_artifact" in {
        item["code"] for item in check_metadata_artifacts(
            compiled["object_ref"], compiled["format_version"], extra
        )["diagnostics"]
    }
    assert "unsafe_artifact_path" in {
        item["code"] for item in check_metadata_artifacts(
            compiled["object_ref"], compiled["format_version"], unsafe
        )["diagnostics"]
    }


@pytest.mark.parametrize(
    ("path", "code"),
    [
        (
            "Reports/Продажи/Templates/ОсновнаяСхемаКомпоновкиДанных.xml",
            "missing_report_dcs_descriptor",
        ),
        (
            "Reports/Продажи/Templates/ОсновнаяСхемаКомпоновкиДанных/Ext/Template.xml",
            "missing_report_dcs_xml",
        ),
    ],
)
def test_report_checker_rejects_missing_dcs_artifacts(path, code):
    compiled = compile_metadata_object(_specification())
    artifacts = _artifacts(compiled)
    del artifacts[path]
    checked = check_metadata_artifacts(
        compiled["object_ref"], compiled["format_version"], artifacts
    )
    assert code in {item["code"] for item in checked["diagnostics"]}
