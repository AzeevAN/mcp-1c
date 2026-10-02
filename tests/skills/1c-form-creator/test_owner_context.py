from __future__ import annotations

import json
from pathlib import Path

import pytest

from form_core.compiler import compile_managed_form
from form_core.decompiler import decompile_managed_form
from form_core.models import (
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


def _document_object_payload() -> dict[str, object]:
    payload = _catalog_object_payload()
    context = payload["context"]
    assert isinstance(context, dict)
    context["owner"] = "Документ.ТестовыйДокумент"
    attributes = payload["attributes"]
    assert isinstance(attributes, list)
    main = attributes[-1]
    assert isinstance(main, dict)
    value_type = main["type"]
    assert isinstance(value_type, dict)
    value_type["object"] = "Документ.ТестовыйДокумент"
    elements = payload["elements"]
    assert isinstance(elements, list)
    group = elements[0]
    assert isinstance(group, dict)
    children = group["children"]
    assert isinstance(children, list)
    field = children[0]
    assert isinstance(field, dict)
    field["data_path"] = "Объект.Дата"
    payload["events"] = []
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


def test_document_object_context_сохраняется_в_модели():
    form = parse_managed_form_spec(_document_object_payload())

    assert form.context.owner == "Документ.ТестовыйДокумент"
    assert form.context.role == "object"


@pytest.mark.parametrize(
    ("factory", "expected"),
    [
        (
            _catalog_object_payload,
            (
                "<WindowOpeningMode>LockOwnerWindow</WindowOpeningMode>",
                "<UseForFoldersAndItems>Items</UseForFoldersAndItems>",
            ),
        ),
        (
            _document_object_payload,
            (
                "<AutoTime>CurrentOrLast</AutoTime>",
                "<UsePostingMode>Auto</UsePostingMode>",
                "<RepostOnWrite>true</RepostOnWrite>",
            ),
        ),
        (
            _information_register_record_payload,
            ("<WindowOpeningMode>LockOwnerWindow</WindowOpeningMode>",),
        ),
    ],
)
def test_owner_default_root_profile_roundtrip(factory, expected):
    payload = factory()
    compiled = compile_managed_form(payload)
    xml = compiled.artifacts[0].content
    for marker in expected:
        assert marker in xml

    result = decompile_managed_form(
        xml,
        form_name=payload["form_name"],
        context=payload["context"],
        module_bsl=compiled.artifacts[1].content,
    )

    assert result.status == "decompiled"
    assert result.specification == compiled.specification


@pytest.mark.parametrize(
    ("factory", "old", "new"),
    [
        (
            _catalog_object_payload,
            "<UseForFoldersAndItems>Items</UseForFoldersAndItems>",
            "",
        ),
        (
            _catalog_object_payload,
            "<UseForFoldersAndItems>Items</UseForFoldersAndItems>",
            "<UseForFoldersAndItems>FoldersAndItems</UseForFoldersAndItems>",
        ),
        (
            _document_object_payload,
            "<UsePostingMode>Auto</UsePostingMode>",
            "<UsePostingMode>Ask</UsePostingMode>",
        ),
        (
            _information_register_record_payload,
            "<WindowOpeningMode>LockOwnerWindow</WindowOpeningMode>",
            "",
        ),
    ],
)
def test_owner_default_root_profile_rejects_missing_or_wrong(factory, old, new):
    payload = factory()
    compiled = compile_managed_form(payload)
    xml = compiled.artifacts[0].content.replace(old, new, 1)

    result = decompile_managed_form(
        xml,
        form_name=payload["form_name"],
        context=payload["context"],
        module_bsl=compiled.artifacts[1].content,
    )

    assert result.status == "rejected"
    assert any(
        item.code == "invalid_owner_default_profile"
        for item in result.diagnostics
    )


def test_catalog_default_root_profile_rejects_document_property():
    payload = _catalog_object_payload()
    compiled = compile_managed_form(payload)
    xml = compiled.artifacts[0].content.replace(
        '<AutoCommandBar name="ФормаКоманднаяПанель" id="-1"/>',
        "<AutoTime>CurrentOrLast</AutoTime>\r\n"
        '\t<AutoCommandBar name="ФормаКоманднаяПанель" id="-1"/>',
        1,
    )

    result = decompile_managed_form(
        xml,
        form_name=payload["form_name"],
        context=payload["context"],
        module_bsl=compiled.artifacts[1].content,
    )

    assert result.status == "rejected"
    assert any(
        item.code == "invalid_owner_default_profile"
        for item in result.diagnostics
    )


def test_document_object_context_требует_главный_metadata_object():
    payload = _document_object_payload()
    attributes = payload["attributes"]
    assert isinstance(attributes, list)
    main = attributes[-1]
    assert isinstance(main, dict)
    main["main"] = False

    with pytest.raises(FormsContractError) as caught:
        parse_managed_form_spec(payload)

    assert ("incompatible_owner_context", "$.attributes") in _codes(
        caught.value
    )


def test_document_object_context_сверяется_с_главным_metadata_object():
    payload = _document_object_payload()
    attributes = payload["attributes"]
    assert isinstance(attributes, list)
    main = attributes[-1]
    assert isinstance(main, dict)
    value_type = main["type"]
    assert isinstance(value_type, dict)
    value_type["object"] = "Документ.ДругойДокумент"

    with pytest.raises(FormsContractError) as caught:
        parse_managed_form_spec(payload)

    assert (
        "incompatible_owner_context",
        "$.attributes[2].type.object",
    ) in _codes(caught.value)


def test_catalog_object_context_отклоняет_теневой_реквизит_формы():
    payload = _catalog_object_payload()
    attributes = payload["attributes"]
    assert isinstance(attributes, list)
    attributes.append(
        {
            "name": "Артикул",
            "type": {"kind": "string", "length": 50},
        }
    )
    elements = payload["elements"]
    assert isinstance(elements, list)
    group = elements[0]
    assert isinstance(group, dict)
    children = group["children"]
    assert isinstance(children, list)
    field = children[0]
    assert isinstance(field, dict)
    field["data_path"] = "Объект.Артикул"

    with pytest.raises(FormsContractError) as caught:
        parse_managed_form_spec(payload)

    assert (
        "shadowed_main_object_attribute",
        "$.attributes[3].name",
    ) in _codes(caught.value)


def test_catalog_object_context_разрешает_собственный_реквизит_формы():
    payload = _catalog_object_payload()
    attributes = payload["attributes"]
    assert isinstance(attributes, list)
    attributes.append(
        {
            "name": "СтатусПроверки",
            "type": {"kind": "string", "length": 100},
        }
    )

    form = parse_managed_form_spec(payload)

    assert form.attributes[-1].name == "СтатусПроверки"


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


def test_catalog_object_context_объясняет_как_назначить_главный_реквизит():
    payload = _catalog_object_payload()
    attributes = payload["attributes"]
    assert isinstance(attributes, list)
    main = attributes[-1]
    assert isinstance(main, dict)
    main["main"] = False

    with pytest.raises(FormsContractError) as caught:
        parse_managed_form_spec(payload)

    diagnostic = next(
        item
        for item in caught.value.diagnostics
        if item.code == "incompatible_owner_context"
    )
    assert "main=true" in diagnostic.message


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
    assert any(
        diagnostic.code == "catalog_object_context_verified"
        and diagnostic.status == "passed"
        for diagnostic in result.diagnostics
    )


def test_document_object_context_компилирует_DocumentObject_и_roundtrip():
    payload = _document_object_payload()
    compiled = compile_managed_form(payload)
    form_xml = next(
        artifact.content
        for artifact in compiled.artifacts
        if artifact.path.endswith("/Form.xml")
    )
    module_bsl = next(
        artifact.content
        for artifact in compiled.artifacts
        if artifact.path.endswith("/Module.bsl")
    )

    result = decompile_managed_form(
        form_xml,
        form_name=payload["form_name"],
        context=payload["context"],
        module_bsl=module_bsl,
    )

    assert "cfg:DocumentObject.ТестовыйДокумент" in form_xml
    assert "<MainAttribute>true</MainAttribute>" in form_xml
    assert "<DataPath>Объект.Дата</DataPath>" in form_xml
    assert "<Events>" not in form_xml
    assert "PostAndClose" not in form_xml
    assert any(
        diagnostic.code == "document_object_context_verified"
        and diagnostic.status == "passed"
        for diagnostic in compiled.diagnostics
    )
    assert result.status == "decompiled"
    assert result.specification == compiled.specification
    assert any(
        diagnostic.code == "document_object_context_verified"
        and diagnostic.status == "passed"
        for diagnostic in result.diagnostics
    )


def test_information_register_record_context_сохраняется_в_модели():
    form = parse_managed_form_spec(_information_register_record_payload())

    assert form.context.owner == "РегистрСведений.ТестовыйРегистр"
    assert form.context.role == "record"
    assert form.attributes[0].name == "Запись"
    assert form.attributes[0].main is True
    assert form.attributes[0].saved_data is True


def test_information_register_record_context_отклоняет_теневой_реквизит():
    payload = _information_register_record_payload()
    attributes = payload["attributes"]
    assert isinstance(attributes, list)
    attributes.append(
        {
            "name": "Значение",
            "type": {"kind": "number", "digits": 10, "fraction_digits": 2},
        }
    )

    with pytest.raises(FormsContractError) as caught:
        parse_managed_form_spec(payload)

    assert (
        "shadowed_main_object_attribute",
        "$.attributes[1].name",
    ) in _codes(caught.value)


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
    module_bsl = next(
        artifact.content
        for artifact in result.artifacts
        if artifact.path.endswith("/Module.bsl")
    )
    assert "Данные записи: Запись.<Реквизит>" in module_bsl
    assert 'РеквизитФормыВЗначение("Запись")' in module_bsl
    assert any(
        item.code == "information_register_record_context_verified"
        and item.status == "passed"
        for item in result.diagnostics
    )


def test_catalog_object_compiler_объясняет_доступ_к_данным_формы():
    result = compile_managed_form(_catalog_object_payload())
    module_bsl = next(
        artifact.content
        for artifact in result.artifacts
        if artifact.path.endswith("/Module.bsl")
    )

    assert "Данные формы: Объект.<Реквизит>" in module_bsl
    assert 'РеквизитФормыВЗначение("Объект")' in module_bsl
