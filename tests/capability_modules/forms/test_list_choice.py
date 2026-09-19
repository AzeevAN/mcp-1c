from __future__ import annotations

import re

import re

import pytest

from mcp1c.capability_modules.forms.compiler import compile_managed_form
from mcp1c.capability_modules.forms.decompiler import decompile_managed_form
from mcp1c.capability_modules.forms.models import FormsContractError, parse_managed_form_spec


def _payload(owner: str, role: str) -> dict[str, object]:
    return {
        "schema_version": 2,
        "form_name": "ФормаСписка" if role == "list" else "ФормаВыбора",
        "context": {"owner": owner, "role": role},
        "format_version": "2.20",
        "title": {"ru": "Список"},
        "attributes": [
            {
                "name": "Список",
                "type": {
                    "kind": "dynamic_list",
                    "main_table": owner,
                    "dynamic_data_read": True,
                },
                "main": True,
            }
        ],
        "elements": [
            {
                "kind": "table",
                "name": "Список",
                "data_path": "Список",
                "columns": [
                    {
                        "kind": "input_field",
                        "name": "Наименование",
                        "data_path": "Список.Наименование",
                    }
                ],
            }
        ],
        "commands": [],
        "events": [],
    }


@pytest.mark.parametrize(
    ("owner", "role"),
    [
        ("Справочник.Товары", "list"),
        ("Справочник.Товары", "choice"),
        ("Документ.Заказ", "list"),
        ("Документ.Заказ", "choice"),
        ("РегистрСведений.Курсы", "list"),
    ],
)
def test_list_choice_compile_check_decompile_roundtrip(owner: str, role: str):
    payload = _payload(owner, role)
    compiled = compile_managed_form(payload)
    xml = compiled.artifacts[0].content

    assert "<CommandBarLocation>None</CommandBarLocation>" in xml
    assert (
        '<AutoCommandBar name="ФормаКоманднаяПанель" id="-1"/>'
        in xml
    )
    assert '<AutoCommandBar name="СписокКоманднаяПанель"' in xml
    assert "<Autofill>false</Autofill>" not in xml
    if role == "list":
        assert "<WindowOpeningMode>" not in xml
        assert "<ChoiceMode>" not in xml
    else:
        assert "<WindowOpeningMode>LockOwnerWindow</WindowOpeningMode>" in xml
        assert "<ChoiceMode>true</ChoiceMode>" in xml

    result = decompile_managed_form(
        xml,
        form_name=payload["form_name"],
        context=payload["context"],
        module_bsl=compiled.artifacts[1].content,
    )
    assert result.status == "decompiled"
    assert result.coverage.structural == "passed"
    assert result.specification == compiled.specification


def _record_set_payload() -> dict[str, object]:
    return {
        "schema_version": 2,
        "form_name": "ФормаНабораЗаписей",
        "context": {
            "owner": "РегистрСведений.Курсы",
            "role": "record_set",
        },
        "format_version": "2.20",
        "title": {"ru": "Набор записей"},
        "attributes": [
            {
                "name": "Курсы",
                "type": {
                    "kind": "metadata_object",
                    "object": "РегистрСведений.Курсы",
                },
                "main": True,
                "saved_data": True,
            }
        ],
        "elements": [
            {
                "kind": "table",
                "name": "НаборЗаписей",
                "data_path": "Курсы",
                "columns": [
                    {
                        "kind": "input_field",
                        "name": "Валюта",
                        "data_path": "Курсы.Валюта",
                    },
                    {
                        "kind": "input_field",
                        "name": "Курс",
                        "data_path": "Курсы.Курс",
                    },
                ],
            }
        ],
        "commands": [],
        "events": [],
    }


def test_information_register_record_set_roundtrip():
    payload = _record_set_payload()
    payload["attributes"].append(
        {
            "name": "ДругойРегистр",
            "type": {
                "kind": "metadata_object",
                "object": "РегистрСведений.Другой",
            },
        }
    )
    compiled = compile_managed_form(payload)
    xml = compiled.artifacts[0].content

    assert "cfg:InformationRegisterRecordSet.Курсы" in xml
    assert "cfg:InformationRegisterRecordManager.Другой" in xml
    assert "cfg:InformationRegisterRecordSet.Другой" not in xml
    assert "<SavedData>true</SavedData>" in xml
    assert "<DataPath>Курсы</DataPath>" in xml
    assert "Данные набора записей: Курсы.<Реквизит>." in compiled.artifacts[1].content

    result = decompile_managed_form(
        xml,
        form_name=payload["form_name"],
        context=payload["context"],
        module_bsl=compiled.artifacts[1].content,
    )

    assert result.status == "decompiled"
    assert result.coverage.structural == "passed"
    assert result.specification == compiled.specification
    assert any(
        item.status == "passed"
        and item.code == "information_register_record_set_context_verified"
        for item in result.diagnostics
    )


def test_information_register_record_set_rejects_record_manager_xml_type():
    payload = _record_set_payload()
    compiled = compile_managed_form(payload)
    xml = compiled.artifacts[0].content.replace(
        "cfg:InformationRegisterRecordSet.Курсы",
        "cfg:InformationRegisterRecordManager.Курсы",
    )

    result = decompile_managed_form(
        xml,
        form_name=payload["form_name"],
        context=payload["context"],
        module_bsl=compiled.artifacts[1].content,
    )

    assert result.status == "rejected"
    assert any(
        item.code == "information_register_role_type_mismatch"
        for item in result.diagnostics
    )


def test_record_set_xml_type_is_forbidden_on_secondary_attribute():
    payload = _record_set_payload()
    payload["attributes"].append(
        {
            "name": "ДругойРегистр",
            "type": {
                "kind": "metadata_object",
                "object": "РегистрСведений.Другой",
            },
        }
    )
    compiled = compile_managed_form(payload)
    xml = compiled.artifacts[0].content.replace(
        "cfg:InformationRegisterRecordManager.Другой",
        "cfg:InformationRegisterRecordSet.Другой",
    )

    result = decompile_managed_form(
        xml,
        form_name=payload["form_name"],
        context=payload["context"],
        module_bsl=compiled.artifacts[1].content,
    )

    assert result.status == "rejected"
    assert any(
        item.code == "unsupported_record_set_attribute_type"
        for item in result.diagnostics
    )


@pytest.mark.parametrize(
    ("mutation", "path"),
    [
        (
            lambda value: value["attributes"][0].update(name="Набор"),
            "$.attributes[0].name",
        ),
        (
            lambda value: value["attributes"][0].update(saved_data=False),
            "$.attributes[0].saved_data",
        ),
        (
            lambda value: value["elements"][0].update(name="Таблица"),
            "$.elements",
        ),
        (
            lambda value: value["elements"][0].update(data_path="Набор"),
            "$.elements",
        ),
    ],
)
def test_information_register_record_set_fails_closed(mutation, path: str):
    payload = _record_set_payload()
    mutation(payload)

    with pytest.raises(FormsContractError) as caught:
        parse_managed_form_spec(payload)

    assert any(
        item.code == "incompatible_owner_context" and item.path == path
        for item in caught.value.diagnostics
    )


@pytest.mark.parametrize(
    ("mutation", "path"),
    [
        (lambda value: value["attributes"][0].update(name="Данные"), "$.attributes[0].name"),
        (lambda value: value["attributes"][0].update(main=False), "$.attributes"),
        (
            lambda value: value["attributes"][0].update(
                type={"kind": "string", "length": 20}
            ),
            "$.attributes",
        ),
        (
            lambda value: value["attributes"][0]["type"].update(
                main_table="Справочник.Другой"
            ),
            "$.attributes[0].type.main_table",
        ),
        (lambda value: value["elements"][0].update(data_path="Данные"), "$.elements"),
    ],
)
def test_list_choice_fail_closed_for_owner_profile(mutation, path: str):
    payload = _payload("Справочник.Товары", "list")
    mutation(payload)

    with pytest.raises(FormsContractError) as caught:
        parse_managed_form_spec(payload)

    assert any(
        item.code == "incompatible_owner_context" and item.path == path
        for item in caught.value.diagnostics
    )


def test_list_rejects_choice_xml_markers():
    choice = _payload("Справочник.Товары", "choice")
    xml = compile_managed_form(choice).artifacts[0].content

    result = decompile_managed_form(
        xml,
        form_name="ФормаСписка",
        context={"owner": "Справочник.Товары", "role": "list"},
    )

    assert result.status == "rejected"
    assert any(item.code == "unexpected_choice_role_marker" for item in result.diagnostics)


@pytest.mark.parametrize(
    "replace",
    [
        ("<WindowOpeningMode>LockOwnerWindow</WindowOpeningMode>\r\n", ""),
        ("<ChoiceMode>true</ChoiceMode>\r\n", ""),
        ("LockOwnerWindow", "Independent"),
        ("<ChoiceMode>true</ChoiceMode>", "<ChoiceMode>false</ChoiceMode>"),
    ],
)
def test_choice_rejects_missing_or_wrong_xml_markers(replace: tuple[str, str]):
    payload = _payload("Документ.Заказ", "choice")
    xml = compile_managed_form(payload).artifacts[0].content.replace(*replace)

    result = decompile_managed_form(
        xml,
        form_name=payload["form_name"],
        context=payload["context"],
    )

    assert result.status == "rejected"
    assert any(
        item.code in {"missing_choice_role_marker", "invalid_choice_role_marker"}
        for item in result.diagnostics
    )


@pytest.mark.parametrize("location", [None, "Top"])
@pytest.mark.parametrize("role", ["list", "choice"])
def test_list_choice_rejects_missing_or_wrong_command_bar_location(
    role: str,
    location: str | None,
):
    payload = _payload("Справочник.Товары", role)
    compiled = compile_managed_form(payload)
    marker = "<CommandBarLocation>None</CommandBarLocation>"
    replacement = (
        ""
        if location is None
        else f"<CommandBarLocation>{location}</CommandBarLocation>"
    )
    xml = compiled.artifacts[0].content.replace(marker, replacement, 1)

    result = decompile_managed_form(
        xml,
        form_name=payload["form_name"],
        context=payload["context"],
    )

    assert result.status == "rejected"
    expected_code = (
        "missing_list_choice_command_bar_location"
        if location is None
        else "invalid_list_choice_command_bar_location"
    )
    assert any(item.code == expected_code for item in result.diagnostics)


@pytest.mark.parametrize("role", ["list", "choice"])
@pytest.mark.parametrize("mutation", ["missing", "autofill_false"])
def test_list_choice_rejects_missing_or_disabled_table_command_bar(
    role: str,
    mutation: str,
):
    payload = _payload("Справочник.Товары", role)
    compiled = compile_managed_form(payload)
    pattern = r'(<AutoCommandBar name="СписокКоманднаяПанель" id="[^"]+")/>'
    replacement = (
        ""
        if mutation == "missing"
        else (
            r"\1>" + "\r\n"
            "\t\t\t<Autofill>false</Autofill>\r\n"
            "\t\t</AutoCommandBar>"
        )
    )
    xml, replacements = re.subn(
        pattern,
        replacement,
        compiled.artifacts[0].content,
        count=1,
    )
    assert replacements == 1

    result = decompile_managed_form(
        xml,
        form_name=payload["form_name"],
        context=payload["context"],
    )

    assert result.status == "rejected"
    expected_code = (
        "missing_list_choice_table_command_bar"
        if mutation == "missing"
        else "invalid_list_choice_table_autofill"
    )
    assert any(item.code == expected_code for item in result.diagnostics)


def test_list_choice_minimal_profile_rejects_commands_and_events():
    payload = _payload("Справочник.Товары", "list")
    payload["commands"] = [
        {"name": "Обновить", "title": {"ru": "Обновить"}, "action": "Обновить"}
    ]
    payload["events"] = [
        {"event": "OnOpen", "handler": "ПриОткрытии"}
    ]

    with pytest.raises(FormsContractError) as caught:
        parse_managed_form_spec(payload)

    paths = {item.path for item in caught.value.diagnostics}
    assert {"$.commands", "$.events"} <= paths


def test_list_choice_remains_closed_for_other_owner_kinds():
    payload = _payload("Справочник.Товары", "list")
    payload["context"] = {"owner": "Обработка.Импорт", "role": "list"}

    with pytest.raises(FormsContractError) as caught:
        parse_managed_form_spec(payload)

    assert any(
        item.code == "unsupported_owner_role"
        for item in caught.value.diagnostics
    )
