from __future__ import annotations

import json
from pathlib import Path

import pytest

from mcp1c.capability_modules.forms.compiler import compile_managed_form
from mcp1c.capability_modules.forms.models import (
    FormsContractError,
    parse_managed_form_spec,
)


FIXTURE = Path(__file__).with_name("fixtures") / "minimal_form.json"


def _payload() -> dict[str, object]:
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


def _catalog_object_payload() -> dict[str, object]:
    payload = _payload()
    payload["schema_version"] = 2
    payload["context"] = {
        "owner": "Справочник.ТестовыйОбъект",
        "role": "object",
    }
    attributes = payload["attributes"]
    assert isinstance(attributes, list)
    attributes.append(
        {
            "name": "Объект",
            "type": {
                "kind": "metadata_object",
                "object": "Справочник.ТестовыйОбъект",
            },
            "main": True,
        }
    )
    return payload


def _information_register_record_payload() -> dict[str, object]:
    payload = _payload()
    payload["form_name"] = "ФормаЗаписи"
    payload["context"] = {
        "owner": "РегистрСведений.ТестовыйРегистр",
        "role": "record",
    }
    payload["attributes"] = [
        {
            "name": "Запись",
            "type": {
                "kind": "metadata_object",
                "object": "РегистрСведений.ТестовыйРегистр",
            },
            "main": True,
            "saved_data": True,
        }
    ]
    payload["elements"] = [
        {
            "kind": "usual_group",
            "name": "ОсновныеДанные",
            "title": {"ru": "Основные данные"},
            "children": [
                {
                    "kind": "input_field",
                    "name": "Значение",
                    "data_path": "Запись.Значение",
                }
            ],
        }
    ]
    payload["commands"] = []
    return payload


def _codes(error: FormsContractError) -> set[tuple[str, str]]:
    return {(item.code, item.path) for item in error.diagnostics}


def test_forms_schema_v1_отклоняется_после_перехода_на_v2():
    payload = _payload()
    payload["schema_version"] = 1

    with pytest.raises(FormsContractError) as caught:
        parse_managed_form_spec(payload)

    assert ("unsupported_schema_version", "$.schema_version") in _codes(
        caught.value
    )


def test_forms_schema_v2_требует_context():
    payload = _payload()
    payload["schema_version"] = 2
    payload.pop("context")

    with pytest.raises(FormsContractError) as caught:
        parse_managed_form_spec(payload)

    assert ("missing_key", "$.context") in _codes(caught.value)


def test_catalog_object_context_сохраняется_в_модели():
    form = parse_managed_form_spec(_catalog_object_payload())

    assert form.schema_version == 2
    assert form.context.owner == "Справочник.ТестовыйОбъект"
    assert form.context.role == "object"


def test_catalog_object_context_сверяется_с_главным_реквизитом():
    payload = _catalog_object_payload()
    attributes = payload["attributes"]
    assert isinstance(attributes, list)
    main = attributes[-1]
    assert isinstance(main, dict)
    value_type = main["type"]
    assert isinstance(value_type, dict)
    value_type["object"] = "Справочник.ДругойОбъект"

    with pytest.raises(FormsContractError) as caught:
        parse_managed_form_spec(payload)

    assert (
        "incompatible_owner_context",
        "$.attributes[2].type.object",
    ) in _codes(caught.value)


def test_custom_context_не_требует_объектный_главный_реквизит():
    payload = _payload()
    payload["schema_version"] = 2
    payload["context"] = {
        "owner": "Обработка.ТестоваяОбработка",
        "role": "custom",
    }

    form = parse_managed_form_spec(payload)

    assert form.context.role == "custom"


def test_catalog_object_context_сохраняется_в_result_без_отдельной_xml_grammar():
    payload = _catalog_object_payload()

    result = compile_managed_form(payload)

    assert result.specification is not None
    assert result.specification["context"] == payload["context"]
    form_xml = next(
        artifact.content
        for artifact in result.artifacts
        if artifact.path.endswith("/Form.xml")
    )
    assert "Справочник.ТестовыйОбъект" not in form_xml
    assert "cfg:CatalogObject" in form_xml


def test_information_register_record_context_сохраняется_в_модели():
    form = parse_managed_form_spec(_information_register_record_payload())

    assert form.context.owner == "РегистрСведений.ТестовыйРегистр"
    assert form.context.role == "record"
    assert form.attributes[0].name == "Запись"
    assert form.attributes[0].main is True
    assert form.attributes[0].saved_data is True


def test_information_register_record_context_требует_главную_запись():
    payload = _information_register_record_payload()
    attributes = payload["attributes"]
    assert isinstance(attributes, list)
    main = attributes[0]
    assert isinstance(main, dict)
    main["main"] = False

    with pytest.raises(FormsContractError) as caught:
        parse_managed_form_spec(payload)

    assert ("incompatible_owner_context", "$.attributes") in _codes(
        caught.value
    )


def test_information_register_record_context_сверяется_с_владельцем():
    payload = _information_register_record_payload()
    attributes = payload["attributes"]
    assert isinstance(attributes, list)
    main = attributes[0]
    assert isinstance(main, dict)
    value_type = main["type"]
    assert isinstance(value_type, dict)
    value_type["object"] = "РегистрСведений.ДругойРегистр"

    with pytest.raises(FormsContractError) as caught:
        parse_managed_form_spec(payload)

    assert (
        "incompatible_owner_context",
        "$.attributes[0].type.object",
    ) in _codes(caught.value)


def test_information_register_record_context_требует_имя_запись():
    payload = _information_register_record_payload()
    attributes = payload["attributes"]
    assert isinstance(attributes, list)
    main = attributes[0]
    assert isinstance(main, dict)
    main["name"] = "Объект"
    elements = payload["elements"]
    assert isinstance(elements, list)
    group = elements[0]
    assert isinstance(group, dict)
    children = group["children"]
    assert isinstance(children, list)
    field = children[0]
    assert isinstance(field, dict)
    field["data_path"] = "Объект.Значение"

    with pytest.raises(FormsContractError) as caught:
        parse_managed_form_spec(payload)

    assert ("incompatible_owner_context", "$.attributes[0].name") in _codes(
        caught.value
    )


def test_information_register_record_context_требует_saved_data():
    payload = _information_register_record_payload()
    attributes = payload["attributes"]
    assert isinstance(attributes, list)
    main = attributes[0]
    assert isinstance(main, dict)
    main.pop("saved_data")

    with pytest.raises(FormsContractError) as caught:
        parse_managed_form_spec(payload)

    assert (
        "incompatible_owner_context",
        "$.attributes[0].saved_data",
    ) in _codes(caught.value)


def test_information_register_record_context_отклоняет_saved_data_false():
    payload = _information_register_record_payload()
    attributes = payload["attributes"]
    assert isinstance(attributes, list)
    main = attributes[0]
    assert isinstance(main, dict)
    main["saved_data"] = False

    with pytest.raises(FormsContractError) as caught:
        parse_managed_form_spec(payload)

    assert (
        "incompatible_owner_context",
        "$.attributes[0].saved_data",
    ) in _codes(caught.value)


def test_information_register_record_компилирует_native_main_attribute():
    result = compile_managed_form(_information_register_record_payload())
    form_xml = next(
        artifact.content
        for artifact in result.artifacts
        if artifact.path.endswith("/Form.xml")
    )

    assert "cfg:InformationRegisterRecordManager.ТестовыйРегистр" in form_xml
    assert "<MainAttribute>true</MainAttribute>" in form_xml
    assert "<SavedData>true</SavedData>" in form_xml
    assert "<DataPath>Запись.Значение</DataPath>" in form_xml
    assert '<AutoCommandBar name="ФормаКоманднаяПанель" id="-1"/>' in form_xml
    assert "\t<Commands>\r\n\t</Commands>" in form_xml
    assert any(
        item.code == "information_register_record_context_verified"
        and item.status == "passed"
        for item in result.diagnostics
    )
