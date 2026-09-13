from __future__ import annotations

import json
from pathlib import Path
from xml.etree import ElementTree as ET

from mcp1c.capability_modules.forms.checker import check_managed_form
from mcp1c.capability_modules.forms.compiler import compile_managed_form


FIXTURES = Path(__file__).with_name("fixtures")
LOGFORM = "http://v8.1c.ru/8.3/xcf/logform"
V8 = "http://v8.1c.ru/8.1/data/core"


def _payload() -> dict:
    return json.loads((FIXTURES / "minimal_form.json").read_text(encoding="utf-8"))


def _rich_payload() -> dict:
    return json.loads(
        (FIXTURES / "file_import_form.json").read_text(encoding="utf-8")
    )


def _pair() -> tuple[str, str]:
    result = compile_managed_form(_payload())
    return result.artifacts[0].content, result.artifacts[1].content


def _diagnostics(result, code: str) -> list:
    return [item for item in result.diagnostics if item.code == code]


def test_compiler_pair_проходит_заявленные_статические_уровни():
    xml, module = _pair()

    result = check_managed_form(
        xml,
        form_name="ФормаПараметров",
        module_bsl=module,
    )

    assert result.status == "checked"
    assert result.artifacts == ()
    assert result.specification == compile_managed_form(_payload()).specification
    assert result.coverage.to_dict() == {
        "xml_parse": "passed",
        "structural": "passed",
        "configuration_links": "not_checked",
        "bsl_static": "passed",
        "platform_import": "not_checked",
        "runtime_visual": "not_checked",
    }
    assert "valid" not in result.to_dict()


def test_богатая_форма_импорта_проходит_статические_уровни():
    compiled = compile_managed_form(_rich_payload())

    result = check_managed_form(
        compiled.artifacts[0].content,
        form_name="ФормаИмпортаФайла",
        module_bsl=compiled.artifacts[1].content,
    )

    assert result.coverage.structural == "passed"
    assert result.coverage.bsl_static == "passed"
    assert result.specification == compiled.specification


def test_checker_проверяет_label_decoration_и_его_клиентские_события():
    payload = _payload()
    payload["elements"].append(
        {
            "kind": "label_decoration",
            "name": "Пояснение",
            "title": {"ru": "Проверьте параметры"},
            "hyperlink": True,
        }
    )
    payload["events"] = [
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
    compiled = compile_managed_form(payload)

    result = check_managed_form(
        compiled.artifacts[0].content,
        form_name=payload["form_name"],
        module_bsl=compiled.artifacts[1].content,
    )

    assert result.coverage.structural == "passed"
    assert result.coverage.bsl_static == "passed"
    assert result.specification == compiled.specification


def test_checker_проверяет_label_field_и_его_события():
    payload = _payload()
    payload["elements"].append(
        {
            "kind": "label_field",
            "name": "Итог",
            "data_path": "ПервоеЗначение",
            "hyperlink": True,
        }
    )
    payload["events"] = [
        {"owner": "Итог", "event": "OnChange", "handler": "ИтогИзменён"},
        {"owner": "Итог", "event": "Click", "handler": "ИтогНажатие"},
        {
            "owner": "Итог",
            "event": "URLProcessing",
            "handler": "ИтогОбработкаСсылки",
        },
    ]
    compiled = compile_managed_form(payload)

    result = check_managed_form(
        compiled.artifacts[0].content,
        form_name=payload["form_name"],
        module_bsl=compiled.artifacts[1].content,
    )

    assert result.coverage.structural == "passed"
    assert result.coverage.bsl_static == "passed"
    assert result.specification == compiled.specification


def test_checker_проверяет_radio_button_field():
    payload = _payload()
    payload["elements"].append(
        {
            "kind": "radio_button_field",
            "name": "Режим",
            "data_path": "ПервоеЗначение",
            "choice_list": [
                {"value": "A", "presentation": {"ru": "Первый"}},
                {"value": "B", "presentation": {"ru": "Второй"}},
            ],
        }
    )
    payload["events"] = [
        {"owner": "Режим", "event": "OnChange", "handler": "РежимИзменён"}
    ]
    compiled = compile_managed_form(payload)

    result = check_managed_form(
        compiled.artifacts[0].content,
        form_name=payload["form_name"],
        module_bsl=compiled.artifacts[1].content,
    )

    assert result.coverage.structural == "passed"
    assert result.coverage.bsl_static == "passed"


def test_checker_проверяет_command_bar_и_её_кнопки():
    payload = _payload()
    payload["elements"].append(
        {
            "kind": "command_bar",
            "name": "Действия",
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

    result = check_managed_form(
        compiled.artifacts[0].content,
        form_name=payload["form_name"],
        module_bsl=compiled.artifacts[1].content,
    )

    assert result.coverage.structural == "passed"
    assert result.coverage.bsl_static == "passed"
    assert result.specification == compiled.specification


def test_checker_проверяет_popup_внутри_command_bar():
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

    result = check_managed_form(
        compiled.artifacts[0].content,
        form_name=payload["form_name"],
        module_bsl=compiled.artifacts[1].content,
    )

    assert result.coverage.structural == "passed"
    assert result.coverage.bsl_static == "passed"
    assert result.specification == compiled.specification


def test_checker_проверяет_button_group_внутри_command_bar():
    payload = _payload()
    payload["elements"].append(
        {
            "kind": "command_bar",
            "name": "Действия",
            "children": [
                {
                    "kind": "button_group",
                    "name": "ОсновныеДействия",
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
    compiled = compile_managed_form(payload)

    result = check_managed_form(
        compiled.artifacts[0].content,
        form_name=payload["form_name"],
        module_bsl=compiled.artifacts[1].content,
    )

    assert result.coverage.structural == "passed"
    assert result.coverage.bsl_static == "passed"
    assert result.specification == compiled.specification


def test_checker_использует_явный_версионный_профиль_событий():
    payload = _rich_payload()
    payload["platform_version"] = "8.3.25"
    payload["events"] = [{"event": "BeforeClose", "handler": "ПередЗакрытием"}]
    compiled = compile_managed_form(payload)

    result = check_managed_form(
        compiled.artifacts[0].content,
        form_name=payload["form_name"],
        module_bsl=compiled.artifacts[1].content,
        platform_version="8.3.25",
    )

    assert result.coverage.structural == "passed"
    assert result.coverage.bsl_static == "passed"
    assert result.specification == compiled.specification


def test_дубликат_id_внутри_элементов_даёт_structural_failed():
    xml, module = _pair()
    xml = xml.replace('name="ВтороеЗначение" id="6"', 'name="ВтороеЗначение" id="3"')

    result = check_managed_form(
        xml,
        form_name="ФормаПараметров",
        module_bsl=module,
    )

    assert result.coverage.structural == "failed"
    assert _diagnostics(result, "duplicate_element_id")


def test_одинаковый_id_элемента_реквизита_и_команды_допустим():
    xml, module = _pair()

    result = check_managed_form(
        xml,
        form_name="ФормаПараметров",
        module_bsl=module,
    )

    assert result.coverage.structural == "passed"
    assert _diagnostics(result, "separate_id_spaces_valid")


def test_повтор_языка_в_одном_заголовке_даёт_structural_failed():
    xml, module = _pair()
    root = ET.fromstring(xml)
    title = root.find(f"{{{LOGFORM}}}Title")
    first_item = title.find(f"{{{V8}}}item")
    title.append(ET.fromstring(ET.tostring(first_item, encoding="unicode")))

    result = check_managed_form(
        ET.tostring(root, encoding="unicode"),
        form_name="ФормаПараметров",
        module_bsl=module,
    )

    assert result.coverage.structural == "failed"
    assert _diagnostics(result, "duplicate_localization_language")


def test_пустой_локализованный_заголовок_не_даёт_ложный_failed():
    xml, module = _pair()
    root = ET.fromstring(xml)
    title = root.find(f"{{{LOGFORM}}}Title")
    for item in list(title):
        title.remove(item)

    result = check_managed_form(
        ET.tostring(root, encoding="unicode"),
        form_name="ФормаПараметров",
        module_bsl=module,
    )

    assert result.coverage.structural == "unsupported"
    assert not _diagnostics(result, "invalid_localization_item")


def test_вложенный_id_типа_не_смешивается_с_id_реквизита():
    xml, module = _pair()
    xml = xml.replace(
        "<v8:StringQualifiers>",
        '<v8:StringQualifiers id="1">',
        1,
    )

    result = check_managed_form(
        xml,
        form_name="ФормаПараметров",
        module_bsl=module,
    )

    assert result.coverage.structural == "unsupported"
    assert not _diagnostics(result, "duplicate_attribute_id")


def test_неизвестный_узел_не_получает_structural_passed():
    xml, module = _pair()
    xml = xml.replace("</Form>", "\t<Unknown/>\r\n</Form>")

    result = check_managed_form(
        xml,
        form_name="ФормаПараметров",
        module_bsl=module,
    )

    assert result.coverage.structural == "unsupported"
    assert _diagnostics(result, "unsupported_xml_node")


def test_отсутствующий_bsl_handler_даёт_warning_а_не_ложную_ошибку():
    xml, _module = _pair()

    result = check_managed_form(
        xml,
        form_name="ФормаПараметров",
        module_bsl="",
    )

    assert result.coverage.bsl_static == "warning"
    assert len(_diagnostics(result, "handler_not_found")) == 2


def test_неверная_директива_и_арность_bsl_handler_дают_failed():
    xml, module = _pair()
    module = module.replace("&НаСервере", "&НаКлиенте", 1).replace(
        "(Отказ, СтандартнаяОбработка)", "()", 1
    )

    result = check_managed_form(
        xml,
        form_name="ФормаПараметров",
        module_bsl=module,
    )

    assert result.coverage.bsl_static == "failed"
    assert _diagnostics(result, "handler_directive_mismatch")
    assert _diagnostics(result, "handler_arity_mismatch")


def test_отличающаяся_арность_без_других_ошибок_даёт_warning():
    xml, module = _pair()
    module = module.replace("(Команда)", "()", 1)

    result = check_managed_form(
        xml,
        form_name="ФормаПараметров",
        module_bsl=module,
    )

    assert result.coverage.bsl_static == "warning"
    assert _diagnostics(result, "handler_arity_mismatch")[0].status == "warning"


def test_зарезервированное_имя_xml_handler_даёт_bsl_failed():
    xml, module = _pair()
    xml = xml.replace("<Action>Проверить</Action>", "<Action>Выполнить</Action>")
    module = module.replace("Процедура Проверить(", "Процедура Выполнить(")

    result = check_managed_form(
        xml,
        form_name="ФормаПараметров",
        module_bsl=module,
    )

    assert result.coverage.bsl_static == "failed"
    assert _diagnostics(result, "reserved_bsl_keyword")


def test_синхронная_проверка_существования_файла_на_клиенте_даёт_bsl_failed():
    xml, module = _pair()
    module += """

&НаКлиенте
Процедура ПрочитатьФайл()

	ВыбранныйФайл = Новый Файл(\"пример.csv\");
	Если ВыбранныйФайл.Существует() Тогда
		Сообщить(\"Файл найден\");
	КонецЕсли;

КонецПроцедуры
"""

    result = check_managed_form(
        xml,
        form_name="ФормаПараметров",
        module_bsl=module,
    )

    assert result.coverage.bsl_static == "failed"
    assert _diagnostics(result, "forbidden_synchronous_client_call")


def test_проверка_существования_файла_на_сервере_не_даёт_ложный_failed():
    xml, module = _pair()
    module += """

&НаСервере
Процедура ПрочитатьФайлНаСервере()

	ВыбранныйФайл = Новый Файл("пример.csv");
	Если ВыбранныйФайл.Существует() Тогда
		Сообщить("Файл найден");
	КонецЕсли;

КонецПроцедуры
"""

    result = check_managed_form(
        xml,
        form_name="ФормаПараметров",
        module_bsl=module,
    )

    assert result.coverage.bsl_static == "passed"
    assert not _diagnostics(result, "forbidden_synchronous_client_call")


def test_без_module_bsl_уровень_остаётся_not_checked_с_причиной():
    xml, _module = _pair()

    result = check_managed_form(xml, form_name="ФормаПараметров")

    assert result.coverage.bsl_static == "not_checked"
    assert _diagnostics(result, "module_not_provided")


def test_unresolved_form_command_сохраняет_structural_failed():
    xml, module = _pair()
    xml = xml.replace("Form.Command.Проверить", "Form.Command.Неизвестная")

    result = check_managed_form(
        xml,
        form_name="ФормаПараметров",
        module_bsl=module,
    )

    assert result.coverage.structural == "failed"
    assert _diagnostics(result, "unresolved_command")


def test_checker_разрешает_поддержанные_стандартные_команды_формы_и_таблицы():
    payload = _rich_payload()
    payload["elements"][0]["children"].extend(
        [
            {
                "kind": "button",
                "name": "НастроитьФорму",
                "command": "CustomizeForm",
                "command_kind": "form_standard",
            },
            {
                "kind": "button",
                "name": "СтрокуВверх",
                "command": "MoveUp",
                "command_kind": "item_standard",
                "command_owner": "ТаблицаДанныхПоле",
            },
        ]
    )
    compiled = compile_managed_form(payload)

    result = check_managed_form(
        compiled.artifacts[0].content,
        form_name=payload["form_name"],
        module_bsl=compiled.artifacts[1].content,
    )

    assert result.coverage.structural == "passed"
    assert not _diagnostics(result, "non_form_command_not_checked")


def test_checker_не_пишет_файлы(monkeypatch, tmp_path):
    xml, module = _pair()
    before = list(tmp_path.iterdir())

    def forbidden_write(*_args, **_kwargs):
        raise AssertionError("checker не должен писать файлы")

    monkeypatch.setattr(Path, "write_text", forbidden_write)
    monkeypatch.setattr(Path, "write_bytes", forbidden_write)
    monkeypatch.setattr("builtins.open", forbidden_write)
    result = check_managed_form(
        xml,
        form_name="ФормаПараметров",
        module_bsl=module,
    )

    assert result.status == "checked"
    assert list(tmp_path.iterdir()) == before


def test_ждать_в_обычной_процедуре_даёт_failed():
    xml, module = _pair()
    module = module.replace(
        "\t// TODO: Реализовать обработчик команды.",
        "\tРезультат = Ждать ПолучитьРезультатАсинх();",
    )

    result = check_managed_form(
        xml, form_name="ФормаПараметров", module_bsl=module
    )

    assert result.coverage.bsl_static == "failed"
    assert _diagnostics(result, "await_requires_async")


def test_асинх_серверная_процедура_даёт_failed():
    xml, module = _pair()
    module = module.replace(
        "Процедура ПриСозданииНаСервере(",
        "Асинх Процедура ПриСозданииНаСервере(",
    )

    result = check_managed_form(
        xml, form_name="ФормаПараметров", module_bsl=module
    )

    assert result.coverage.bsl_static == "failed"
    assert _diagnostics(result, "async_requires_client_context")


def test_ждать_в_асинхронной_клиентской_процедуре_проходит():
    xml, module = _pair()
    module = module.replace(
        "Процедура Проверить(", "Асинх Процедура Проверить("
    ).replace(
        "\t// TODO: Реализовать обработчик команды.",
        "\tРезультат = Ждать ПолучитьРезультатАсинх();",
    )

    result = check_managed_form(
        xml, form_name="ФормаПараметров", module_bsl=module
    )

    assert result.coverage.bsl_static == "passed"
    assert not _diagnostics(result, "await_requires_async")
    assert not _diagnostics(result, "async_requires_client_context")


def test_checker_проверяет_директиву_события_элемента():
    payload = json.loads(
        (FIXTURES / "file_import_form.json").read_text(encoding="utf-8")
    )
    payload["events"] = [
        {
            "owner": "ПутьКФайлуПоле",
            "event": "OnChange",
            "handler": "ПутьКФайлуПриИзменении",
        }
    ]
    compiled = compile_managed_form(payload)
    xml, module = (item.content for item in compiled.artifacts)
    module = module.replace("&НаКлиенте", "&НаСервере", 1)

    result = check_managed_form(
        xml, form_name=payload["form_name"], module_bsl=module
    )

    assert result.coverage.bsl_static == "failed"
    assert _diagnostics(result, "handler_directive_mismatch")
