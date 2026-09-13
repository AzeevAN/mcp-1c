from __future__ import annotations

import json
from pathlib import Path

import pytest

from mcp1c.capability_modules.forms.rules import (
    RULE_TOPICS,
    FormsRuleQueryError,
    get_managed_form_rules,
)


def test_темы_правил_закрыты_и_имеют_стабильный_порядок():
    assert RULE_TOPICS == (
        "overview",
        "specification",
        "elements",
        "attributes",
        "layout",
        "commands_events",
        "diagnostics",
    )


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
            "button",
            "pages",
            "table",
        ],
        "recursive_children": [
            "usual_group",
            "input_field",
            "check_box_field",
            "button",
            "pages",
            "table",
        ],
        "page_representations": ["tabs_on_top"],
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
        "event_owner": "поле owner отсутствует для формы либо содержит имя элемента",
        "form_events": [
            "OnCreateAtServer",
            "OnOpen",
            "NotificationProcessing",
            "ExternalEvent",
            "FillCheckProcessingAtServer",
        ],
        "element_events": {
            "input_field": ["OnChange"],
            "check_box_field": ["OnChange"],
            "pages": ["OnCurrentPageChange"],
            "table": ["Selection", "OnActivateRow"],
        },
        "command_reference": "Form.Command.<name>",
        "async": "Асинх и Ждать только в клиентском контексте платформы 8.3.18+",
    }
    by_code = {rule["code"]: rule for rule in payload["rules"]}
    assert by_code["on_create_at_server_stub"]["value"] == {
        "directive": "&НаСервере",
        "parameters": ["Отказ", "СтандартнаяОбработка"],
    }
    assert by_code["unknown_event_not_checked"]["status"] == "boundary"
    assert by_code["owner_aware_event_catalog"]["status"] == "supported"
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
