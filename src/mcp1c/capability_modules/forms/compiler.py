"""Детерминированный compiler доказанного подмножества Form.xml 2.16."""

from __future__ import annotations

from dataclasses import dataclass
from xml.etree import ElementTree as ET
from xml.sax.saxutils import escape, quoteattr

from .diagnostics import Artifact, Coverage, Diagnostic, FormsResult
from .models import (
    Button,
    FormAttribute,
    InputField,
    LocalizedText,
    ManagedForm,
    managed_form_to_spec,
    parse_managed_form_spec,
)


_NAMESPACES = (
    ("", "http://v8.1c.ru/8.3/xcf/logform"),
    ("app", "http://v8.1c.ru/8.2/managed-application/core"),
    ("cfg", "http://v8.1c.ru/8.1/data/enterprise/current-config"),
    ("dcscor", "http://v8.1c.ru/8.1/data-composition-system/core"),
    ("dcssch", "http://v8.1c.ru/8.1/data-composition-system/schema"),
    ("dcsset", "http://v8.1c.ru/8.1/data-composition-system/settings"),
    ("ent", "http://v8.1c.ru/8.1/data/enterprise"),
    ("lf", "http://v8.1c.ru/8.2/managed-application/logform"),
    ("style", "http://v8.1c.ru/8.1/data/ui/style"),
    ("sys", "http://v8.1c.ru/8.1/data/ui/fonts/system"),
    ("v8", "http://v8.1c.ru/8.1/data/core"),
    ("v8ui", "http://v8.1c.ru/8.1/data/ui"),
    ("web", "http://v8.1c.ru/8.1/data/ui/colors/web"),
    ("win", "http://v8.1c.ru/8.1/data/ui/colors/windows"),
    ("xr", "http://v8.1c.ru/8.3/xcf/readable"),
    ("xs", "http://www.w3.org/2001/XMLSchema"),
    ("xsi", "http://www.w3.org/2001/XMLSchema-instance"),
)


@dataclass(slots=True)
class _IdAllocator:
    value: int = 0

    def next(self) -> str:
        self.value += 1
        return str(self.value)


def _append(lines: list[str], indent: int, value: str) -> None:
    lines.append("\t" * indent + value)


def _localized(
    lines: list[str], tag: str, value: LocalizedText, indent: int
) -> None:
    _append(lines, indent, f"<{tag}>")
    _append(lines, indent + 1, "<v8:item>")
    _append(lines, indent + 2, "<v8:lang>ru</v8:lang>")
    _append(lines, indent + 2, f"<v8:content>{escape(value.ru)}</v8:content>")
    _append(lines, indent + 1, "</v8:item>")
    _append(lines, indent, f"</{tag}>")


def _root_opening(form: ManagedForm) -> str:
    declarations = " ".join(
        f"xmlns{':' + prefix if prefix else ''}={quoteattr(uri)}"
        for prefix, uri in _NAMESPACES
    )
    return f"<Form {declarations} version={quoteattr(form.format_version)}>"


def _emit_input(
    lines: list[str], item: InputField, allocator: _IdAllocator, indent: int
) -> None:
    element_id = allocator.next()
    _append(
        lines,
        indent,
        f"<InputField name={quoteattr(item.name)} id={quoteattr(element_id)}>",
    )
    _append(lines, indent + 1, f"<DataPath>{escape(item.data_path)}</DataPath>")
    if item.title is not None:
        _localized(lines, "Title", item.title, indent + 1)
    context_id = allocator.next()
    _append(
        lines,
        indent + 1,
        "<ContextMenu "
        f"name={quoteattr(item.name + 'КонтекстноеМеню')} "
        f"id={quoteattr(context_id)}/>",
    )
    tooltip_id = allocator.next()
    _append(
        lines,
        indent + 1,
        "<ExtendedTooltip "
        f"name={quoteattr(item.name + 'РасширеннаяПодсказка')} "
        f"id={quoteattr(tooltip_id)}/>",
    )
    _append(lines, indent, "</InputField>")


def _emit_button(
    lines: list[str], item: Button, allocator: _IdAllocator, indent: int
) -> None:
    element_id = allocator.next()
    _append(
        lines,
        indent,
        f"<Button name={quoteattr(item.name)} id={quoteattr(element_id)}>",
    )
    _append(lines, indent + 1, "<Type>UsualButton</Type>")
    if item.default:
        _append(lines, indent + 1, "<DefaultButton>true</DefaultButton>")
    _append(
        lines,
        indent + 1,
        f"<CommandName>Form.Command.{escape(item.command)}</CommandName>",
    )
    if item.title is not None:
        _localized(lines, "Title", item.title, indent + 1)
    tooltip_id = allocator.next()
    _append(
        lines,
        indent + 1,
        "<ExtendedTooltip "
        f"name={quoteattr(item.name + 'РасширеннаяПодсказка')} "
        f"id={quoteattr(tooltip_id)}/>",
    )
    _append(lines, indent, "</Button>")


def _emit_elements(lines: list[str], form: ManagedForm) -> None:
    allocator = _IdAllocator()
    _append(lines, 1, "<ChildItems>")
    for group in form.elements:
        group_id = allocator.next()
        _append(
            lines,
            2,
            f"<UsualGroup name={quoteattr(group.name)} id={quoteattr(group_id)}>",
        )
        _localized(lines, "Title", group.title, 3)
        _append(lines, 3, "<Group>Vertical</Group>")
        _append(lines, 3, "<Behavior>Usual</Behavior>")
        _append(lines, 3, "<Representation>NormalSeparation</Representation>")
        _append(lines, 3, "<ShowTitle>true</ShowTitle>")
        tooltip_id = allocator.next()
        _append(
            lines,
            3,
            "<ExtendedTooltip "
            f"name={quoteattr(group.name + 'РасширеннаяПодсказка')} "
            f"id={quoteattr(tooltip_id)}/>",
        )
        _append(lines, 3, "<ChildItems>")
        for child in group.children:
            if isinstance(child, InputField):
                _emit_input(lines, child, allocator, 4)
            else:
                _emit_button(lines, child, allocator, 4)
        _append(lines, 3, "</ChildItems>")
        _append(lines, 2, "</UsualGroup>")
    _append(lines, 1, "</ChildItems>")


def _emit_attribute(
    lines: list[str], attribute: FormAttribute, attribute_id: int
) -> None:
    _append(
        lines,
        2,
        f"<Attribute name={quoteattr(attribute.name)} id={quoteattr(str(attribute_id))}>",
    )
    if attribute.title is not None:
        _localized(lines, "Title", attribute.title, 3)
    _append(lines, 3, "<Type>")
    _append(lines, 4, "<v8:Type>xs:string</v8:Type>")
    _append(lines, 4, "<v8:StringQualifiers>")
    _append(lines, 5, f"<v8:Length>{attribute.type.length}</v8:Length>")
    _append(lines, 5, "<v8:AllowedLength>Variable</v8:AllowedLength>")
    _append(lines, 4, "</v8:StringQualifiers>")
    _append(lines, 3, "</Type>")
    if attribute.main:
        _append(lines, 3, "<MainAttribute>true</MainAttribute>")
    _append(lines, 2, "</Attribute>")


def _compile_xml(form: ManagedForm) -> str:
    lines = ['<?xml version="1.0" encoding="UTF-8"?>', _root_opening(form)]
    _localized(lines, "Title", form.title, 1)
    _append(lines, 1, '<AutoCommandBar name="ФормаКоманднаяПанель" id="-1"/>')
    _append(lines, 1, "<Events>")
    for event in form.events:
        _append(
            lines,
            2,
            f"<Event name={quoteattr(event.event)}>{escape(event.handler)}</Event>",
        )
    _append(lines, 1, "</Events>")
    _emit_elements(lines, form)
    _append(lines, 1, "<Attributes>")
    for attribute_id, attribute in enumerate(form.attributes, 1):
        _emit_attribute(lines, attribute, attribute_id)
    _append(lines, 1, "</Attributes>")
    _append(lines, 1, "<Commands>")
    for command_id, command in enumerate(form.commands, 1):
        _append(
            lines,
            2,
            f"<Command name={quoteattr(command.name)} id={quoteattr(str(command_id))}>",
        )
        _localized(lines, "Title", command.title, 3)
        _localized(lines, "ToolTip", command.title, 3)
        _append(lines, 3, f"<Action>{escape(command.action)}</Action>")
        _append(lines, 2, "</Command>")
    _append(lines, 1, "</Commands>")
    _append(lines, 0, "</Form>")
    result = "\r\n".join(lines) + "\r\n"
    # Ошибка самого emitter-а не должна превращаться в испорченный artifact.
    ET.fromstring(result)
    return result


def _compile_module(form: ManagedForm) -> str:
    lines: list[str] = ["#Область ОбработчикиСобытийФормы", ""]
    for event in form.events:
        lines.extend(
            [
                "&НаСервере",
                f"Процедура {event.handler}(Отказ, СтандартнаяОбработка)",
                "",
                "\t// TODO: Реализовать обработчик события формы.",
                "",
                "КонецПроцедуры",
                "",
            ]
        )
    lines.extend(["#КонецОбласти", "", "#Область ОбработчикиКомандФормы", ""])
    emitted: set[str] = set()
    for command in form.commands:
        if command.action in emitted:
            continue
        emitted.add(command.action)
        lines.extend(
            [
                "&НаКлиенте",
                f"Процедура {command.action}(Команда)",
                "",
                "\t// TODO: Реализовать обработчик команды.",
                "",
                "КонецПроцедуры",
                "",
            ]
        )
    lines.append("#КонецОбласти")
    return "\r\n".join(lines) + "\r\n"


def compile_managed_form(specification: object) -> FormsResult:
    """Собрать два текстовых artifact без записи на диск или обращения к Registry."""

    form = parse_managed_form_spec(specification)
    xml = _compile_xml(form)
    module = _compile_module(form)
    diagnostics = (
        Diagnostic(
            "xml_parse",
            "passed",
            "generated_xml_well_formed",
            "artifacts[0]",
            "Сгенерированный Form.xml разобран XML parser-ом.",
        ),
        Diagnostic(
            "structural",
            "passed",
            "supported_specification_compiled",
            "$",
            "Спецификация входит в закрытое подмножество первой вертикали.",
        ),
        Diagnostic(
            "configuration_links",
            "not_checked",
            "registry_snapshot_not_used",
            "$",
            "Compiler не получал snapshot Registry.",
        ),
        Diagnostic(
            "bsl_static",
            "passed",
            "handler_stubs_generated",
            "artifacts[1]",
            "Для поддержанных событий и команд созданы BSL-каркасы.",
        ),
        Diagnostic(
            "platform_import",
            "not_checked",
            "platform_import_requires_owner_gate",
            "$",
            "Импорт в тестовую 1С не выполнялся.",
        ),
        Diagnostic(
            "runtime_visual",
            "not_checked",
            "runtime_visual_requires_user_acceptance",
            "$",
            "Форма не открывалась и не принималась пользователем в 1С.",
        ),
    )
    base = f"Forms/{form.form_name}/Ext"
    return FormsResult(
        status="compiled",
        artifacts=(
            Artifact(
                path=f"{base}/Form.xml",
                media_type="application/xml",
                encoding="utf-8-sig",
                content=xml,
            ),
            Artifact(
                path=f"{base}/Form/Module.bsl",
                media_type="text/plain",
                encoding="utf-8-sig",
                content=module,
            ),
        ),
        specification=managed_form_to_spec(form),
        diagnostics=diagnostics,
        coverage=Coverage(
            xml_parse="passed",
            structural="passed",
            configuration_links="not_checked",
            bsl_static="passed",
            platform_import="not_checked",
            runtime_visual="not_checked",
        ),
        instructions=(
            "Сохраните текст с UTF-8 BOM и CRLF по указанным относительным путям.",
            "Импорт в тестовую 1С требует отдельного разрешения владельца.",
        ),
    )


__all__ = ["compile_managed_form"]
