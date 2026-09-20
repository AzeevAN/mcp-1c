from __future__ import annotations

from copy import deepcopy

import pytest

from mcp1c.capability_modules.forms.checker import check_managed_form
from mcp1c.capability_modules.forms.compiler import compile_managed_form
from mcp1c.capability_modules.forms.decompiler import decompile_managed_form
from mcp1c.capability_modules.forms.models import (
    FormsContractError,
    parse_managed_form_spec,
)


def _specification() -> dict[str, object]:
    return {
        "schema_version": 2,
        "context": {"owner": "Обработка.Импорт", "role": "object"},
        "form_name": "Форма",
        "format_version": "2.20",
        "title": {"ru": "Импорт"},
        "attributes": [
            {
                "name": "Объект",
                "type": {
                    "kind": "metadata_object",
                    "object": "Обработка.Импорт",
                },
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


def test_data_processor_object_form_compile_check_decompile_roundtrip():
    specification = _specification()
    compiled = compile_managed_form(specification)
    form_xml = compiled.artifacts[0].content
    module_bsl = compiled.artifacts[1].content

    assert "cfg:DataProcessorObject.Импорт" in form_xml
    assert "<SavedData>" not in form_xml
    assert any(
        item.code == "data_processor_object_context_verified"
        and item.status == "passed"
        for item in compiled.diagnostics
    )

    checked = check_managed_form(
        form_xml,
        form_name="Форма",
        context=specification["context"],
        module_bsl=module_bsl,
    )
    assert checked.status == "checked"
    assert checked.coverage.structural == "passed"

    decompiled = decompile_managed_form(
        form_xml,
        form_name="Форма",
        context=specification["context"],
        module_bsl=module_bsl,
    )
    assert decompiled.status == "decompiled"
    assert decompiled.specification == compiled.specification


def test_data_processor_checker_rejects_application_bsl():
    specification = _specification()
    compiled = compile_managed_form(specification)
    module_bsl = (
        compiled.artifacts[1].content
        + "\r\nПроцедура Выполнить()\r\nКонецПроцедуры\r\n"
    )

    checked = check_managed_form(
        compiled.artifacts[0].content,
        form_name="Форма",
        context=specification["context"],
        module_bsl=module_bsl,
    )

    assert checked.coverage.bsl_static == "failed"
    assert "unsupported_data_processor_module_bsl" in {
        item.code for item in checked.diagnostics
    }


@pytest.mark.parametrize(
    ("mutation", "path"),
    [
        (
            lambda value: value["context"].update(
                {"owner": "ВнешняяОбработка.Импорт"}
            ),
            "$.context.owner",
        ),
        (
            lambda value: value["context"].update({"role": "list"}),
            "$.context",
        ),
        (
            lambda value: value["attributes"][0]["type"].update(
                {"object": "Обработка.Чужая"}
            ),
            "$.attributes[0].type.object",
        ),
        (
            lambda value: value["attributes"][0].update({"name": "Данные"}),
            "$.attributes[0].name",
        ),
        (
            lambda value: value["commands"].append(
                {
                    "name": "Выполнить",
                    "title": {"ru": "Выполнить"},
                    "action": "Выполнить",
                }
            ),
            "$.commands",
        ),
        (
            lambda value: value["events"].append(
                {
                    "event": "OnCreateAtServer",
                    "handler": "ПриСозданииНаСервере",
                }
            ),
            "$.events",
        ),
        (
            lambda value: value["elements"].append(
                {
                    "kind": "button",
                    "name": "Записать",
                    "command": "Write",
                    "command_kind": "form_standard",
                }
            ),
            "$.elements[1]",
        ),
    ],
)
def test_data_processor_object_form_fail_closed(mutation, path):
    specification = deepcopy(_specification())
    mutation(specification)

    with pytest.raises(FormsContractError) as caught:
        parse_managed_form_spec(specification)

    assert path in {item.path for item in caught.value.diagnostics}
