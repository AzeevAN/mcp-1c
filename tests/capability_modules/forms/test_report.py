from __future__ import annotations

from copy import deepcopy

import pytest

from mcp1c.capability_modules.forms.checker import check_managed_form
from mcp1c.capability_modules.forms.compiler import compile_managed_form
from mcp1c.capability_modules.forms.decompiler import decompile_managed_form
from mcp1c.capability_modules.forms.models import FormsContractError, parse_managed_form_spec


def _specification() -> dict[str, object]:
    return {
        "schema_version": 2,
        "context": {"owner": "Отчет.Продажи", "role": "object"},
        "form_name": "ФормаОтчета",
        "format_version": "2.20",
        "title": {"ru": "Продажи"},
        "attributes": [{
            "name": "Отчет",
            "type": {"kind": "metadata_object", "object": "Отчет.Продажи"},
            "main": True,
        }],
        "elements": [{
            "kind": "label_decoration",
            "name": "Заголовок",
            "title": {"ru": "Продажи"},
        }],
        "commands": [],
        "events": [],
    }


def test_report_object_form_compile_check_decompile_roundtrip():
    specification = _specification()
    compiled = compile_managed_form(specification)
    form_xml = compiled.artifacts[0].content
    module_bsl = compiled.artifacts[1].content

    assert "cfg:ReportObject.Продажи" in form_xml
    assert '<Attribute name="Отчет"' in form_xml
    assert "<ReportResult>Результат</ReportResult>" in form_xml
    assert "<DetailsData>ДанныеРасшифровки</DetailsData>" in form_xml
    assert "<ReportFormType>Main</ReportFormType>" in form_xml
    assert "<AutoShowState>Auto</AutoShowState>" in form_xml
    assert (
        "<CustomSettingsFolder>"
        "КомпоновщикНастроекПользовательскиеНастройки"
        "</CustomSettingsFolder>"
    ) in form_xml
    assert "<ReportResultViewMode>Auto</ReportResultViewMode>" in form_xml
    assert (
        "<ViewModeApplicationOnSetReportResult>Auto"
        "</ViewModeApplicationOnSetReportResult>"
    ) in form_xml
    assert '<UsualGroup name="КомпоновщикНастроекПользовательскиеНастройки"' in form_xml
    assert '<SpreadSheetDocumentField name="Результат"' in form_xml
    assert '<Attribute name="Результат"' in form_xml
    assert "mxl:SpreadsheetDocument" in form_xml
    assert '<Attribute name="ДанныеРасшифровки"' in form_xml
    assert '<AutoCommandBar name="ФормаКоманднаяПанель" id="-1"/>' in form_xml
    assert "<Autofill>false</Autofill>" not in form_xml
    assert "<SavedData>" not in form_xml
    assert "Отчет.<Реквизит>" in module_bsl
    assert 'РеквизитФормыВЗначение("Отчет")' in module_bsl
    assert "Объект.<Реквизит>" not in module_bsl
    assert any(
        item.code == "report_object_context_verified" and item.status == "passed"
        for item in compiled.diagnostics
    )

    checked = check_managed_form(
        form_xml,
        form_name="ФормаОтчета",
        context=specification["context"],
        module_bsl=module_bsl,
    )
    assert checked.coverage.structural == "passed"
    assert checked.coverage.bsl_static == "not_checked"

    decompiled = decompile_managed_form(
        form_xml,
        form_name="ФормаОтчета",
        context=specification["context"],
        module_bsl=module_bsl,
    )
    assert decompiled.specification == compiled.specification


def test_report_default_form_needs_no_user_elements():
    specification = _specification()
    specification["elements"] = []

    compiled = compile_managed_form(specification)
    form_xml = compiled.artifacts[0].content
    module_bsl = compiled.artifacts[1].content

    assert '<SpreadSheetDocumentField name="Результат"' in form_xml
    assert "Заголовок" not in form_xml
    checked = check_managed_form(
        form_xml,
        form_name="ФормаОтчета",
        context=specification["context"],
        module_bsl=module_bsl,
    )
    decompiled = decompile_managed_form(
        form_xml,
        form_name="ФормаОтчета",
        context=specification["context"],
        module_bsl=module_bsl,
    )
    assert checked.coverage.structural == "passed"
    assert decompiled.specification == compiled.specification


@pytest.mark.parametrize(
    ("mutation", "path"),
    [
        (lambda value: value["context"].update({"owner": "Отчет.Чужой"}), "$.attributes[0].type.object"),
        (lambda value: value["context"].update({"role": "list"}), "$.context"),
        (lambda value: value["attributes"][0].update({"name": "Объект"}), "$.attributes[0].name"),
        (lambda value: value["attributes"][0]["type"].update({"object": "Отчет.Чужой"}), "$.attributes[0].type.object"),
        (lambda value: value["attributes"][0].update({"saved_data": True}), "$.attributes[0].saved_data"),
        (lambda value: value["commands"].append({"name": "Сформировать", "title": {"ru": "Сформировать"}, "action": "Сформировать"}), "$.commands"),
        (lambda value: value["events"].append({"event": "OnCreateAtServer", "handler": "ПриСозданииНаСервере"}), "$.events"),
        (lambda value: value["elements"].append({"kind": "button", "name": "Сформировать", "command": "Write", "command_kind": "form_standard"}), "$.elements[1]"),
    ],
)
def test_report_object_form_fails_closed(mutation, path):
    specification = deepcopy(_specification())
    mutation(specification)

    with pytest.raises(FormsContractError) as caught:
        parse_managed_form_spec(specification)

    assert path in {item.path for item in caught.value.diagnostics}


def test_report_checker_rejects_application_bsl():
    specification = _specification()
    compiled = compile_managed_form(specification)
    checked = check_managed_form(
        compiled.artifacts[0].content,
        form_name="ФормаОтчета",
        context=specification["context"],
        module_bsl=compiled.artifacts[1].content + "\r\nПроцедура Сформировать()\r\nКонецПроцедуры\r\n",
    )
    assert checked.coverage.bsl_static == "failed"
    assert "unsupported_report_module_bsl" in {item.code for item in checked.diagnostics}


def test_report_form_allows_local_non_main_attributes():
    specification = _specification()
    specification["attributes"].append({
        "name": "Параметр",
        "type": {"kind": "string", "length": 100},
    })
    specification["elements"].append({
        "kind": "input_field",
        "name": "Параметр",
        "data_path": "Параметр",
    })
    compiled = compile_managed_form(specification)
    assert '<Attribute name="Параметр"' in compiled.artifacts[0].content
    assert '<Attribute name="Параметр" id="4">' in compiled.artifacts[0].content
    assert '<InputField name="Параметр" id="9">' in compiled.artifacts[0].content
    decompiled = decompile_managed_form(
        compiled.artifacts[0].content,
        form_name="ФормаОтчета",
        context=specification["context"],
        module_bsl=compiled.artifacts[1].content,
    )
    assert decompiled.coverage.structural == "passed"
    assert decompiled.specification == compiled.specification


@pytest.mark.parametrize(
    "name", ["Результат", "результат", "ДанныеРасшифровки", "дАнНыЕрАсШиФрОвКи"]
)
def test_report_default_profile_rejects_reserved_system_names(name: str):
    specification = _specification()
    specification["attributes"].append({
        "name": name,
        "type": {"kind": "string", "length": 10},
    })

    with pytest.raises(FormsContractError) as caught:
        parse_managed_form_spec(specification)

    assert any(
        item.code == "reserved_report_form_name"
        and item.path == "$.attributes[1].name"
        for item in caught.value.diagnostics
    )


@pytest.mark.parametrize(
    "mutation",
    [
        lambda xml: xml.replace(
            "<ReportResult>Результат</ReportResult>", "", 1
        ),
        lambda xml: xml.replace(
            "<ReportFormType>Main</ReportFormType>",
            "<ReportFormType>Auxiliary</ReportFormType>",
            1,
        ),
        lambda xml: xml.replace(
            "<AutoCommandBar name=\"ФормаКоманднаяПанель\" id=\"-1\"/>",
            "<AutoCommandBar name=\"ФормаКоманднаяПанель\" id=\"-1\">"
            "<Autofill>false</Autofill></AutoCommandBar>",
            1,
        ),
        lambda xml: xml.replace(
            'name="КомпоновщикНастроекПользовательскиеНастройки" id="1"',
            'name="КомпоновщикНастроекПользовательскиеНастройки" id="9"',
            1,
        ),
        lambda xml: xml.replace(
            '<SpreadSheetDocumentField name="Результат" id="3">',
            "",
            1,
        ).replace("</SpreadSheetDocumentField>", "", 1),
        lambda xml: xml.replace(
            "mxl:SpreadsheetDocument", "xs:string", 1
        ),
        lambda xml: xml.replace(
            '<Attribute name="ДанныеРасшифровки" id="3">',
            '<Attribute name="ДанныеРасшифровки" id="3"/>'
            '<Attribute name="ДанныеРасшифровки" id="3">',
            1,
        ),
    ],
)
def test_report_default_profile_mutations_fail_closed(mutation):
    specification = _specification()
    compiled = compile_managed_form(specification)
    form_xml = mutation(compiled.artifacts[0].content)

    decompiled = decompile_managed_form(
        form_xml,
        form_name="ФормаОтчета",
        context=specification["context"],
        module_bsl=compiled.artifacts[1].content,
    )
    checked = check_managed_form(
        form_xml,
        form_name="ФормаОтчета",
        context=specification["context"],
        module_bsl=compiled.artifacts[1].content,
    )

    assert decompiled.coverage.structural == "failed"
    assert checked.coverage.structural == "failed"
    assert "invalid_report_form_profile" in {
        item.code for item in decompiled.diagnostics
    }


@pytest.mark.parametrize(
    "name", ["РезультатРасширеннаяПодсказка", "рЕзУлЬтАтКоНтЕкСтНоЕмЕнЮ"]
)
def test_report_default_profile_rejects_reserved_element_names(name: str):
    specification = _specification()
    specification["elements"].append(
        {
            "kind": "label_decoration",
            "name": name,
            "title": {"ru": "Коллизия"},
        }
    )

    with pytest.raises(FormsContractError) as caught:
        parse_managed_form_spec(specification)

    assert any(
        item.code == "reserved_report_form_name"
        and item.path == "$.elements[1].name"
        for item in caught.value.diagnostics
    )


def test_report_allows_system_element_name_in_attribute_namespace():
    specification = _specification()
    specification["attributes"].append(
        {
            "name": "РезультатКонтекстноеМеню",
            "type": {"kind": "string", "length": 10},
        }
    )

    assert compile_managed_form(specification).status == "compiled"
