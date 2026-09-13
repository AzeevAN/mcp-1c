from __future__ import annotations

import copy
import json
from pathlib import Path
from xml.etree import ElementTree as ET

import pytest

from mcp1c.capability_modules.forms.compiler import compile_managed_form
from mcp1c.capability_modules.forms.decompiler import decompile_managed_form


FIXTURES = Path(__file__).with_name("fixtures")


def _payload() -> dict:
    return json.loads((FIXTURES / "minimal_form.json").read_text(encoding="utf-8"))


def _rich_payload() -> dict:
    return json.loads(
        (FIXTURES / "file_import_form.json").read_text(encoding="utf-8")
    )


def _xml() -> str:
    return (FIXTURES / "minimal_form.xml").read_text(encoding="utf-8")


def _diagnostics(result, code: str) -> list:
    return [item for item in result.diagnostics if item.code == code]


def test_fixture_декомпилируется_в_каноническую_спецификацию():
    result = decompile_managed_form(_xml(), form_name="ФормаПараметров")

    assert result.status == "decompiled"
    assert result.specification == compile_managed_form(_payload()).specification
    assert result.artifacts == ()
    assert result.coverage.xml_parse == "passed"
    assert result.coverage.structural == "passed"

def test_compile_decompile_даёт_нормализованный_roundtrip():
    payload = _payload()
    payload["title"]["ru"] = 'A & <B> "C"'
    compiled = compile_managed_form(payload)

    result = decompile_managed_form(
        compiled.artifacts[0].content,
        form_name=payload["form_name"],
        module_bsl=compiled.artifacts[1].content,
    )

    assert result.specification == compiled.specification
    assert result.coverage.structural == "passed"

    assert result.coverage.bsl_static == "not_checked"
    assert _diagnostics(result, "bsl_check_deferred")


def test_богатая_форма_импорта_даёт_lossless_roundtrip():
    compiled = compile_managed_form(_rich_payload())

    result = decompile_managed_form(
        compiled.artifacts[0].content,
        form_name="ФормаИмпортаФайла",
        module_bsl=compiled.artifacts[1].content,
    )

    assert result.specification == compiled.specification
    assert result.coverage.structural == "passed"


def test_owner_aware_события_элементов_дают_lossless_roundtrip():
    payload = _rich_payload()
    payload["events"] = [
        {"event": "OnOpen", "handler": "ПриОткрытии"},
        {
            "owner": "ПутьКФайлуПоле",
            "event": "OnChange",
            "handler": "ПутьКФайлуПриИзменении",
        },
        {
            "owner": "ТаблицаДанныхПоле",
            "event": "OnActivateRow",
            "handler": "ТаблицаДанныхПолеПриАктивизацииСтроки",
        },
    ]
    compiled = compile_managed_form(payload)

    result = decompile_managed_form(
        compiled.artifacts[0].content,
        form_name=payload["form_name"],
        module_bsl=compiled.artifacts[1].content,
    )

    assert result.specification == compiled.specification
    assert result.coverage.structural == "passed"


def test_расширенный_каталог_событий_даёт_lossless_roundtrip():
    payload = _rich_payload()
    payload["events"] = [
        {"event": "BeforeClose", "handler": "ПередЗакрытием"},
        {
            "owner": "ПутьКФайлуПоле",
            "event": "StartChoice",
            "handler": "НачалоВыбора",
        },
        {
            "owner": "ТаблицаДанныхПоле",
            "event": "BeforeAddRow",
            "handler": "ПередДобавлением",
        },
    ]
    compiled = compile_managed_form(payload)

    result = decompile_managed_form(
        compiled.artifacts[0].content,
        form_name=payload["form_name"],
        module_bsl=compiled.artifacts[1].content,
    )

    assert result.specification == compiled.specification
    assert result.coverage.structural == "passed"


def test_явная_версия_платформы_сохраняется_в_lossless_roundtrip():
    payload = _rich_payload()
    payload["platform_version"] = "8.3.24"
    payload["events"] = [{"event": "BeforeClose", "handler": "ПередЗакрытием"}]
    compiled = compile_managed_form(payload)

    result = decompile_managed_form(
        compiled.artifacts[0].content,
        form_name=payload["form_name"],
        module_bsl=compiled.artifacts[1].content,
        platform_version="8.3.24",
    )

    assert result.specification == compiled.specification
    assert result.specification["platform_version"] == "8.3.24"
    assert result.coverage.structural == "passed"


def test_объектный_главный_реквизит_даёт_lossless_roundtrip():
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
    compiled = compile_managed_form(payload)

    result = decompile_managed_form(
        compiled.artifacts[0].content,
        form_name=payload["form_name"],
        module_bsl=compiled.artifacts[1].content,
    )

    assert result.status == "decompiled"
    assert result.specification == compiled.specification


def test_объектные_события_и_команда_записи_дают_lossless_roundtrip():
    payload = _payload()
    payload["attributes"][0] = {
        "name": "Объект",
        "type": {"kind": "metadata_object", "object": "Справочник.Товары"},
        "main": True,
    }
    payload["elements"][0]["children"][0]["data_path"] = "Объект.Наименование"
    payload["elements"][0]["children"][2] = {
        "kind": "button",
        "name": "Записать",
        "command": "Write",
        "command_kind": "form_standard",
    }
    payload["events"] = [
        {"event": "OnReadAtServer", "handler": "ПриЧтенииНаСервере"},
        {"event": "BeforeWrite", "handler": "ПередЗаписью"},
        {"event": "AfterWrite", "handler": "ПослеЗаписи"},
    ]
    compiled = compile_managed_form(payload)

    result = decompile_managed_form(
        compiled.artifacts[0].content,
        form_name=payload["form_name"],
        module_bsl=compiled.artifacts[1].content,
    )

    assert result.status == "decompiled"
    assert result.specification == compiled.specification


def test_документные_команды_дают_lossless_roundtrip():
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
    compiled = compile_managed_form(payload)

    result = decompile_managed_form(
        compiled.artifacts[0].content,
        form_name=payload["form_name"],
        module_bsl=compiled.artifacts[1].content,
    )

    assert result.status == "decompiled"
    assert result.specification == compiled.specification


def test_ссылочный_и_составной_типы_дают_lossless_roundtrip():
    payload = _payload()
    payload["attributes"][0]["type"] = {
        "kind": "metadata_reference",
        "object": "Справочник.Товары",
    }
    payload["attributes"][1]["type"] = {
        "kind": "composite",
        "variants": [
            {"kind": "metadata_reference", "object": "Перечисление.Состояния"},
            {"kind": "string", "length": 30},
        ],
    }
    compiled = compile_managed_form(payload)

    result = decompile_managed_form(
        compiled.artifacts[0].content,
        form_name=payload["form_name"],
        module_bsl=compiled.artifacts[1].content,
    )

    assert result.status == "decompiled"
    assert result.specification == compiled.specification


def test_составной_тип_колонки_таблицы_даёт_lossless_roundtrip():
    payload = _rich_payload()
    payload["attributes"][13]["type"]["columns"][0]["type"] = {
        "kind": "composite",
        "variants": [
            {"kind": "metadata_reference", "object": "Справочник.Товары"},
            {"kind": "string", "length": 100},
        ],
    }
    compiled = compile_managed_form(payload)

    result = decompile_managed_form(
        compiled.artifacts[0].content,
        form_name=payload["form_name"],
        module_bsl=compiled.artifacts[1].content,
    )

    assert result.status == "decompiled"
    assert result.specification == compiled.specification


def test_автоматический_dynamic_list_даёт_lossless_roundtrip():
    payload = _payload()
    payload["attributes"][0]["type"] = {
        "kind": "dynamic_list",
        "main_table": "РегистрСведений.Цены",
        "dynamic_data_read": True,
    }
    compiled = compile_managed_form(payload)

    result = decompile_managed_form(
        compiled.artifacts[0].content,
        form_name=payload["form_name"],
        module_bsl=compiled.artifacts[1].content,
    )

    assert result.status == "decompiled"
    assert result.specification == compiled.specification


def test_label_decoration_с_событиями_даёт_lossless_roundtrip():
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
    compiled = compile_managed_form(payload)

    result = decompile_managed_form(
        compiled.artifacts[0].content,
        form_name=payload["form_name"],
        module_bsl=compiled.artifacts[1].content,
    )

    assert result.specification == compiled.specification
    assert result.coverage.structural == "passed"


def test_label_decoration_без_или_с_повтором_context_menu_остаётся_inventory():
    payload = _payload()
    payload["elements"].append(
        {
            "kind": "label_decoration",
            "name": "Пояснение",
            "title": {"ru": "Проверьте параметры"},
        }
    )
    original = compile_managed_form(payload).artifacts[0].content

    for mutation in ("missing", "repeated"):
        root = ET.fromstring(original)
        label = next(
            node
            for node in root.iter(
                "{http://v8.1c.ru/8.3/xcf/logform}LabelDecoration"
            )
            if node.attrib.get("name") == "Пояснение"
        )
        context_menu = label.find(
            "{http://v8.1c.ru/8.3/xcf/logform}ContextMenu"
        )
        assert context_menu is not None
        if mutation == "missing":
            label.remove(context_menu)
        else:
            label.append(copy.deepcopy(context_menu))

        result = decompile_managed_form(
            ET.tostring(root, encoding="unicode"),
            form_name=payload["form_name"],
        )

        assert result.coverage.structural == "unsupported"
        assert _diagnostics(result, "missing_or_repeated_xml_node")


def test_label_field_с_событиями_даёт_lossless_roundtrip():
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
    compiled = compile_managed_form(payload)

    result = decompile_managed_form(
        compiled.artifacts[0].content,
        form_name=payload["form_name"],
        module_bsl=compiled.artifacts[1].content,
    )

    assert result.specification == compiled.specification
    assert result.coverage.structural == "passed"

    table_payload = _rich_payload()
    column = table_payload["elements"][0]["children"][1]["pages"][0][
        "children"
    ][0]["columns"][0]
    column["kind"] = "label_field"
    column["hyperlink"] = True
    table_compiled = compile_managed_form(table_payload)

    table_result = decompile_managed_form(
        table_compiled.artifacts[0].content,
        form_name=table_payload["form_name"],
        module_bsl=table_compiled.artifacts[1].content,
    )

    assert table_result.specification == table_compiled.specification
    assert table_result.coverage.structural == "passed"


def test_label_field_без_data_path_остаётся_inventory():
    payload = _payload()
    payload["elements"].append(
        {
            "kind": "label_field",
            "name": "Итог",
            "data_path": "ПервоеЗначение",
        }
    )
    root = ET.fromstring(compile_managed_form(payload).artifacts[0].content)
    field = next(
        node
        for node in root.iter("{http://v8.1c.ru/8.3/xcf/logform}LabelField")
        if node.attrib.get("name") == "Итог"
    )
    data_path = field.find("{http://v8.1c.ru/8.3/xcf/logform}DataPath")
    assert data_path is not None
    field.remove(data_path)

    result = decompile_managed_form(
        ET.tostring(root, encoding="unicode"),
        form_name=payload["form_name"],
    )

    assert result.coverage.structural == "unsupported"
    assert _diagnostics(result, "missing_or_repeated_xml_node")


def test_radio_button_field_даёт_lossless_roundtrip():
    payload = _payload()
    payload["elements"].append(
        {
            "kind": "radio_button_field",
            "name": "Режим",
            "data_path": "ПервоеЗначение",
            "radio_button_type": "radio_buttons",
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
    compiled = compile_managed_form(payload)

    result = decompile_managed_form(
        compiled.artifacts[0].content,
        form_name=payload["form_name"],
        module_bsl=compiled.artifacts[1].content,
    )

    assert result.specification == compiled.specification
    assert result.coverage.structural == "passed"

    root = ET.fromstring(compiled.artifacts[0].content)
    field = next(
        node
        for node in root.iter(
            "{http://v8.1c.ru/8.3/xcf/logform}RadioButtonField"
        )
    )
    choice_list = field.find("{http://v8.1c.ru/8.3/xcf/logform}ChoiceList")
    assert choice_list is not None
    field.remove(choice_list)
    unsupported = decompile_managed_form(
        ET.tostring(root, encoding="unicode"),
        form_name=payload["form_name"],
    )

    assert unsupported.coverage.structural == "unsupported"
    assert _diagnostics(unsupported, "missing_or_repeated_xml_node")


def test_command_bar_с_кнопками_даёт_lossless_roundtrip():
    payload = _payload()
    payload["elements"].append(
        {
            "kind": "command_bar",
            "name": "Действия",
            "title": {"ru": "Действия"},
            "horizontal_location": "center",
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
    compiled = compile_managed_form(payload)

    result = decompile_managed_form(
        compiled.artifacts[0].content,
        form_name=payload["form_name"],
        module_bsl=compiled.artifacts[1].content,
    )

    assert result.specification == compiled.specification
    assert result.coverage.structural == "passed"

    for missing_tag in ("ExtendedTooltip", "ChildItems"):
        root = ET.fromstring(compiled.artifacts[0].content)
        bar = next(
            node
            for node in root.iter(
                "{http://v8.1c.ru/8.3/xcf/logform}CommandBar"
            )
        )
        node = bar.find(
            f"{{http://v8.1c.ru/8.3/xcf/logform}}{missing_tag}"
        )
        assert node is not None
        bar.remove(node)
        unsupported = decompile_managed_form(
            ET.tostring(root, encoding="unicode"),
            form_name=payload["form_name"],
        )

        assert unsupported.coverage.structural == "unsupported"
        assert _diagnostics(unsupported, "missing_or_repeated_xml_node")


def test_popup_с_кнопками_даёт_lossless_roundtrip():
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
    compiled = compile_managed_form(payload)

    result = decompile_managed_form(
        compiled.artifacts[0].content,
        form_name=payload["form_name"],
        module_bsl=compiled.artifacts[1].content,
    )

    assert result.specification == compiled.specification
    assert result.coverage.structural == "passed"

    for missing_tag in ("Title", "ExtendedTooltip", "ChildItems"):
        root = ET.fromstring(compiled.artifacts[0].content)
        popup = next(
            node
            for node in root.iter(
                "{http://v8.1c.ru/8.3/xcf/logform}Popup"
            )
        )
        child = popup.find(
            f"{{http://v8.1c.ru/8.3/xcf/logform}}{missing_tag}"
        )
        assert child is not None
        popup.remove(child)

        unsupported = decompile_managed_form(
            ET.tostring(root, encoding="unicode"),
            form_name=payload["form_name"],
        )

        assert unsupported.coverage.structural == "unsupported"
        assert _diagnostics(unsupported, "missing_or_repeated_xml_node")


def test_ручной_dynamic_list_остаётся_inventory():
    payload = _payload()
    payload["attributes"][0]["type"] = {
        "kind": "dynamic_list",
        "main_table": "Справочник.Товары",
        "dynamic_data_read": True,
    }
    compiled = compile_managed_form(payload)
    xml = compiled.artifacts[0].content.replace(
        "<ManualQuery>false</ManualQuery>",
        "<ManualQuery>true</ManualQuery><QueryText>ВЫБРАТЬ 1</QueryText>",
    )

    result = decompile_managed_form(xml, form_name=payload["form_name"])

    assert result.status == "decompiled"
    assert result.coverage.structural == "unsupported"
    assert _diagnostics(result, "manual_dynamic_list_not_supported")


def test_decompiler_разрешает_неизвестную_версию_с_предупреждением():
    result = decompile_managed_form(
        _xml(),
        form_name="ФормаПараметров",
        platform_version="8.3.16",
    )

    assert result.status == "decompiled"
    assert result.specification["platform_version"] == "8.3.16"
    assert _diagnostics(result, "platform_compatibility_unverified")


def test_стандартные_команды_формы_и_таблицы_дают_lossless_roundtrip():
    payload = _rich_payload()
    payload["elements"][0]["children"].extend(
        [
            {
                "kind": "button",
                "name": "СправкаФормы",
                "command": "Help",
                "command_kind": "form_standard",
            },
            {
                "kind": "button",
                "name": "УдалитьСтроку",
                "command": "Delete",
                "command_kind": "item_standard",
                "command_owner": "ТаблицаДанныхПоле",
            },
        ]
    )
    compiled = compile_managed_form(payload)

    result = decompile_managed_form(
        compiled.artifacts[0].content,
        form_name=payload["form_name"],
        module_bsl=compiled.artifacts[1].content,
    )

    assert result.specification == compiled.specification
    assert result.coverage.structural == "passed"


def test_форма_только_со_стандартной_командой_даёт_lossless_roundtrip():
    payload = _payload()
    payload["commands"] = []
    payload["elements"][0]["children"][2] = {
        "kind": "button",
        "name": "ЗакрытьФорму",
        "command": "Close",
        "command_kind": "form_standard",
    }
    compiled = compile_managed_form(payload)

    result = decompile_managed_form(
        compiled.artifacts[0].content,
        form_name=payload["form_name"],
        module_bsl=compiled.artifacts[1].content,
    )

    assert result.specification == compiled.specification
    assert result.coverage.structural == "passed"
    assert not _diagnostics(result, "outside_compiler_subset")


def test_неподдержанная_стандартная_команда_остаётся_inventory():
    xml = _xml().replace(
        "Form.Command.Проверить", "Form.StandardCommand.Create"
    )

    result = decompile_managed_form(xml, form_name="ФормаПараметров")

    assert result.status == "decompiled"
    assert result.coverage.structural == "unsupported"
    assert result.specification["elements"][0]["children"][2]["command"] == "Create"
    assert _diagnostics(result, "unsupported_standard_command")


def test_неизвестное_событие_элемента_остаётся_inventory_а_не_угадывается():
    payload = _rich_payload()
    payload["events"] = [
        {
            "owner": "ПутьКФайлуПоле",
            "event": "OnChange",
            "handler": "ПутьКФайлуПриИзменении",
        }
    ]
    xml = compile_managed_form(payload).artifacts[0].content.replace(
        'name="OnChange"', 'name="BeforeClose"', 1
    )

    result = decompile_managed_form(xml, form_name=payload["form_name"])

    assert result.coverage.structural == "unsupported"
    assert result.specification["events"] == [
        {
            "owner": "ПутьКФайлуПоле",
            "event": "BeforeClose",
            "handler": "ПутьКФайлуПриИзменении",
        }
    ]
    assert _diagnostics(result, "unsupported_event")


def test_неизвестный_элемент_остаётся_в_inventory_с_точным_путём_и_tag():
    xml = _xml().replace(
        "\t</ChildItems>\n\t<Attributes>",
        '\t\t<CalendarField name="НеизвестныйКалендарь" id="11"><Mystery/></CalendarField>\n'
        "\t</ChildItems>\n\t<Attributes>",
        1,
    )

    result = decompile_managed_form(xml, form_name="ФормаПараметров")

    assert result.status == "decompiled"
    assert result.specification is not None
    assert result.coverage.structural == "unsupported"
    unsupported = _diagnostics(result, "unsupported_xml_node")
    assert [(item.path, item.message) for item in unsupported] == [
        (
                "/Form/ChildItems/CalendarField[1]",
                "Неподдержанный XML-узел: CalendarField.",
        ),
        (
                "/Form/ChildItems/CalendarField[1]/Mystery[1]",
            "Неподдержанный XML-узел: Mystery.",
        ),
    ]
    assert "allow_lossy" in result.instructions[0]


@pytest.mark.parametrize("version", ["2.19", "2.20"])
def test_новая_версия_читается_только_как_inventory(version):
    xml = _xml().replace('version="2.16"', f'version="{version}"', 1)

    result = decompile_managed_form(xml, form_name="ФормаПараметров")

    assert result.status == "decompiled"
    assert result.specification["format_version"] == version
    assert result.coverage.xml_parse == "passed"
    assert result.coverage.structural == "unsupported"
    assert _diagnostics(result, "unsupported_format_version")[0].path == (
        "/Form/@version"
    )


@pytest.mark.parametrize(
    ("declaration", "code"),
    [
        ('<!DOCTYPE Form SYSTEM "outside.dtd">', "doctype_forbidden"),
        ('<!ENTITY hidden "value">', "entity_forbidden"),
    ],
)
def test_dtd_и_entity_отклоняются_до_xml_parser(monkeypatch, declaration, code):
    xml = _xml().replace("?>", f"?>\n{declaration}", 1)

    def forbidden_parse(_value):
        raise AssertionError("опасная декларация не должна доходить до XML parser")

    monkeypatch.setattr(
        "mcp1c.capability_modules.forms.decompiler.ET.fromstring",
        forbidden_parse,
    )
    result = decompile_managed_form(xml, form_name="ФормаПараметров")

    assert result.status == "rejected"
    assert result.specification is None
    assert result.coverage.xml_parse == "failed"
    assert _diagnostics(result, code)


def test_вход_сверх_лимита_отклоняется_до_xml_parser(monkeypatch):
    xml = _xml()
    limit = len(xml.encode("utf-8")) - 1
    monkeypatch.setattr(
        "mcp1c.capability_modules.forms.decompiler.MAX_FORM_XML_BYTES",
        limit,
    )

    def forbidden_parse(_value):
        raise AssertionError("слишком большой XML не должен разбираться")

    monkeypatch.setattr(
        "mcp1c.capability_modules.forms.decompiler.ET.fromstring",
        forbidden_parse,
    )
    result = decompile_managed_form(xml, form_name="ФормаПараметров")

    assert result.status == "rejected"
    assert result.coverage.xml_parse == "failed"
    assert _diagnostics(result, "form_xml_too_large")[0].message.endswith(
        f"{limit} байт."
    )


def test_битый_xml_возвращает_структурированный_rejected():
    result = decompile_managed_form("<Form>", form_name="ФормаПараметров")

    assert result.status == "rejected"
    assert result.specification is None
    assert result.coverage.xml_parse == "failed"
    assert _diagnostics(result, "malformed_xml")
    assert _diagnostics(result, "structure_not_checked_after_xml_rejection")


def test_каждый_not_checked_уровень_rejected_имеет_причину():
    result = decompile_managed_form(
        _xml(),
        form_name="ФормаПараметров",
        module_bsl=42,
    )

    not_checked = {
        level
        for level, status in result.coverage.to_dict().items()
        if status == "not_checked"
    }
    explained = {
        item.level for item in result.diagnostics if item.status == "not_checked"
    }
    assert result.status == "rejected"
    assert not_checked <= explained


def test_противоречивая_спецификация_не_получает_инструкцию_roundtrip():
    xml = _xml().replace(
        'name="ВтороеЗначение" id="2"',
        'name="ПервоеЗначение" id="2"',
        1,
    )

    result = decompile_managed_form(xml, form_name="ФормаПараметров")

    assert result.status == "rejected"
    assert result.coverage.structural == "failed"
    assert _diagnostics(result, "duplicate_attribute_name")
    assert "запрещена" in result.instructions[0]
    assert "пригодна" not in result.instructions[0]


def test_чужой_namespace_не_выдаётся_за_управляемую_форму():
    xml = _xml().replace(
        'xmlns="http://v8.1c.ru/8.3/xcf/logform"',
        'xmlns="urn:foreign"',
        1,
    )

    result = decompile_managed_form(xml, form_name="ФормаПараметров")

    assert result.status == "rejected"
    assert result.specification is None
    assert result.coverage.xml_parse == "passed"
    assert result.coverage.structural == "failed"
    assert _diagnostics(result, "unexpected_root")


def test_decompiler_не_пишет_файлы(monkeypatch, tmp_path):
    xml = _xml()
    before = list(tmp_path.iterdir())

    def forbidden_write(*_args, **_kwargs):
        raise AssertionError("decompiler не должен писать файлы")

    monkeypatch.setattr(Path, "write_text", forbidden_write)
    monkeypatch.setattr(Path, "write_bytes", forbidden_write)
    monkeypatch.setattr("builtins.open", forbidden_write)
    result = decompile_managed_form(xml, form_name="ФормаПараметров")

    assert result.status == "decompiled"
    assert list(tmp_path.iterdir()) == before


def test_неподдержанный_xml_attribute_не_теряется_молча():
    xml = _xml().replace(
        '<InputField name="ПервоеЗначение" id="3">',
        '<InputField name="ПервоеЗначение" id="3" custom="value">',
        1,
    )

    result = decompile_managed_form(xml, form_name="ФормаПараметров")

    assert result.coverage.structural == "unsupported"
    assert _diagnostics(result, "unsupported_xml_attribute")[0].path == (
        "/Form/ChildItems/UsualGroup[1]/ChildItems/InputField[1]/@custom"
    )


def test_отсутствующие_секции_тип_и_action_дают_inventory_без_выдуманных_полей():
    root = ET.fromstring(_xml())
    namespace = {"f": "http://v8.1c.ru/8.3/xcf/logform"}
    events = root.find("f:Events", namespace)
    attribute = root.find("f:Attributes/f:Attribute", namespace)
    attribute_type = attribute.find("f:Type", namespace)
    command = root.find("f:Commands/f:Command", namespace)
    action = command.find("f:Action", namespace)
    root.remove(events)
    attribute.remove(attribute_type)
    command.remove(action)

    result = decompile_managed_form(
        ET.tostring(root, encoding="unicode"),
        form_name="ФормаПараметров",
    )

    assert result.status == "decompiled"
    assert result.coverage.structural == "unsupported"
    assert result.specification["events"] == []
    assert "type" not in result.specification["attributes"][0]
    assert "action" not in result.specification["commands"][0]
    assert _diagnostics(result, "attribute_type_not_representable")
    assert _diagnostics(result, "command_action_not_representable")
    assert _diagnostics(result, "outside_compiler_subset")


def test_исходный_payload_не_изменяется():
    xml = _xml()
    original = copy.deepcopy(xml)

    decompile_managed_form(xml, form_name="ФормаПараметров")

    assert xml == original
