from __future__ import annotations

import copy
import json
from pathlib import Path
from xml.etree import ElementTree as ET

import pytest

from mcp1c.capability_modules.forms.compiler import compile_managed_form
from mcp1c.capability_modules.forms.models import FormsContractError
from mcp1c.bsl_lex import разобрать


FIXTURES = Path(__file__).with_name("fixtures")
LOGFORM = "http://v8.1c.ru/8.3/xcf/logform"


def _payload() -> dict:
    return json.loads((FIXTURES / "minimal_form.json").read_text(encoding="utf-8"))


def _rich_payload() -> dict:
    return json.loads(
        (FIXTURES / "file_import_form.json").read_text(encoding="utf-8")
    )


def _expected(name: str) -> str:
    # Git хранит fixtures с LF, а Конфигуратор во всём доказательном корпусе
    # выгружает текстовые артефакты с CRLF.
    return (FIXTURES / name).read_text(encoding="utf-8").replace("\n", "\r\n")


def _artifacts(result) -> dict[str, object]:
    return {artifact.path: artifact for artifact in result.artifacts}


def test_compiler_byte_deterministic_и_совпадает_с_обезличенным_fixture():
    first = compile_managed_form(_payload())
    second = compile_managed_form(_payload())

    assert first.to_dict() == second.to_dict()
    artifacts = _artifacts(first)
    assert artifacts["Forms/ФормаПараметров/Ext/Form.xml"].content == _expected(
        "minimal_form.xml"
    )
    assert artifacts[
        "Forms/ФормаПараметров/Ext/Form/Module.bsl"
    ].content == _expected("minimal_module.bsl")


def test_неизвестная_версия_компилируется_с_предупреждением():
    payload = _payload()
    payload["platform_version"] = "8.3.28"

    result = compile_managed_form(payload)

    assert result.status == "compiled"
    assert any(
        item.code == "platform_compatibility_unverified"
        and item.status == "warning"
        for item in result.diagnostics
    )


def test_отсутствующая_версия_компилируется_с_явной_неопределённостью():
    result = compile_managed_form(_payload())

    assert any(
        item.code == "platform_version_unspecified"
        and item.status == "warning"
        for item in result.diagnostics
    )


def test_compiler_создаёт_богатую_форму_импорта_детерминированно():
    first = compile_managed_form(_rich_payload())
    second = compile_managed_form(_rich_payload())

    assert first.to_dict() == second.to_dict()
    root = ET.fromstring(first.artifacts[0].content)
    q = lambda name: f"{{{LOGFORM}}}{name}"
    assert len(list(root.iter(q("Pages")))) == 1
    assert len(list(root.iter(q("Page")))) == 3
    assert len(list(root.iter(q("Table")))) == 1
    assert len(list(root.iter(q("CheckBoxField")))) == 2
    assert len(list(root.iter(q("ChoiceList")))) == 3
    assert len(list(root.iter(q("Column")))) == 10
    assert any((node.text or "") == "xs:boolean" for node in root.iter())
    assert any((node.text or "") == "xs:decimal" for node in root.iter())
    assert any((node.text or "") == "xs:dateTime" for node in root.iter())
    assert any((node.text or "") == "v8:ValueTable" for node in root.iter())
    assert any((node.text or "") == "AlwaysHorizontal" for node in root.iter())
    assert len(list(root.iter(q("HorizontalStretch")))) >= 5
    assert len(list(root.iter(q("VerticalStretch")))) >= 4


def test_compiler_создаёт_настраиваемую_автоматическую_панель_таблицы():
    payload = _rich_payload()
    table = payload["elements"][0]["children"][1]["pages"][0]["children"][0]
    table["auto_command_bar"] = {
        "kind": "auto_command_bar",
        "autofill": False,
        "children": [
            {
                "kind": "button",
                "name": "ДобавитьСтроку",
                "command": "Add",
                "command_kind": "item_standard",
                "command_owner": "ТаблицаДанныхПоле",
            },
            {
                "kind": "popup",
                "name": "Дополнительно",
                "title": {"ru": "Дополнительно"},
                "children": [
                    {
                        "kind": "button",
                        "name": "ВыполнитьИмпортИзМеню",
                        "command": "ВыполнитьИмпорт",
                    }
                ],
            },
            {
                "kind": "button_group",
                "name": "Строки",
                "children": [
                    {
                        "kind": "button",
                        "name": "УдалитьСтроку",
                        "command": "Delete",
                        "command_kind": "item_standard",
                        "command_owner": "ТаблицаДанныхПоле",
                    }
                ],
            },
        ],
    }

    xml = compile_managed_form(payload).artifacts[0].content
    root = ET.fromstring(xml)
    q = lambda name: f"{{{LOGFORM}}}{name}"
    table_node = next(
        node
        for node in root.iter(q("Table"))
        if node.attrib.get("name") == "ТаблицаДанныхПоле"
    )
    bar = table_node.find(q("AutoCommandBar"))

    assert bar is not None
    assert [child.tag for child in bar] == [q("Autofill"), q("ChildItems")]
    assert bar.find(q("Autofill")).text == "false"
    children = bar.find(q("ChildItems"))
    assert [child.tag for child in children] == [
        q("Button"),
        q("Popup"),
        q("ButtonGroup"),
    ]
    assert (
        children.find(q("Button")).find(q("CommandName")).text
        == "Form.Item.ТаблицаДанныхПоле.StandardCommand.Add"
    )


def test_compiler_создаёт_настраиваемое_контекстное_меню_таблицы():
    payload = _rich_payload()
    table = payload["elements"][0]["children"][1]["pages"][0]["children"][0]
    table["context_menu"] = {
        "kind": "context_menu",
        "autofill": False,
        "children": [
            {
                "kind": "button",
                "name": "УдалитьСтрокуИзМеню",
                "command": "Delete",
                "command_kind": "item_standard",
                "command_owner": "ТаблицаДанныхПоле",
            },
            {
                "kind": "popup",
                "name": "ДополнительноВМеню",
                "title": {"ru": "Дополнительно"},
                "children": [
                    {
                        "kind": "button",
                        "name": "ВыполнитьИмпортИзКонтекстногоМеню",
                        "command": "ВыполнитьИмпорт",
                    }
                ],
            },
        ],
    }

    xml = compile_managed_form(payload).artifacts[0].content
    root = ET.fromstring(xml)
    q = lambda name: f"{{{LOGFORM}}}{name}"
    table_node = next(
        node
        for node in root.iter(q("Table"))
        if node.attrib.get("name") == "ТаблицаДанныхПоле"
    )
    menu = table_node.find(q("ContextMenu"))

    assert menu is not None
    assert [child.tag for child in menu] == [q("Autofill"), q("ChildItems")]
    assert menu.find(q("Autofill")).text == "false"
    children = menu.find(q("ChildItems"))
    assert [child.tag for child in children] == [q("Button"), q("Popup")]
    assert (
        children.find(q("Button")).find(q("CommandName")).text
        == "Form.Item.ТаблицаДанныхПоле.StandardCommand.Delete"
    )


def test_compiler_создаёт_label_decoration_с_companions_и_событиями():
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

    result = compile_managed_form(payload)
    root = ET.fromstring(result.artifacts[0].content)
    q = lambda name: f"{{{LOGFORM}}}{name}"
    label = next(
        node
        for node in root.iter(q("LabelDecoration"))
        if node.attrib.get("name") == "Пояснение"
    )

    assert [child.tag.rsplit("}", 1)[-1] for child in label] == [
        "HorizontalStretch",
        "VerticalStretch",
        "Title",
        "Hyperlink",
        "ContextMenu",
        "ExtendedTooltip",
        "Events",
    ]
    assert label.find(q("ContextMenu")).attrib["name"] == "ПояснениеКонтекстноеМеню"
    assert label.find(q("ExtendedTooltip")).attrib["name"] == (
        "ПояснениеРасширеннаяПодсказка"
    )
    assert [event.attrib["name"] for event in label.find(q("Events"))] == [
        "Click",
        "URLProcessing",
    ]
    module = result.artifacts[1].content
    assert "Процедура ПояснениеНажатие(Элемент)" in module
    assert (
        "Процедура ПояснениеОбработкаСсылки(Элемент, "
        "НавигационнаяСсылкаФорматированнойСтроки, СтандартнаяОбработка)"
        in module
    )


def test_compiler_создаёт_label_field_с_hiperlink_и_событиями():
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
    payload["events"].extend(
        [
            {"owner": "Итог", "event": "OnChange", "handler": "ИтогИзменён"},
            {"owner": "Итог", "event": "Click", "handler": "ИтогНажатие"},
            {
                "owner": "Итог",
                "event": "URLProcessing",
                "handler": "ИтогОбработкаСсылки",
            },
        ]
    )

    result = compile_managed_form(payload)
    root = ET.fromstring(result.artifacts[0].content)
    q = lambda name: f"{{{LOGFORM}}}{name}"
    field = next(
        node
        for node in root.iter(q("LabelField"))
        if node.attrib.get("name") == "Итог"
    )

    assert [child.tag.rsplit("}", 1)[-1] for child in field] == [
        "DataPath",
        "Title",
        "HorizontalStretch",
        "VerticalStretch",
        "Hiperlink",
        "ReadOnly",
        "ContextMenu",
        "ExtendedTooltip",
        "Events",
    ]
    assert field.find(q("Hiperlink")).text == "true"
    assert [event.attrib["name"] for event in field.find(q("Events"))] == [
        "OnChange",
        "Click",
        "URLProcessing",
    ]
    module = result.artifacts[1].content
    assert "Процедура ИтогИзменён(Элемент)" in module
    assert "Процедура ИтогНажатие(Элемент, СтандартнаяОбработка)" in module
    assert (
        "Процедура ИтогОбработкаСсылки(Элемент, "
        "НавигационнаяСсылкаФорматированнойСтроки, СтандартнаяОбработка)"
        in module
    )


def test_compiler_создаёт_radio_button_field_с_choice_list():
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
    payload["events"].append(
        {"owner": "Режим", "event": "OnChange", "handler": "РежимИзменён"}
    )

    result = compile_managed_form(payload)
    root = ET.fromstring(result.artifacts[0].content)
    q = lambda name: f"{{{LOGFORM}}}{name}"
    field = next(node for node in root.iter(q("RadioButtonField")))

    assert [child.tag.rsplit("}", 1)[-1] for child in field] == [
        "DataPath",
        "Title",
        "RadioButtonType",
        "ColumnsCount",
        "ChoiceList",
        "ContextMenu",
        "ExtendedTooltip",
        "Events",
    ]
    assert field.find(q("RadioButtonType")).text == "Tumbler"
    assert len(field.find(q("ChoiceList"))) == 2
    assert "Процедура РежимИзменён(Элемент)" in result.artifacts[1].content


def test_compiler_создаёт_command_bar_с_явными_кнопками():
    payload = _payload()
    payload["elements"].append(
        {
            "kind": "command_bar",
            "name": "Действия",
            "title": {"ru": "Действия"},
            "horizontal_location": "right",
            "horizontal_stretch": True,
            "vertical_stretch": False,
            "children": [
                {
                    "kind": "button",
                    "name": "ПроверитьНаПанели",
                    "command": "Проверить",
                }
            ],
        }
    )

    result = compile_managed_form(payload)
    root = ET.fromstring(result.artifacts[0].content)
    q = lambda name: f"{{{LOGFORM}}}{name}"
    bar = next(node for node in root.iter(q("CommandBar")))

    assert [child.tag.rsplit("}", 1)[-1] for child in bar] == [
        "Title",
        "HorizontalLocation",
        "HorizontalStretch",
        "VerticalStretch",
        "ExtendedTooltip",
        "ChildItems",
    ]
    assert bar.find(q("HorizontalLocation")).text == "Right"
    assert [
        child.tag.rsplit("}", 1)[-1]
        for child in bar.find(q("ChildItems"))
    ] == ["Button"]


def test_compiler_создаёт_popup_внутри_command_bar():
    payload = _payload()
    payload["elements"].append(
        {
            "kind": "command_bar",
            "name": "Действия",
            "children": [
                {
                    "kind": "popup",
                    "name": "Дополнительно",
                    "title": {"ru": "Дополнительно"},
                    "children": [
                        {
                            "kind": "button",
                            "name": "ПроверитьДополнительно",
                            "command": "Проверить",
                        }
                    ],
                }
            ],
        }
    )

    root = ET.fromstring(compile_managed_form(payload).artifacts[0].content)
    q = lambda name: f"{{{LOGFORM}}}{name}"
    popup = next(node for node in root.iter(q("Popup")))

    assert [child.tag.rsplit("}", 1)[-1] for child in popup] == [
        "Title",
        "ExtendedTooltip",
        "ChildItems",
    ]
    assert [child.tag.rsplit("}", 1)[-1] for child in popup.find(q("ChildItems"))] == [
        "Button"
    ]


def test_compiler_создаёт_button_group_внутри_command_bar():
    payload = _payload()
    payload["elements"].append(
        {
            "kind": "command_bar",
            "name": "Действия",
            "children": [
                {
                    "kind": "button_group",
                    "name": "ОсновныеДействия",
                    "title": {"ru": "Основные действия"},
                    "representation": "compact",
                    "children": [
                        {
                            "kind": "button",
                            "name": "ПроверитьВГруппе",
                            "command": "Проверить",
                        }
                    ],
                }
            ],
        }
    )

    root = ET.fromstring(compile_managed_form(payload).artifacts[0].content)
    q = lambda name: f"{{{LOGFORM}}}{name}"
    group = next(node for node in root.iter(q("ButtonGroup")))

    assert [child.tag.rsplit("}", 1)[-1] for child in group] == [
        "Title",
        "Representation",
        "ExtendedTooltip",
        "ChildItems",
    ]
    assert group.find(q("Representation")).text == "Compact"
    assert [child.tag.rsplit("}", 1)[-1] for child in group.find(q("ChildItems"))] == [
        "Button"
    ]


def test_compiler_создаёт_источники_команд_с_явными_и_пустыми_детьми():
    payload = _payload()
    payload["elements"].append(
        {
            "kind": "command_bar",
            "name": "Действия",
            "command_source": {"kind": "form"},
            "children": [
                {
                    "kind": "button_group",
                    "name": "ГлобальныеКоманды",
                    "command_source": {"kind": "form_global_commands"},
                    "children": [],
                },
                {
                    "kind": "popup",
                    "name": "КомандыРеквизита",
                    "title": {"ru": "Команды реквизита"},
                    "command_source": {
                        "kind": "item",
                        "item": "ПервоеЗначение",
                    },
                    "children": [],
                },
            ],
        }
    )

    root = ET.fromstring(compile_managed_form(payload).artifacts[0].content)
    q = lambda name: f"{{{LOGFORM}}}{name}"
    bar = next(node for node in root.iter(q("CommandBar")))
    group = next(node for node in root.iter(q("ButtonGroup")))
    popup = next(node for node in root.iter(q("Popup")))

    assert [child.tag.rsplit("}", 1)[-1] for child in bar] == [
        "CommandSource",
        "ExtendedTooltip",
        "ChildItems",
    ]
    assert bar.find(q("CommandSource")).text == "Form"
    assert [child.tag.rsplit("}", 1)[-1] for child in group] == [
        "CommandSource",
        "ExtendedTooltip",
    ]
    assert group.find(q("CommandSource")).text == "FormCommandPanelGlobalCommands"
    assert [child.tag.rsplit("}", 1)[-1] for child in popup] == [
        "Title",
        "CommandSource",
        "ExtendedTooltip",
    ]
    assert popup.find(q("CommandSource")).text == "Item.ПервоеЗначение"


def test_кодирование_даёт_utf8_bom_crlf_и_валидный_logform_xml():
    result = compile_managed_form(_payload())
    xml = result.artifacts[0]
    module = result.artifacts[1]

    xml_bytes = xml.content.encode(xml.encoding)
    module_bytes = module.content.encode(module.encoding)
    assert xml_bytes.startswith(b"\xef\xbb\xbf")
    assert module_bytes.startswith(b"\xef\xbb\xbf")
    assert b"\r\n" in xml_bytes and b"\n" not in xml_bytes.replace(b"\r\n", b"")
    assert b"\r\n" in module_bytes and b"\n" not in module_bytes.replace(
        b"\r\n", b""
    )
    root = ET.fromstring(xml_bytes)
    assert root.tag == f"{{{LOGFORM}}}Form"
    assert root.attrib["version"] == "2.16"


def test_compiler_пишет_объектный_главный_реквизит_без_имени_конфигурации():
    payload = _payload()
    payload["attributes"][0] = {
        "name": "Объект",
        "type": {
            "kind": "metadata_object",
            "object": "Обработка.НоваяОбработка",
        },
        "main": True,
    }
    payload["elements"][0]["children"][0].update(
        {"name": "Комментарий", "data_path": "Объект.Комментарий"}
    )

    result = compile_managed_form(payload)
    xml = result.artifacts[0].content

    assert "<v8:Type>cfg:DataProcessorObject.НоваяОбработка</v8:Type>" in xml
    assert "<MainAttribute>true</MainAttribute>" in xml
    assert "Конфигурация" not in xml


def test_compiler_пишет_ссылочный_и_составной_типы_в_порядке_спецификации():
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

    xml = compile_managed_form(payload).artifacts[0].content

    assert "<v8:Type>cfg:CatalogRef.Товары</v8:Type>" in xml
    document = xml.index("<v8:Type>cfg:DocumentRef.Заказ</v8:Type>")
    string = xml.index("<v8:Type>xs:string</v8:Type>", document)
    qualifiers = xml.index("<v8:StringQualifiers>", string)
    assert document < string < qualifiers
    assert "<v8:Length>50</v8:Length>" in xml[qualifiers:]


def test_compiler_пишет_автоматический_dynamic_list_с_основной_таблицей():
    payload = _payload()
    payload["attributes"][0]["type"] = {
        "kind": "dynamic_list",
        "main_table": "Справочник.Товары",
        "dynamic_data_read": False,
    }

    xml = compile_managed_form(payload).artifacts[0].content

    assert "<v8:Type>cfg:DynamicList</v8:Type>" in xml
    assert '<Settings xsi:type="DynamicList">' in xml
    assert "<ManualQuery>false</ManualQuery>" in xml
    assert "<DynamicDataRead>false</DynamicDataRead>" in xml
    assert "<MainTable>Catalog.Товары</MainTable>" in xml
    assert "<dcsset:itemsViewMode>Normal</dcsset:itemsViewMode>" in xml


def test_id_детерминированы_и_пространства_элементов_реквизитов_команд_разделены():
    root = ET.fromstring(compile_managed_form(_payload()).artifacts[0].content)
    child_items = root.find(f"{{{LOGFORM}}}ChildItems")
    attributes = root.find(f"{{{LOGFORM}}}Attributes")
    commands = root.find(f"{{{LOGFORM}}}Commands")

    element_ids = [
        node.attrib["id"] for node in child_items.iter() if "id" in node.attrib
    ]
    attribute_ids = [node.attrib["id"] for node in attributes if "id" in node.attrib]
    command_ids = [node.attrib["id"] for node in commands if "id" in node.attrib]

    assert element_ids == [str(value) for value in range(1, 11)]
    assert attribute_ids == ["1", "2"]
    assert command_ids == ["1"]
    assert len(element_ids) == len(set(element_ids))


def test_xml_экранирует_пользовательский_текст_без_изменения_смысла():
    payload = _payload()
    payload["title"]["ru"] = 'A & <B> "C"'

    xml = compile_managed_form(payload).artifacts[0].content
    root = ET.fromstring(xml)
    content = root.find(
        f"{{{LOGFORM}}}Title/{{http://v8.1c.ru/8.1/data/core}}item"
    )

    assert content is not None
    assert list(content)[1].text == 'A & <B> "C"'
    assert "A &amp; &lt;B&gt; \"C\"" in xml


def test_result_не_объявляет_статический_green_нативной_приёмкой():
    payload = compile_managed_form(_payload()).to_dict()

    assert payload["status"] == "compiled"
    assert "valid" not in payload
    assert payload["coverage"] == {
        "xml_parse": "passed",
        "structural": "passed",
        "configuration_links": "not_checked",
        "bsl_static": "passed",
        "platform_import": "not_checked",
        "runtime_visual": "not_checked",
    }
    not_checked = {
        item["level"]
        for item in payload["diagnostics"]
        if item["status"] == "not_checked"
    }
    assert not_checked == {
        "configuration_links",
        "platform_import",
        "runtime_visual",
    }


def test_формат_2_20_сохраняется_в_Form_xml():
    payload = _payload()
    payload["format_version"] = "2.20"

    result = compile_managed_form(payload)

    assert result.status == "compiled"
    assert ET.fromstring(result.artifacts[0].content).attrib["version"] == "2.20"


def test_неподтверждённая_версия_формата_сохраняется_с_предупреждением():
    payload = _payload()
    payload["format_version"] = "2.21"

    result = compile_managed_form(payload)

    assert result.status == "compiled"
    assert ET.fromstring(result.artifacts[0].content).attrib["version"] == "2.21"
    assert any(
        item.code == "form_format_compatibility_unverified"
        and item.status == "warning"
        for item in result.diagnostics
    )


def test_некорректная_версия_формата_отклоняется_до_создания_результата():
    payload = _payload()
    payload["format_version"] = "latest"

    with pytest.raises(FormsContractError, match="число.число"):
        compile_managed_form(payload)


def test_один_action_двух_команд_создаёт_одну_процедуру():
    payload = _payload()
    payload["commands"].append(
        {
            "name": "ПроверитьПовторно",
            "title": {"ru": "Проверить повторно"},
            "action": "Проверить",
        }
    )

    module = compile_managed_form(payload).artifacts[1].content

    assert module.count("Процедура Проверить(Команда)") == 1


def test_стандартные_команды_формы_и_таблицы_компилируются_без_bsl_каркасов():
    payload = _rich_payload()
    payload["elements"][0]["children"].extend(
        [
            {
                "kind": "button",
                "name": "ЗакрытьФорму",
                "command": "Close",
                "command_kind": "form_standard",
            },
            {
                "kind": "button",
                "name": "ДобавитьСтроку",
                "command": "Add",
                "command_kind": "item_standard",
                "command_owner": "ТаблицаДанныхПоле",
            },
        ]
    )

    result = compile_managed_form(payload)

    assert (
        "<CommandName>Form.StandardCommand.Close</CommandName>"
        in result.artifacts[0].content
    )
    assert (
        "<CommandName>Form.Item.ТаблицаДанныхПоле.StandardCommand.Add</CommandName>"
        in result.artifacts[0].content
    )
    assert "ЗакрытьФорму(" not in result.artifacts[1].content
    assert "ДобавитьСтроку(" not in result.artifacts[1].content


def test_документные_команды_компилируются_как_стандартные_без_bsl():
    payload = _payload()
    payload["attributes"][0] = {
        "name": "Объект",
        "type": {
            "kind": "metadata_object",
            "object": "Документ.ТестовыйДокумент",
        },
        "main": True,
    }
    payload["elements"][0]["children"][0]["data_path"] = "Объект.Наименование"
    payload["elements"][0]["children"].extend(
        {
            "kind": "button",
            "name": f"Команда{index}",
            "command": command,
            "command_kind": "form_standard",
        }
        for index, command in enumerate(
            ("Post", "PostAndClose", "UndoPosting"), 1
        )
    )

    result = compile_managed_form(payload)
    xml = result.artifacts[0].content
    module = result.artifacts[1].content

    for command in ("Post", "PostAndClose", "UndoPosting"):
        assert f"Form.StandardCommand.{command}" in xml
    assert "Команда1(" not in module
    assert "Команда2(" not in module
    assert "Команда3(" not in module


def test_сгенерированные_bsl_каркасы_читаются_текущим_лексером():
    module = compile_managed_form(_payload()).artifacts[1].content

    procedures = разобрать(module)

    assert [item.имя for item in procedures] == [
        "ПриСозданииНаСервере",
        "Проверить",
    ]
    assert [item.директива for item in procedures] == ["НаСервере", "НаКлиенте"]
    assert [item.параметры for item in procedures] == [
        "Отказ, СтандартнаяОбработка",
        "Команда",
    ]


def test_один_handler_с_разными_сигнатурами_отклоняется():
    payload = _payload()
    payload["commands"][0]["action"] = "ПриСозданииНаСервере"

    with pytest.raises(FormsContractError) as caught:
        compile_managed_form(payload)

    assert any(
        item.code == "handler_signature_conflict" for item in caught.value.diagnostics
    )


def test_повторяющееся_событие_формы_отклоняется():
    payload = _payload()
    payload["events"].append(copy.deepcopy(payload["events"][0]))

    with pytest.raises(FormsContractError) as caught:
        compile_managed_form(payload)

    assert any(item.code == "duplicate_event" for item in caught.value.diagnostics)


def test_owner_aware_события_попадают_к_своим_элементам_и_получают_точные_каркасы():
    payload = _rich_payload()
    payload["events"] = [
        {"event": "OnOpen", "handler": "ПриОткрытии"},
        {
            "owner": "ПутьКФайлуПоле",
            "event": "OnChange",
            "handler": "ПутьКФайлуПриИзменении",
        },
        {
            "owner": "СтраницыРезультата",
            "event": "OnCurrentPageChange",
            "handler": "СтраницыРезультатаПриСменеСтраницы",
        },
        {
            "owner": "ТаблицаДанныхПоле",
            "event": "Selection",
            "handler": "ТаблицаДанныхПолеВыбор",
        },
    ]

    result = compile_managed_form(payload)
    root = ET.fromstring(result.artifacts[0].content)
    q = lambda name: f"{{{LOGFORM}}}{name}"
    by_name = {
        node.attrib.get("name"): node
        for node in root.iter()
        if "name" in node.attrib
    }
    assert root.find(f"{q('Events')}/{q('Event')}").attrib["name"] == "OnOpen"
    assert by_name["ПутьКФайлуПоле"].find(
        f"{q('Events')}/{q('Event')}"
    ).attrib["name"] == "OnChange"
    assert by_name["СтраницыРезультата"].find(
        f"{q('Events')}/{q('Event')}"
    ).attrib["name"] == "OnCurrentPageChange"
    assert by_name["ТаблицаДанныхПоле"].find(
        f"{q('Events')}/{q('Event')}"
    ).attrib["name"] == "Selection"

    module = result.artifacts[1].content
    assert "&НаКлиенте\r\nПроцедура ПриОткрытии(Отказ)" in module
    assert "Процедура ПутьКФайлуПриИзменении(Элемент)" in module
    assert (
        "Процедура СтраницыРезультатаПриСменеСтраницы(Элемент, ТекущаяСтраница)"
        in module
    )
    assert (
        "Процедура ТаблицаДанныхПолеВыбор(Элемент, ВыбраннаяСтрока, Поле, "
        "СтандартнаяОбработка)" in module
    )


def test_весь_стабильный_каталог_событий_генерирует_доказанные_сигнатуры():
    payload = _rich_payload()
    payload["events"] = [
        {"event": "OnCreateAtServer", "handler": "Создание"},
        {"event": "OnOpen", "handler": "Открытие"},
        {"event": "NotificationProcessing", "handler": "Оповещение"},
        {"event": "ExternalEvent", "handler": "Внешнее"},
        {"event": "FillCheckProcessingAtServer", "handler": "ПроверкаЗаполнения"},
        {"owner": "ПутьКФайлуПоле", "event": "OnChange", "handler": "ПутьИзменён"},
        {
            "owner": "ПерваяСтрокаЗаголовокПоле",
            "event": "OnChange",
            "handler": "ФлагИзменён",
        },
        {
            "owner": "СтраницыРезультата",
            "event": "OnCurrentPageChange",
            "handler": "СтраницаИзменена",
        },
        {"owner": "ТаблицаДанныхПоле", "event": "Selection", "handler": "Выбор"},
        {
            "owner": "ТаблицаДанныхПоле",
            "event": "OnActivateRow",
            "handler": "СтрокаАктивна",
        },
    ]

    module = compile_managed_form(payload).artifacts[1].content
    parsed = {item.имя: item for item in разобрать(module)}
    expected = {
        "Создание": ("НаСервере", "Отказ, СтандартнаяОбработка"),
        "Открытие": ("НаКлиенте", "Отказ"),
        "Оповещение": ("НаКлиенте", "ИмяСобытия, Параметр, Источник"),
        "Внешнее": ("НаКлиенте", "Источник, Событие, Данные"),
        "ПроверкаЗаполнения": ("НаСервере", "Отказ, ПроверяемыеРеквизиты"),
        "ПутьИзменён": ("НаКлиенте", "Элемент"),
        "ФлагИзменён": ("НаКлиенте", "Элемент"),
        "СтраницаИзменена": ("НаКлиенте", "Элемент, ТекущаяСтраница"),
        "Выбор": (
            "НаКлиенте",
            "Элемент, ВыбраннаяСтрока, Поле, СтандартнаяОбработка",
        ),
        "СтрокаАктивна": ("НаКлиенте", "Элемент"),
    }

    assert {
        name: (procedure.директива, procedure.параметры)
        for name, procedure in parsed.items()
        if name in expected
    } == expected


def test_расширенный_каталог_событий_формы_полей_и_таблицы_даёт_точные_каркасы():
    payload = _rich_payload()
    payload["events"] = [
        {"event": "BeforeClose", "handler": "ПередЗакрытием"},
        {"event": "OnClose", "handler": "ПриЗакрытии"},
        {"event": "ChoiceProcessing", "handler": "ВыборФормы"},
        {
            "owner": "ПутьКФайлуПоле",
            "event": "StartChoice",
            "handler": "НачалоВыбора",
        },
        {"owner": "ПутьКФайлуПоле", "event": "Clearing", "handler": "ОчисткаПоля"},
        {
            "owner": "ПутьКФайлуПоле",
            "event": "ChoiceProcessing",
            "handler": "ВыборПоля",
        },
        {
            "owner": "ПутьКФайлуПоле",
            "event": "AutoComplete",
            "handler": "АвтоПодбор",
        },
        {
            "owner": "ПутьКФайлуПоле",
            "event": "TextEditEnd",
            "handler": "КонецВвода",
        },
        {"owner": "ПутьКФайлуПоле", "event": "Opening", "handler": "ОткрытиеПоля"},
        {
            "owner": "ТаблицаДанныхПоле",
            "event": "ChoiceProcessing",
            "handler": "ВыборТаблицы",
        },
        {
            "owner": "ТаблицаДанныхПоле",
            "event": "OnStartEdit",
            "handler": "НачалоПравки",
        },
        {
            "owner": "ТаблицаДанныхПоле",
            "event": "BeforeAddRow",
            "handler": "ПередДобавлением",
        },
        {
            "owner": "ТаблицаДанныхПоле",
            "event": "BeforeRowChange",
            "handler": "ПередИзменением",
        },
        {
            "owner": "ТаблицаДанныхПоле",
            "event": "BeforeDeleteRow",
            "handler": "ПередУдалением",
        },
        {
            "owner": "ТаблицаДанныхПоле",
            "event": "AfterDeleteRow",
            "handler": "ПослеУдаления",
        },
        {
            "owner": "ТаблицаДанныхПоле",
            "event": "OnChange",
            "handler": "ИзменениеТаблицы",
        },
        {
            "owner": "ТаблицаДанныхПоле",
            "event": "OnEditEnd",
            "handler": "КонецПравки",
        },
    ]

    module = compile_managed_form(payload).artifacts[1].content
    parsed = {item.имя: item for item in разобрать(module)}
    expected = {
        "ПередЗакрытием": (
            "Отказ, ЗавершениеРаботы, ТекстПредупреждения, "
            "СтандартнаяОбработка"
        ),
        "ПриЗакрытии": "ЗавершениеРаботы",
        "ВыборФормы": "ВыбранноеЗначение, ИсточникВыбора",
        "НачалоВыбора": (
            "Элемент, ДанныеВыбора, ВыборДобавлением, СтандартнаяОбработка"
        ),
        "ОчисткаПоля": "Элемент, СтандартнаяОбработка",
        "ВыборПоля": (
            "Элемент, ВыбранноеЗначение, ДополнительныеДанные, "
            "ВыборДобавлением, СтандартнаяОбработка"
        ),
        "АвтоПодбор": (
            "Элемент, Текст, ДанныеВыбора, ПараметрыПолученияДанных, "
            "Ожидание, СтандартнаяОбработка"
        ),
        "КонецВвода": (
            "Элемент, Текст, ДанныеВыбора, ПараметрыПолученияДанных, "
            "СтандартнаяОбработка"
        ),
        "ОткрытиеПоля": "Элемент, СтандартнаяОбработка",
        "ВыборТаблицы": "Элемент, ВыбранноеЗначение, СтандартнаяОбработка",
        "НачалоПравки": "Элемент, НоваяСтрока, Копирование",
        "ПередДобавлением": (
            "Элемент, Отказ, Копирование, Родитель, ЭтоГруппа, Параметр"
        ),
        "ПередИзменением": "Элемент, Отказ",
        "ПередУдалением": "Элемент, Отказ",
        "ПослеУдаления": "Элемент",
        "ИзменениеТаблицы": "Элемент",
        "КонецПравки": "Элемент, НоваяСтрока, ОтменаРедактирования",
    }

    assert {
        name: procedure.параметры
        for name, procedure in parsed.items()
        if name in expected
    } == expected
    assert all(parsed[name].директива == "НаКлиенте" for name in expected)


def test_событие_неподходящего_owner_отклоняется_до_compiler():
    payload = _rich_payload()
    payload["events"] = [
        {
            "owner": "СтраницыРезультата",
            "event": "OnChange",
            "handler": "НеверныйОбработчик",
        }
    ]

    with pytest.raises(FormsContractError) as caught:
        compile_managed_form(payload)

    assert any(item.code == "unsupported_owner_event" for item in caught.value.diagnostics)


def test_объектные_события_дают_доказанные_директивы_и_сигнатуры():
    payload = _payload()
    payload["attributes"][0] = {
        "name": "Объект",
        "type": {"kind": "metadata_object", "object": "Справочник.Товары"},
        "main": True,
    }
    payload["elements"][0]["children"][0]["data_path"] = "Объект.Наименование"
    payload["events"] = [
        {"event": "OnReadAtServer", "handler": "ПриЧтенииНаСервере"},
        {"event": "BeforeWrite", "handler": "ПередЗаписью"},
        {"event": "BeforeWriteAtServer", "handler": "ПередЗаписьюНаСервере"},
        {"event": "OnWriteAtServer", "handler": "ПриЗаписиНаСервере"},
        {"event": "AfterWriteAtServer", "handler": "ПослеЗаписиНаСервере"},
        {"event": "AfterWrite", "handler": "ПослеЗаписи"},
    ]

    result = compile_managed_form(payload)
    module = result.artifacts[1].content

    assert "Процедура ПриЧтенииНаСервере(ТекущийОбъект)" in module
    assert "Процедура ПередЗаписью(Отказ, ПараметрыЗаписи)" in module
    assert (
        "Процедура ПередЗаписьюНаСервере(Отказ, ТекущийОбъект, ПараметрыЗаписи)"
        in module
    )
    assert (
        "Процедура ПриЗаписиНаСервере(Отказ, ТекущийОбъект, ПараметрыЗаписи)"
        in module
    )
    assert (
        "Процедура ПослеЗаписиНаСервере(ТекущийОбъект, ПараметрыЗаписи)"
        in module
    )
    assert "Процедура ПослеЗаписи(ПараметрыЗаписи)" in module


def test_повторяется_пара_owner_и_event_а_не_одно_имя_event():
    payload = _payload()
    payload["events"] = [
        {"owner": "ПервоеЗначение", "event": "OnChange", "handler": "Первое"},
        {"owner": "ВтороеЗначение", "event": "OnChange", "handler": "Второе"},
    ]

    result = compile_managed_form(payload)

    assert result.status == "compiled"


def test_один_handler_одинаковой_сигнатуры_двух_элементов_создаётся_один_раз():
    payload = _payload()
    payload["events"] = [
        {"owner": "ПервоеЗначение", "event": "OnChange", "handler": "Изменение"},
        {"owner": "ВтороеЗначение", "event": "OnChange", "handler": "Изменение"},
    ]

    module = compile_managed_form(payload).artifacts[1].content

    assert module.count("Процедура Изменение(Элемент)") == 1


def test_один_handler_разных_событий_с_разными_сигнатурами_отклоняется():
    payload = _payload()
    payload["events"] = [
        {"event": "OnOpen", "handler": "Общий"},
        {"owner": "ПервоеЗначение", "event": "OnChange", "handler": "Общий"},
    ]

    with pytest.raises(FormsContractError) as caught:
        compile_managed_form(payload)

    assert any(item.code == "handler_signature_conflict" for item in caught.value.diagnostics)


def test_compiler_не_пишет_файлы_и_возвращает_только_два_текстовых_artifact(
    tmp_path,
    monkeypatch,
):
    payload = copy.deepcopy(_payload())
    before = list(tmp_path.iterdir())

    def forbidden_write(*_args, **_kwargs):
        raise AssertionError("compiler не должен писать файлы")

    monkeypatch.setattr(Path, "write_text", forbidden_write)
    monkeypatch.setattr(Path, "write_bytes", forbidden_write)
    monkeypatch.setattr("builtins.open", forbidden_write)
    result = compile_managed_form(payload)

    assert list(tmp_path.iterdir()) == before
    assert [(item.media_type, item.encoding) for item in result.artifacts] == [
        ("application/xml", "utf-8-sig"),
        ("text/plain", "utf-8-sig"),
    ]
    assert all(isinstance(item.content, str) for item in result.artifacts)
