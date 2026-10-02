from __future__ import annotations

from copy import deepcopy

import pytest

from form_core.checker import check_managed_form
from form_core.compiler import compile_managed_form
from form_core.decompiler import decompile_managed_form
from form_core.models import (
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


def test_data_processor_default_form_can_be_empty_like_configurator():
    specification = _specification()
    specification["attributes"] = [specification["attributes"][0]]
    specification["elements"] = []

    compiled = compile_managed_form(specification)
    form_xml = compiled.artifacts[0].content
    module_bsl = compiled.artifacts[1].content

    assert "<ChildItems>" not in form_xml
    checked = check_managed_form(
        form_xml,
        form_name="Форма",
        context=specification["context"],
        module_bsl=module_bsl,
    )
    decompiled = decompile_managed_form(
        form_xml,
        form_name="Форма",
        context=specification["context"],
        module_bsl=module_bsl,
    )
    assert checked.coverage.structural == "passed"
    assert decompiled.specification == compiled.specification


def test_data_processor_checker_accepts_application_bsl_without_bindings():
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

    assert checked.coverage.bsl_static == "not_checked"
    assert not any(item.status == "failed" for item in checked.diagnostics)
    assert any(
        item.code == "bsl_api_not_checked" and item.status == "not_checked"
        for item in checked.diagnostics
    )


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
    ],
)
def test_data_processor_object_form_fail_closed(mutation, path):
    specification = deepcopy(_specification())
    mutation(specification)

    with pytest.raises(FormsContractError) as caught:
        parse_managed_form_spec(specification)

    assert path in {item.path for item in caught.value.diagnostics}
