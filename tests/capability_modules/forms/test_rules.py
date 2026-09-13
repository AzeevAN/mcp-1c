from __future__ import annotations

import json
from pathlib import Path
from typing import get_args, get_type_hints

import pytest

from mcp1c.capability_modules.forms.rules import (
    RULE_TOPICS,
    FormsRuleQueryError,
    get_managed_form_rules,
)
from mcp1c.capability_modules.forms import models


def test_темы_правил_закрыты_и_имеют_стабильный_порядок():
    assert RULE_TOPICS == (
        "overview",
        "terminology",
        "specification",
        "elements",
        "attributes",
        "layout",
        "commands_events",
        "diagnostics",
    )


def test_терминология_покрывает_все_поддержанные_элементы_и_свойства():
    payload = get_managed_form_rules("terminology")
    entries = payload["entries"]

    elements = set(entries["element"])
    assert elements == {
        "usual_group",
        "input_field",
        "check_box_field",
        "label_decoration",
        "label_field",
        "radio_button_field",
        "button",
        "command_bar",
        "popup",
        "button_group",
        "pages",
        "table",
    }

    properties = set(entries["property"])
    assert properties == {
        "action",
        "allowed_sign",
        "attributes",
        "choice_list",
        "children",
        "columns",
        "columns_count",
        "command",
        "command_kind",
        "command_owner",
        "command_source",
        "commands",
        "data_path",
        "default",
        "digits",
        "dynamic_data_read",
        "elements",
        "event",
        "events",
        "format_version",
        "form_name",
        "fraction_digits",
        "fractions",
        "handler",
        "horizontal_location",
        "horizontal_stretch",
        "hyperlink",
        "item",
        "kind",
        "length",
        "list_choice_mode",
        "main",
        "main_table",
        "multiline",
        "name",
        "object",
        "orientation",
        "owner",
        "pages",
        "platform_version",
        "presentation",
        "radio_button_type",
        "read_only",
        "representation",
        "ru",
        "schema_version",
        "show_title",
        "title",
        "type",
        "value",
        "variants",
        "vertical_stretch",
    }
    assert set(entries["attribute_type"]) == {
        get_args(get_type_hints(spec)["kind"])[0]
        for spec in get_args(models.AttributeTypeSpec)
    }


@pytest.mark.parametrize(
    "query",
    ["панель команд", "command bar", "CommandBar", "command_bar"],
)
def test_поиск_панели_команд_одинаково_работает_на_русском_и_английском(query):
    payload = get_managed_form_rules("terminology", query=query)

    assert [entry["canonical"] for entry in payload["matches"]] == ["command_bar"]


@pytest.mark.parametrize(
    "query",
    [
        "положение по горизонтали",
        "horizontal location",
        "HorizontalLocation",
        "horizontal_location",
    ],
)
def test_поиск_свойства_одинаково_работает_на_русском_и_английском(query):
    payload = get_managed_form_rules("terminology", query=query)

    assert [entry["canonical"] for entry in payload["matches"]] == [
        "horizontal_location"
    ]


def test_поиск_русского_значения_возвращает_свойство_и_каноническое_значение():
    payload = get_managed_form_rules("terminology", query="слева")

    assert payload["matches"] == [
        {
            "canonical": "horizontal_location",
            "category": "property",
            "russian": ["положение по горизонтали", "горизонтальное положение"],
            "english": ["horizontal location"],
            "scopes": ["command_bar"],
            "values": {
                "auto": ["автоматически", "auto"],
                "left": ["слева", "left"],
                "center": ["по центру", "center"],
                "right": ["справа", "right"],
            },
            "matched_values": ["left"],
        }
    ]


@pytest.mark.parametrize(
    "query",
    [
        "источник команд",
        "ИсточникКоманд",
        "command source",
        "CommandSource",
        "command_source",
    ],
)
def test_поиск_источника_команд_возвращает_каноническое_свойство(query):
    payload = get_managed_form_rules("terminology", query=query)

    assert [entry["canonical"] for entry in payload["matches"]] == [
        "command_source"
    ]


def test_поиск_неизвестного_термина_возвращает_пустой_результат_без_догадки():
    payload = get_managed_form_rules("terminology", query="трехмерная диаграмма")

    assert payload["matches"] == []
    assert payload["match_policy"] == "exact_alias_or_all_query_tokens"


def test_новый_element_или_property_нельзя_добавить_без_терминологии():
    payload = get_managed_form_rules("terminology")
    entries = payload["entries"]
    element_terms = set(entries["element"])
    property_terms = set(entries["property"])

    element_specs = set(get_args(models.ElementSpec)) | {
        models.PopupSpec,
        models.ButtonGroupSpec,
    }
    contract_specs = element_specs | set(get_args(models.AttributeTypeSpec)) | {
        models.ManagedFormSpec,
        models.LocalizedTextSpec,
        models.FormAttributeSpec,
        models.ValueTableColumnSpec,
        models.ChoiceListItemSpec,
        models.PageSpec,
        models.FormCommandSpec,
        models.CommandSourceSpec,
        models.FormEventSpec,
    }
    implemented_elements = {
        get_args(get_type_hints(spec)["kind"])[0] for spec in element_specs
    }
    implemented_properties = {
        property_name
        for spec in contract_specs
        for property_name in get_type_hints(spec)
    }

    assert element_terms == implemented_elements
    assert property_terms == implemented_properties


@pytest.mark.parametrize("query", ["", "   ", "я" * 161])
def test_невалидный_поисковый_запрос_отклоняется(query):
    with pytest.raises(FormsRuleQueryError, match="запрос"):
        get_managed_form_rules("terminology", query=query)


def test_query_нельзя_передать_в_другую_тему():
    with pytest.raises(FormsRuleQueryError, match="topic=terminology"):
        get_managed_form_rules("overview", query="форма")


def test_результат_поиска_можно_менять_не_повреждая_индекс():
    first = get_managed_form_rules("terminology", query="панель команд")
    first["matches"][0]["russian"].append("чужой термин")

    second = get_managed_form_rules("terminology", query="панель команд")

    assert "чужой термин" not in second["matches"][0]["russian"]


@pytest.mark.parametrize("topic", RULE_TOPICS)
def test_каждая_тема_json_совместима_и_ограничена(topic):
    payload = get_managed_form_rules(topic)
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True)

    assert payload["schema_version"] == 1
    assert payload["topic"] == topic
    assert len(encoded.encode("utf-8")) <= 12 * 1024
    assert payload["coverage"]["platform_import"] == "not_checked"
    assert payload["coverage"]["runtime_visual"] == "not_checked"


def test_overview_не_выдаёт_корпус_за_платформенную_гарантию():
    payload = get_managed_form_rules("overview")

    assert payload["target"] == {
        "format": "configurator_xml",
        "compiler_versions": ["2.16"],
        "other_versions": "inventory_only",
    }
    assert payload["evidence"] == {
        "kind": "anonymized_corpus",
        "scope": "multiple_configurations",
        "platform_requirement": "not_proven",
    }
    assert all(rule["evidence_level"] != "platform_guarantee" for rule in payload["rules"])


def test_specification_возвращает_тот_же_минимальный_пример_что_fixture():
    payload = get_managed_form_rules("specification")
    fixture = json.loads(
        (Path(__file__).with_name("fixtures") / "minimal_form.json").read_text(
            encoding="utf-8"
        )
    )

    assert payload["example"] == fixture
    assert "id" not in json.dumps(payload["example"], ensure_ascii=False)
    by_code = {rule["code"]: rule for rule in payload["rules"]}
    assert "зарезервированными словами BSL" in by_code["identifier_syntax"][
        "summary"
    ]
    assert "unknown_keys_rejected" in by_code


def test_elements_описывают_только_принятые_kinds_и_companions():
    payload = get_managed_form_rules("elements")

    assert payload["supported"] == {
        "root": [
            "usual_group",
            "input_field",
            "check_box_field",
            "label_decoration",
            "label_field",
            "radio_button_field",
            "button",
            "command_bar",
            "pages",
            "table",
        ],
        "recursive_children": [
            "usual_group",
            "input_field",
            "check_box_field",
            "label_decoration",
            "label_field",
            "radio_button_field",
            "button",
            "command_bar",
            "pages",
            "table",
        ],
        "page_representations": ["tabs_on_top"],
        "command_bar_children": ["button", "popup", "button_group"],
        "popup_children": ["button", "button_group"],
    }
    by_code = {rule["code"]: rule for rule in payload["rules"]}
    assert by_code["input_field_companions"]["value"] == [
        "ContextMenu",
        "ExtendedTooltip",
    ]
    assert by_code["button_companions"]["value"] == ["ExtendedTooltip"]
    assert by_code["check_box_requires_boolean"]["value"] == [
        "ContextMenu",
        "ExtendedTooltip",
    ]
    assert by_code["label_decoration_companions"]["value"] == [
        "ContextMenu",
        "ExtendedTooltip",
    ]
    assert by_code["label_field_companions"]["value"] == [
        "ContextMenu",
        "ExtendedTooltip",
    ]
    assert by_code["radio_button_field_structure"]["value"] == [
        "DataPath",
        "RadioButtonType",
        "ChoiceList",
        "ContextMenu",
        "ExtendedTooltip",
    ]
    assert by_code["command_bar_structure"]["value"] == [
        "ExtendedTooltip",
        "ChildItems<Button|Popup|ButtonGroup>",
    ]
    assert by_code["popup_structure"]["value"] == [
        "Title",
        "ExtendedTooltip",
        "ChildItems<Button|ButtonGroup>",
    ]
    assert by_code["button_group_structure"]["value"] == [
        "ExtendedTooltip",
        "ChildItems<Button>",
    ]
    assert by_code["command_source_structure"]["value"] == {
        "containers": ["command_bar", "popup", "button_group"],
        "kinds": ["form", "form_global_commands", "item"],
        "item_target": "existing_attribute_or_element",
        "children": "optional_with_command_source",
    }


def test_layout_оставляет_смысловое_решение_агенту_а_compiler_его_сохраняет():
    payload = get_managed_form_rules("layout")
    by_code = {rule["code"]: rule for rule in payload["rules"]}

    assert by_code["agent_owns_semantic_layout"]["status"] == "required"
    assert by_code["compiler_preserves_layout"]["status"] == "required"
    assert by_code["supported_layout_properties"]["value"][
        "group_orientation"
    ] == ["vertical", "horizontal", "always_horizontal"]
    assert payload["example"]["tree"][1].startswith("pages:")


def test_events_публикуют_закрытый_owner_aware_каталог_и_async_границу():
    payload = get_managed_form_rules("commands_events")

    assert payload["supported"] == {
        "platform_version": (
            "необязательная версия вида 8.3.23 или 8.3.23.1997; "
            "неизвестная версия использует ближайший профиль с предупреждением"
        ),
        "platform_profiles": [
            {
                "name": "managed_form_8_3_5_documented",
                "minimum": "8.3.5",
                "maximum": "8.3.5",
                "documented_versions": ["8.3.5.1570"],
                "event_profile": "8.3.5",
                "form_formats": [],
                "support": "documentation_only",
                "evidence": {
                    "documented": ["8.3.5.1570"],
                    "interval": "exact_only",
                },
            },
            {
                "name": "managed_form_2_16_modern",
                "minimum": "8.3.23",
                "maximum": "8.3.27",
                "documented_versions": [
                    "8.3.23.1997",
                    "8.3.26.15",
                    "8.3.27.2130",
                ],
                "event_profile": "modern",
                "form_formats": ["2.16"],
                "support": "compiler",
                "evidence": {
                    "documented": [
                        "8.3.23.1997",
                        "8.3.26.15",
                        "8.3.27.2130",
                    ],
                    "interval": "inferred_between_confirmed_versions",
                },
            },
        ],
        "event_owner": "поле owner отсутствует для формы либо содержит имя элемента",
        "form_events": [
            "OnCreateAtServer",
            "OnOpen",
            "NotificationProcessing",
            "ExternalEvent",
            "FillCheckProcessingAtServer",
            "BeforeClose",
            "OnClose",
            "ChoiceProcessing",
        ],
        "object_form_events": [
            "OnReadAtServer",
            "BeforeWrite",
            "BeforeWriteAtServer",
            "OnWriteAtServer",
            "AfterWriteAtServer",
            "AfterWrite",
        ],
        "element_events": {
            "input_field": [
                "OnChange",
                "StartChoice",
                "Clearing",
                "ChoiceProcessing",
                "AutoComplete",
                "TextEditEnd",
                "Opening",
            ],
            "check_box_field": ["OnChange"],
            "label_decoration": ["Click", "URLProcessing"],
            "label_field": ["OnChange", "Click", "URLProcessing"],
            "radio_button_field": ["OnChange"],
            "pages": ["OnCurrentPageChange"],
            "table": [
                "Selection",
                "OnActivateRow",
                "ChoiceProcessing",
                "OnStartEdit",
                "BeforeAddRow",
                "BeforeRowChange",
                "BeforeDeleteRow",
                "AfterDeleteRow",
                "OnChange",
                "OnEditEnd",
            ],
        },
        "command_reference": "Form.Command.<name>",
        "standard_commands": {
            "form": ["Help", "Close", "CustomizeForm"],
            "object_form": ["Write", "WriteAndClose"],
            "document_form": ["Post", "PostAndClose", "UndoPosting"],
            "table": ["Add", "Delete", "MoveUp", "MoveDown"],
        },
        "async": "Асинх и Ждать только в клиентском контексте платформы 8.3.18+",
    }
    by_code = {rule["code"]: rule for rule in payload["rules"]}
    assert by_code["on_create_at_server_stub"]["value"] == {
        "directive": "&НаСервере",
        "parameters": ["Отказ", "СтандартнаяОбработка"],
    }
    assert by_code["unknown_event_not_checked"]["status"] == "boundary"
    assert by_code["owner_aware_event_catalog"]["status"] == "supported"
    assert by_code["object_form_lifecycle_catalog"]["evidence_level"] == (
        "platform_documentation"
    )
    assert by_code["object_form_lifecycle_catalog"]["value"][
        "BeforeWriteAtServer"
    ] == {
        "directive": "&НаСервере",
        "parameters": ["Отказ", "ТекущийОбъект", "ПараметрыЗаписи"],
    }
    assert by_code["document_posting_events_not_form_events"]["status"] == (
        "boundary"
    )
    assert by_code["managed_form_2_16_event_profile"]["status"] == "boundary"
    assert by_code["platform_version_matrix"]["status"] == "supported"
    assert by_code["standard_command_catalog"]["value"]["table"] == [
        "Add",
        "Delete",
        "MoveUp",
        "MoveDown",
    ]
    assert by_code["standard_command_catalog"]["value"]["document_form"] == [
        "Post",
        "PostAndClose",
        "UndoPosting",
    ]
    assert by_code["async_client_contract"]["status"] == "required"
    assert by_code["no_synchronous_file_exists_on_client"]["status"] == "required"
    assert "Файл.Существует()" in by_code[
        "no_synchronous_file_exists_on_client"
    ]["summary"]


def test_неизвестная_тема_отклоняется_с_перечнем_доступных():
    with pytest.raises(FormsRuleQueryError, match="overview.*diagnostics"):
        get_managed_form_rules("all")


def test_ответ_можно_менять_не_повреждая_реестр_следующего_запроса():
    first = get_managed_form_rules("elements")
    first["rules"][2]["value"].append("ЧужойУзел")

    second = get_managed_form_rules("elements")

    assert second["rules"][2]["value"] == ["ContextMenu", "ExtendedTooltip"]


def test_ответ_не_раскрывает_имена_корпусов_и_локальные_пути():
    rendered = json.dumps(
        [get_managed_form_rules(topic) for topic in RULE_TOPICS],
        ensure_ascii=False,
    )

    assert "Розниц" not in rendered
    assert "data/" not in rendered
    assert "/Users/" not in rendered
    assert ".zip" not in rendered
