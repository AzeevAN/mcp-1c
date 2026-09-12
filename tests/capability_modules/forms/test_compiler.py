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


def test_невалидная_спецификация_отклоняется_до_создания_результата():
    payload = _payload()
    payload["format_version"] = "2.20"

    with pytest.raises(FormsContractError, match="2.16"):
        compile_managed_form(payload)


def test_один_action_двух_команд_создаёт_одну_процедуру():
    payload = _payload()
    payload["commands"].append(
        {
            "name": "ВыполнитьПовторно",
            "title": {"ru": "Выполнить повторно"},
            "action": "Выполнить",
        }
    )

    module = compile_managed_form(payload).artifacts[1].content

    assert module.count("Процедура Выполнить(Команда)") == 1


def test_сгенерированные_bsl_каркасы_читаются_текущим_лексером():
    module = compile_managed_form(_payload()).artifacts[1].content

    procedures = разобрать(module)

    assert [item.имя for item in procedures] == [
        "ПриСозданииНаСервере",
        "Выполнить",
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
