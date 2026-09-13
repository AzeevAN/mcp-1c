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
RICH_FIXTURE = Path(__file__).with_name("fixtures") / "file_import_form.json"


def _payload() -> dict:
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


def _rich_payload() -> dict:
    return json.loads(RICH_FIXTURE.read_text(encoding="utf-8"))


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


def test_спецификация_импорта_поддерживает_типы_страницы_таблицу_и_выбор():
    form = parse_managed_form_spec(_rich_payload())

    assert [item.type.kind for item in form.attributes[3:7]] == [
        "string",
        "string",
        "boolean",
        "number",
    ]
    assert form.attributes[12].type.kind == "date"
    assert form.attributes[13].type.kind == "value_table"
    source_group = form.elements[0].children[0]
    assert source_group.orientation == "always_horizontal"
    assert source_group.representation == "none"
    assert source_group.show_title is False
    assert source_group.horizontal_stretch is True
    assert source_group.children[0].horizontal_stretch is True
    assert source_group.children[1].choice_list[2].value == "XLSX"
    pages = form.elements[0].children[1]
    assert pages.kind == "pages"
    assert pages.horizontal_stretch is True
    assert pages.vertical_stretch is True
    assert [page.name for page in pages.pages] == [
        "СтраницаДанные",
        "СтраницаПредпросмотр",
        "СтраницаНастройки",
    ]
    assert len(pages.pages[0].children[0].columns) == 10
    assert pages.pages[0].children[0].vertical_stretch is True
    assert pages.pages[2].children[2].kind == "check_box_field"
    assert pages.pages[2].children[5].kind == "check_box_field"


def test_статическая_надпись_имеет_собственный_kind_и_не_требует_data_path():
    payload = _payload()
    payload["elements"].append(
        {
            "kind": "label_decoration",
            "name": "Пояснение",
            "title": {"ru": "Проверьте параметры"},
            "hyperlink": True,
            "horizontal_stretch": False,
            "vertical_stretch": True,
        }
    )
    payload["events"].extend(
        [
            {
                "owner": "Пояснение",
                "event": "Click",
                "handler": "ПояснениеНажатие",
            },
            {
                "owner": "Пояснение",
                "event": "URLProcessing",
                "handler": "ПояснениеОбработкаСсылки",
            },
        ]
    )

    form = parse_managed_form_spec(payload)

    label = form.elements[-1]
    assert label.kind == "label_decoration"
    assert label.title.ru == "Проверьте параметры"
    assert label.hyperlink is True
    assert label.horizontal_stretch is False
    assert label.vertical_stretch is True


def test_поле_надписи_имеет_data_path_и_собственные_свойства():
    payload = _payload()
    payload["elements"].append(
        {
            "kind": "label_field",
            "name": "Итог",
            "data_path": "ПервоеЗначение",
            "title": {"ru": "Итог"},
            "hyperlink": True,
            "read_only": True,
            "horizontal_stretch": False,
            "vertical_stretch": True,
        }
    )

    form = parse_managed_form_spec(payload)

    field = form.elements[-1]
    assert field.kind == "label_field"
    assert field.data_path == "ПервоеЗначение"
    assert field.title.ru == "Итог"
    assert field.hyperlink is True
    assert field.read_only is True
    assert field.horizontal_stretch is False
    assert field.vertical_stretch is True

    table_payload = _rich_payload()
    column = table_payload["elements"][0]["children"][1]["pages"][0][
        "children"
    ][0]["columns"][0]
    column["kind"] = "label_field"
    column["hyperlink"] = True

    table_form = parse_managed_form_spec(table_payload)
    table_column = table_form.elements[0].children[1].pages[0].children[0].columns[0]

    assert table_column.kind == "label_field"
    assert table_column.hyperlink is True


def test_переключатель_требует_данные_и_варианты():
    payload = _payload()
    payload["elements"].append(
        {
            "kind": "radio_button_field",
            "name": "Режим",
            "data_path": "ПервоеЗначение",
            "title": {"ru": "Режим"},
            "radio_button_type": "tumbler",
            "columns_count": 2,
            "choice_list": [
                {"value": "A", "presentation": {"ru": "Первый"}},
                {"value": "B", "presentation": {"ru": "Второй"}},
            ],
        }
    )

    form = parse_managed_form_spec(payload)
    field = form.elements[-1]

    assert field.kind == "radio_button_field"
    assert field.radio_button_type == "tumbler"
    assert field.columns_count == 2
    assert [item.value for item in field.choice_list] == ["A", "B"]


def test_панель_команд_содержит_только_явные_кнопки():
    payload = _payload()
    payload["elements"].append(
        {
            "kind": "command_bar",
            "name": "Действия",
            "title": {"ru": "Действия"},
            "horizontal_location": "right",
            "horizontal_stretch": True,
            "children": [
                {
                    "kind": "button",
                    "name": "ПроверитьНаПанели",
                    "command": "Проверить",
                }
            ],
        }
    )

    form = parse_managed_form_spec(payload)
    bar = form.elements[-1]

    assert bar.kind == "command_bar"
    assert bar.title.ru == "Действия"
    assert bar.horizontal_location == "right"
    assert bar.horizontal_stretch is True
    assert [item.name for item in bar.children] == ["ПроверитьНаПанели"]

    payload["elements"][-1]["children"] = []
    with pytest.raises(FormsContractError) as caught:
        parse_managed_form_spec(payload)

    assert (
        "command_bar_buttons_required",
        "$.elements[1].children",
    ) in _codes(caught.value)

    payload["elements"][-1]["children"] = [
        {
            "kind": "label_decoration",
            "name": "НедопустимоеПояснение",
        }
    ]
    with pytest.raises(FormsContractError) as caught:
        parse_managed_form_spec(payload)

    assert (
        "unsupported_command_bar_child_kind",
        "$.elements[1].children[0].kind",
    ) in _codes(caught.value)


def test_таблица_отклоняет_путь_к_необъявленной_колонке():
    payload = _rich_payload()
    payload["elements"][0]["children"][1]["pages"][0]["children"][0][
        "columns"
    ][0]["data_path"] = "ТаблицаДанных.Неизвестная"

    with pytest.raises(FormsContractError) as caught:
        parse_managed_form_spec(payload)

    assert (
        "unresolved_table_column",
        "$.elements[0].children[1].pages[0].children[0].columns[0].data_path",
    ) in _codes(caught.value)


def test_флажок_требует_булевый_реквизит():
    payload = _rich_payload()
    payload["elements"][0]["children"][1]["pages"][2]["children"][2][
        "data_path"
    ] = "Кодировка"

    with pytest.raises(FormsContractError) as caught:
        parse_managed_form_spec(payload)

    assert (
        "check_box_requires_boolean",
        "$.elements[0].children[1].pages[2].children[2].data_path",
    ) in _codes(caught.value)


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
    assert definition["properties"]["platform_version"]["type"] == "string"
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


@pytest.mark.parametrize("version", ["8.3.23", "8.3.24", "8.3.27.2130"])
def test_доказанный_интервал_платформы_принимается_явно(version):
    payload = _payload()
    payload["platform_version"] = version

    form = parse_managed_form_spec(payload)

    assert form.platform_version == version
    assert form.event_profile == "modern"


@pytest.mark.parametrize(
    ("version", "event_profile"),
    [("8.3.16", "8.3.5"), ("8.3.22", "8.3.5"), ("8.3.28", "modern")],
)
def test_неподтверждённая_версия_не_отклоняется(version, event_profile):
    payload = _payload()
    payload["platform_version"] = version

    form = parse_managed_form_spec(payload)

    assert form.platform_version == version
    assert form.event_profile == event_profile


def test_невалидная_версия_по_прежнему_отклоняется():
    payload = _payload()
    payload["platform_version"] = "8.3"

    with pytest.raises(FormsContractError) as caught:
        parse_managed_form_spec(payload)

    assert ("invalid_platform_version", "$.platform_version") in _codes(
        caught.value
    )


def test_8_3_5_разрешается_как_непроверенный_form_xml_2_16():
    payload = _payload()
    payload["platform_version"] = "8.3.5.1570"

    form = parse_managed_form_spec(payload)

    assert form.platform_version == "8.3.5.1570"
    assert form.event_profile == "8.3.5"


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
                {"kind": "calendar_field"}
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
                {"kind": "uuid"}
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
            lambda value: value["events"][0].update({"event": "UnknownEvent"}),
            ("unsupported_owner_event", "$.events[0].event"),
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


def test_объектный_главный_реквизит_разрешает_путь_к_его_полю():
    payload = _payload()
    payload["attributes"][0] = {
        "name": "Объект",
        "type": {
            "kind": "metadata_object",
            "object": "Обработка.НоваяОбработка",
        },
        "main": True,
    }
    field = payload["elements"][0]["children"][0]
    field.update({"name": "Комментарий", "data_path": "Объект.Комментарий"})

    form = parse_managed_form_spec(payload)

    assert form.attributes[0].type.object == "Обработка.НоваяОбработка"
    assert form.attributes[0].main is True


@pytest.mark.parametrize(
    "object_name",
    ["НеизвестныйВид.Объект", "Обработка", "Обработка.Неверное-Имя"],
)
def test_некорректный_объектный_тип_отклоняется(object_name):
    payload = _payload()
    payload["attributes"][0]["type"] = {
        "kind": "metadata_object",
        "object": object_name,
    }

    with pytest.raises(FormsContractError) as caught:
        parse_managed_form_spec(payload)

    assert ("invalid_metadata_object", "$.attributes[0].type.object") in _codes(
        caught.value
    )


def test_ссылочный_и_составной_типы_разбираются_в_типизированную_модель():
    payload = _payload()
    payload["attributes"][0]["type"] = {
        "kind": "metadata_reference",
        "object": "Справочник.Товары",
    }
    payload["attributes"][1]["type"] = {
        "kind": "composite",
        "variants": [
            {"kind": "metadata_reference", "object": "Документ.Заказ"},
            {"kind": "string", "length": 50},
        ],
    }

    form = parse_managed_form_spec(payload)

    assert form.attributes[0].type.object == "Справочник.Товары"
    assert [variant.kind for variant in form.attributes[1].type.variants] == [
        "metadata_reference",
        "string",
    ]


@pytest.mark.parametrize(
    ("value_type", "expected"),
    [
        (
            {"kind": "metadata_reference", "object": "Обработка.Импорт"},
            ("invalid_metadata_reference", "$.attributes[0].type.object"),
        ),
        (
            {"kind": "composite", "variants": [{"kind": "boolean"}]},
            ("composite_type_too_small", "$.attributes[0].type.variants"),
        ),
        (
            {
                "kind": "composite",
                "variants": [{"kind": "boolean"}, {"kind": "boolean"}],
            },
            (
                "duplicate_composite_type_variant",
                "$.attributes[0].type.variants[1]",
            ),
        ),
    ],
)
def test_некорректный_ссылочный_или_составной_тип_отклоняется(value_type, expected):
    payload = _payload()
    payload["attributes"][0]["type"] = value_type

    with pytest.raises(FormsContractError) as caught:
        parse_managed_form_spec(payload)

    assert expected in _codes(caught.value)


def test_dynamic_list_разрешает_таблицу_и_вложенные_поля():
    payload = _rich_payload()
    payload["attributes"][13] = {
        "name": "Список",
        "type": {
            "kind": "dynamic_list",
            "main_table": "Справочник.Товары",
            "dynamic_data_read": True,
        },
        "main": True,
    }
    table = payload["elements"][0]["children"][1]["pages"][0]["children"][0]
    table["data_path"] = "Список"
    for column in table["columns"]:
        column["data_path"] = "Список." + column["name"]

    form = parse_managed_form_spec(payload)

    assert form.attributes[13].type.main_table == "Справочник.Товары"
    assert form.attributes[13].type.dynamic_data_read is True


def test_dynamic_list_с_непрямой_или_неизвестной_таблицей_отклоняется():
    payload = _payload()
    payload["attributes"][0]["type"] = {
        "kind": "dynamic_list",
        "main_table": "Справочник.Товары.Иерархия",
        "dynamic_data_read": True,
    }

    with pytest.raises(FormsContractError) as caught:
        parse_managed_form_spec(payload)

    assert (
        "invalid_dynamic_list_main_table",
        "$.attributes[0].type.main_table",
    ) in _codes(caught.value)


def test_id_не_является_частью_публичной_спецификации():
    payload = _payload()
    payload["commands"][0]["id"] = 1

    with pytest.raises(FormsContractError) as caught:
        parse_managed_form_spec(payload)

    assert ("unknown_key", "$.commands[0].id") in _codes(caught.value)


def test_стандартные_команды_формы_и_таблицы_не_требуют_custom_command():
    payload = _payload()
    payload["commands"] = []
    payload["elements"][0]["children"][2] = {
        "kind": "button",
        "name": "ЗакрытьФорму",
        "command": "Close",
        "command_kind": "form_standard",
    }

    form = parse_managed_form_spec(payload)

    assert form.commands == ()


def test_команда_записи_разрешена_для_главного_объектного_реквизита():
    payload = _payload()
    payload["attributes"][0] = {
        "name": "Объект",
        "type": {"kind": "metadata_object", "object": "Справочник.Товары"},
        "main": True,
    }
    payload["elements"][0]["children"][0]["data_path"] = "Объект.Наименование"
    payload["elements"][0]["children"][2] = {
        "kind": "button",
        "name": "ЗаписатьИЗакрыть",
        "command": "WriteAndClose",
        "command_kind": "form_standard",
    }

    form = parse_managed_form_spec(payload)

    assert form.elements[0].children[2].command == "WriteAndClose"


@pytest.mark.parametrize("command", ["Post", "PostAndClose", "UndoPosting"])
def test_команда_проведения_разрешена_только_для_главного_документа(command):
    payload = _payload()
    payload["attributes"][0] = {
        "name": "Объект",
        "type": {"kind": "metadata_object", "object": "Справочник.Товары"},
        "main": True,
    }
    payload["elements"][0]["children"][0]["data_path"] = "Объект.Наименование"
    payload["elements"][0]["children"][2] = {
        "kind": "button",
        "name": "КомандаДокумента",
        "command": command,
        "command_kind": "form_standard",
    }
    with pytest.raises(FormsContractError) as caught:
        parse_managed_form_spec(payload)
    assert (
        "document_standard_command_requires_document_main_object",
        "$.elements[0].children[2].command",
    ) in _codes(caught.value)

    payload["attributes"][0]["type"]["object"] = "Документ.ТестовыйДокумент"
    form = parse_managed_form_spec(payload)
    assert form.elements[0].children[2].command == command


def test_события_жизненного_цикла_разрешены_только_объектной_форме():
    payload = _payload()
    payload["events"] = [
        {"event": "OnReadAtServer", "handler": "ПриЧтенииНаСервере"}
    ]
    with pytest.raises(FormsContractError) as caught:
        parse_managed_form_spec(payload)
    assert ("object_event_requires_main_object", "$.events[0].event") in _codes(
        caught.value
    )

    payload["attributes"][0] = {
        "name": "Объект",
        "type": {"kind": "metadata_object", "object": "Справочник.Товары"},
        "main": True,
    }
    payload["elements"][0]["children"][0]["data_path"] = "Объект.Наименование"
    form = parse_managed_form_spec(payload)
    assert form.events[0].event == "OnReadAtServer"


@pytest.mark.parametrize(
    ("mutate", "expected"),
    [
        (
            lambda button: button.update(
                {"command": "Write", "command_kind": "form_standard"}
            ),
            (
                "object_standard_command_requires_main_object",
                "$.elements[0].children[2].command",
            ),
        ),
        (
            lambda button: button.update(
                {"command": "Add", "command_kind": "item_standard"}
            ),
            ("missing_key", "$.elements[0].children[2].command_owner"),
        ),
        (
            lambda button: button.update(
                {
                    "command": "Add",
                    "command_kind": "item_standard",
                    "command_owner": "ПервоеЗначение",
                }
            ),
            (
                "unsupported_standard_command_owner",
                "$.elements[0].children[2].command_owner",
            ),
        ),
    ],
)
def test_закрытый_каталог_стандартных_команд_отклоняет_недоказанные_ссылки(
    mutate, expected
):
    payload = _payload()
    button = payload["elements"][0]["children"][2]
    mutate(button)

    with pytest.raises(FormsContractError) as caught:
        parse_managed_form_spec(payload)

    assert expected in _codes(caught.value)


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
