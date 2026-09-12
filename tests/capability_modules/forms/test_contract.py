from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from mcp1c.capability_modules.forms.models import (
    BSL_RESERVED_KEYWORDS,
    FormsContractError,
    ManagedFormSpec,
    parse_managed_form_spec,
)


FIXTURE = Path(__file__).with_name("fixtures") / "minimal_form.json"


def _payload() -> dict:
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


def _codes(error: FormsContractError) -> set[tuple[str, str]]:
    return {(item.code, item.path) for item in error.diagnostics}


def test_минимальная_спецификация_разбирается_в_типизированную_модель():
    form = parse_managed_form_spec(_payload())

    assert form.schema_version == 1
    assert form.form_name == "ФормаПараметров"
    assert form.format_version == "2.16"
    assert [item.name for item in form.attributes] == [
        "ПервоеЗначение",
        "ВтороеЗначение",
    ]
    assert form.elements[0].children[2].command == "Проверить"
    assert form.events[0].event == "OnCreateAtServer"


def test_typed_dict_даёт_mcp_точную_вложенную_json_schema():
    from mcp.server import MCPServer

    def probe(specification: ManagedFormSpec) -> str:
        return str(specification["schema_version"])

    server = MCPServer("forms-contract-probe")
    server.add_tool(probe)
    schema = server._tool_manager.list_tools()[0].parameters

    definition = schema["$defs"]["ManagedFormSpec"]
    assert definition["properties"]["schema_version"]["const"] == 1
    assert definition["properties"]["format_version"]["const"] == "2.16"
    assert set(definition["required"]) == {
        "schema_version",
        "form_name",
        "format_version",
        "title",
        "attributes",
        "elements",
        "commands",
        "events",
    }
    assert schema["properties"]["specification"]["$ref"].endswith(
        "/ManagedFormSpec"
    )


@pytest.mark.parametrize(
    ("mutate", "expected"),
    [
        (
            lambda value: value.update({"неизвестное": True}),
            ("unknown_key", "$.неизвестное"),
        ),
        (
            lambda value: value.update({"format_version": "2.20"}),
            ("unsupported_format_version", "$.format_version"),
        ),
        (
            lambda value: value["elements"][0]["children"][0].update(
                {"kind": "table"}
            ),
            ("unsupported_element_kind", "$.elements[0].children[0].kind"),
        ),
        (
            lambda value: value["attributes"][1].update(
                {"name": "ПервоеЗначение"}
            ),
            ("duplicate_attribute_name", "$.attributes[1].name"),
        ),
        (
            lambda value: value["attributes"][0]["type"].update(
                {"kind": "boolean"}
            ),
            ("unsupported_attribute_type", "$.attributes[0].type.kind"),
        ),
        (
            lambda value: value["elements"][0]["children"][0].update(
                {"data_path": "НеизвестныйРеквизит"}
            ),
            (
                "unresolved_data_path",
                "$.elements[0].children[0].data_path",
            ),
        ),
        (
            lambda value: value["elements"][0]["children"][2].update(
                {"command": "НеизвестнаяКоманда"}
            ),
            (
                "unresolved_command",
                "$.elements[0].children[2].command",
            ),
        ),
        (
            lambda value: value["elements"][0]["children"][2].update(
                {"default": None}
            ),
            ("invalid_type", "$.elements[0].children[2].default"),
        ),
        (
            lambda value: value["events"][0].update({"event": "OnOpen"}),
            ("unsupported_event", "$.events[0].event"),
        ),
    ],
)
def test_неподдержанный_или_несогласованный_вход_отклоняется(mutate, expected):
    payload = copy.deepcopy(_payload())
    mutate(payload)

    with pytest.raises(FormsContractError) as caught:
        parse_managed_form_spec(payload)

    assert expected in _codes(caught.value)


def test_ноль_главных_реквизитов_допустим_а_два_нет():
    payload = _payload()
    parse_managed_form_spec(payload)
    payload["attributes"][0]["main"] = True
    payload["attributes"][1]["main"] = True

    with pytest.raises(FormsContractError) as caught:
        parse_managed_form_spec(payload)

    assert ("multiple_main_attributes", "$.attributes") in _codes(caught.value)


def test_id_не_является_частью_публичной_спецификации():
    payload = _payload()
    payload["commands"][0]["id"] = 1

    with pytest.raises(FormsContractError) as caught:
        parse_managed_form_spec(payload)

    assert ("unknown_key", "$.commands[0].id") in _codes(caught.value)


@pytest.mark.parametrize("reserved", sorted(BSL_RESERVED_KEYWORDS))
def test_зарезервированное_слово_bsl_не_может_быть_именем_обработчика(
    reserved,
):
    payload = _payload()
    payload["commands"][0]["action"] = reserved

    with pytest.raises(FormsContractError) as caught:
        parse_managed_form_spec(payload)

    assert ("reserved_bsl_keyword", "$.commands[0].action") in _codes(
        caught.value
    )
