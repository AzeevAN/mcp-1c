"""Безопасный decompiler доказанного подмножества Form.xml."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from xml.etree import ElementTree as ET

from .diagnostics import Coverage, Diagnostic, FormsResult
from .models import (
    SUPPORTED_FORMAT_VERSION,
    FormsContractError,
    managed_form_to_spec,
    parse_managed_form_spec,
)


# Предел не даёт синхронному stdlib parser принимать неограниченный вход;
# повышать его можно только после отдельного замера времени, RSS и ответа.
MAX_FORM_XML_BYTES = 2 * 1024 * 1024
_LOGFORM = "http://v8.1c.ru/8.3/xcf/logform"
_V8 = "http://v8.1c.ru/8.1/data/core"
_IDENTIFIER = re.compile(r"[A-Za-zА-Яа-яЁё_][A-Za-zА-Яа-яЁё0-9_]*\Z")
_FORBIDDEN_DOCTYPE = re.compile(r"<!\s*DOCTYPE\b", re.IGNORECASE)
_FORBIDDEN_ENTITY = re.compile(r"<!\s*ENTITY\b", re.IGNORECASE)
_SINGLETON_TAGS = frozenset(
    {
        "Title",
        "AutoCommandBar",
        "Events",
        "ChildItems",
        "Attributes",
        "Commands",
        "Group",
        "Behavior",
        "Representation",
        "ShowTitle",
        "ExtendedTooltip",
        "ContextMenu",
        "Type",
        "StringQualifiers",
        "Length",
        "AllowedLength",
        "DefaultButton",
        "CommandName",
        "DataPath",
        "ToolTip",
        "Action",
        "lang",
        "content",
        "MainAttribute",
    }
)


def _q(local: str, namespace: str = _LOGFORM) -> str:
    return f"{{{namespace}}}{local}"


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _path_map(root: ET.Element) -> dict[int, str]:
    paths = {id(root): f"/{_local(root.tag)}"}

    def walk(parent: ET.Element) -> None:
        counts: dict[str, int] = {}
        parent_path = paths[id(parent)]
        for child in parent:
            local = _local(child.tag)
            counts[local] = counts.get(local, 0) + 1
            suffix = "" if local in _SINGLETON_TAGS else f"[{counts[local]}]"
            paths[id(child)] = f"{parent_path}/{local}{suffix}"
            walk(child)

    walk(root)
    return paths


@dataclass(slots=True)
class _Inventory:
    paths: dict[int, str]
    known_nodes: set[int] = field(default_factory=set)
    known_attributes: set[tuple[int, str]] = field(default_factory=set)
    diagnostics: list[Diagnostic] = field(default_factory=list)
    failed: bool = False
    unsupported: bool = False

    def mark(self, node: ET.Element, *attributes: str) -> None:
        self.known_nodes.add(id(node))
        self.known_attributes.update((id(node), name) for name in attributes)

    def issue(
        self,
        code: str,
        path: str,
        message: str,
        *,
        status: str,
    ) -> None:
        if status == "failed":
            self.failed = True
        elif status == "unsupported":
            self.unsupported = True
        self.diagnostics.append(
            Diagnostic(
                level="structural",
                status=status,
                code=code,
                path=path,
                message=message,
            )
        )

    def required_child(
        self,
        parent: ET.Element,
        local: str,
        *,
        namespace: str = _LOGFORM,
    ) -> ET.Element | None:
        matches = [child for child in parent if child.tag == _q(local, namespace)]
        if len(matches) != 1:
            self.issue(
                "missing_or_repeated_xml_node",
                f"{self.paths[id(parent)]}/{local}",
                f"Ожидался ровно один XML-узел {local}.",
                status="unsupported",
            )
            return matches[0] if matches else None
        return matches[0]

    def optional_container(
        self,
        parent: ET.Element,
        local: str,
    ) -> ET.Element | None:
        matches = [child for child in parent if child.tag == _q(local)]
        if not matches:
            return None
        if len(matches) > 1:
            self.issue(
                "repeated_xml_node",
                f"{self.paths[id(parent)]}/{local}",
                f"XML-секция {local} повторяется.",
                status="unsupported",
            )
        self.mark(matches[0])
        return matches[0]

    def report_uncovered(self, root: ET.Element) -> None:
        for node in root.iter():
            path = self.paths[id(node)]
            if id(node) not in self.known_nodes:
                self.issue(
                    "unsupported_xml_node",
                    path,
                    f"Неподдержанный XML-узел: {_local(node.tag)}.",
                    status="unsupported",
                )
            for name in sorted(node.attrib):
                if (id(node), name) not in self.known_attributes:
                    self.issue(
                        "unsupported_xml_attribute",
                        f"{path}/@{_local(name)}",
                        f"Неподдержанный XML-атрибут: {_local(name)}.",
                        status="unsupported",
                    )


def _text(node: ET.Element | None) -> str:
    return "" if node is None or node.text is None else node.text


def _localized(
    inventory: _Inventory,
    parent: ET.Element,
    local: str,
) -> dict[str, str]:
    node = inventory.required_child(parent, local)
    if node is None:
        return {"ru": ""}
    inventory.mark(node)
    item = inventory.required_child(node, "item", namespace=_V8)
    if item is None:
        return {"ru": ""}
    inventory.mark(item)
    lang = inventory.required_child(item, "lang", namespace=_V8)
    content = inventory.required_child(item, "content", namespace=_V8)
    if lang is not None:
        inventory.mark(lang)
    if content is not None:
        inventory.mark(content)
    if _text(lang) != "ru":
        inventory.issue(
            "unsupported_localization",
            inventory.paths[id(lang)] if lang is not None else inventory.paths[id(item)],
            "Первая вертикаль декомпилирует только русскую локализацию.",
            status="unsupported",
        )
    return {"ru": _text(content)}


def _optional_localized(
    inventory: _Inventory,
    parent: ET.Element,
    local: str,
) -> dict[str, str] | None:
    matches = [child for child in parent if child.tag == _q(local)]
    if not matches:
        return None
    if len(matches) > 1:
        inventory.issue(
            "repeated_xml_node",
            f"{inventory.paths[id(parent)]}/{local}",
            f"XML-узел {local} повторяется.",
            status="failed",
        )
    return _localized(inventory, parent, local)


def _attribute_value(
    inventory: _Inventory,
    node: ET.Element,
    name: str,
) -> str:
    inventory.known_attributes.add((id(node), name))
    value = node.get(name)
    if value is None or not value:
        inventory.issue(
            "missing_xml_attribute",
            f"{inventory.paths[id(node)]}/@{name}",
            f"Обязательный XML-атрибут {name} отсутствует.",
            status="failed",
        )
        return ""
    return value


def _subset_attribute(
    inventory: _Inventory,
    node: ET.Element,
    name: str,
) -> str:
    """Прочитать служебный атрибут, не выдавая его отсутствие за битый XML."""

    inventory.known_attributes.add((id(node), name))
    value = node.get(name)
    if value is None or not value:
        inventory.issue(
            "missing_subset_attribute",
            f"{inventory.paths[id(node)]}/@{name}",
            f"Атрибут {name} нужен для roundtrip первой вертикали.",
            status="unsupported",
        )
        return ""
    return value


def _service_node(
    inventory: _Inventory,
    parent: ET.Element,
    local: str,
    expected: str,
) -> ET.Element | None:
    node = inventory.required_child(parent, local)
    if node is None:
        return None
    inventory.mark(node)
    if _text(node) != expected:
        inventory.issue(
            "unsupported_xml_value",
            inventory.paths[id(node)],
            f"Неподдержанное значение XML-узла {local}.",
            status="unsupported",
        )
    return node


def _companion(
    inventory: _Inventory,
    parent: ET.Element,
    local: str,
) -> None:
    node = inventory.required_child(parent, local)
    if node is not None:
        inventory.mark(node, "name", "id")
        _subset_attribute(inventory, node, "name")
        _subset_attribute(inventory, node, "id")


def _input_field(inventory: _Inventory, node: ET.Element) -> dict[str, object]:
    inventory.mark(node, "name", "id")
    item: dict[str, object] = {
        "kind": "input_field",
        "name": _attribute_value(inventory, node, "name"),
    }
    _subset_attribute(inventory, node, "id")
    data_path = inventory.required_child(node, "DataPath")
    if data_path is not None:
        inventory.mark(data_path)
    item["data_path"] = _text(data_path)
    if not _IDENTIFIER.fullmatch(_text(data_path)):
        inventory.issue(
            "unsupported_data_path",
            inventory.paths[id(data_path)] if data_path is not None else inventory.paths[id(node)],
            "DataPath сохранён в inventory, но не входит в compiler первой вертикали.",
            status="unsupported",
        )
    title = _optional_localized(inventory, node, "Title")
    if title is not None:
        item["title"] = title
    _companion(inventory, node, "ContextMenu")
    _companion(inventory, node, "ExtendedTooltip")
    return item


def _button(inventory: _Inventory, node: ET.Element) -> dict[str, object]:
    inventory.mark(node, "name", "id")
    item: dict[str, object] = {
        "kind": "button",
        "name": _attribute_value(inventory, node, "name"),
    }
    _subset_attribute(inventory, node, "id")
    _service_node(inventory, node, "Type", "UsualButton")
    default_nodes = [child for child in node if child.tag == _q("DefaultButton")]
    if default_nodes:
        default_node = default_nodes[0]
        inventory.mark(default_node)
        if len(default_nodes) > 1 or _text(default_node) not in {"true", "false"}:
            inventory.issue(
                "unsupported_xml_value",
                inventory.paths[id(default_node)],
                "DefaultButton должен быть true либо false.",
                status="unsupported",
            )
        if _text(default_node) == "true":
            item["default"] = True
    command = inventory.required_child(node, "CommandName")
    if command is not None:
        inventory.mark(command)
    command_name = _text(command)
    prefix = "Form.Command."
    if not command_name.startswith(prefix):
        inventory.issue(
            "unsupported_command_path",
            inventory.paths[id(command)] if command is not None else inventory.paths[id(node)],
            "Поддерживается только ссылка Form.Command.<Имя>.",
            status="unsupported",
        )
    item["command"] = (
        command_name.removeprefix(prefix) if command_name.startswith(prefix) else command_name
    )
    title = _optional_localized(inventory, node, "Title")
    if title is not None:
        item["title"] = title
    _companion(inventory, node, "ExtendedTooltip")
    return item


def _elements(inventory: _Inventory, root: ET.Element) -> list[dict[str, object]]:
    container = inventory.optional_container(root, "ChildItems")
    if container is None:
        return []
    groups: list[dict[str, object]] = []
    for node in container:
        if node.tag != _q("UsualGroup"):
            continue
        inventory.mark(node, "name", "id")
        group: dict[str, object] = {
            "kind": "usual_group",
            "name": _attribute_value(inventory, node, "name"),
            "title": _localized(inventory, node, "Title"),
        }
        _subset_attribute(inventory, node, "id")
        _service_node(inventory, node, "Group", "Vertical")
        _service_node(inventory, node, "Behavior", "Usual")
        _service_node(inventory, node, "Representation", "NormalSeparation")
        _service_node(inventory, node, "ShowTitle", "true")
        _companion(inventory, node, "ExtendedTooltip")
        children_node = inventory.required_child(node, "ChildItems")
        children: list[dict[str, object]] = []
        if children_node is not None:
            inventory.mark(children_node)
            for child in children_node:
                if child.tag == _q("InputField"):
                    children.append(_input_field(inventory, child))
                elif child.tag == _q("Button"):
                    children.append(_button(inventory, child))
        group["children"] = children
        groups.append(group)
    return groups


def _attributes(inventory: _Inventory, root: ET.Element) -> list[dict[str, object]]:
    container = inventory.optional_container(root, "Attributes")
    if container is None:
        return []
    result: list[dict[str, object]] = []
    for node in container:
        if node.tag != _q("Attribute"):
            continue
        inventory.mark(node, "name", "id")
        item: dict[str, object] = {
            "name": _attribute_value(inventory, node, "name"),
        }
        _subset_attribute(inventory, node, "id")
        title = _optional_localized(inventory, node, "Title")
        if title is not None:
            item["title"] = title
        type_nodes = [child for child in node if child.tag == _q("Type")]
        type_node = type_nodes[0] if type_nodes else None
        supported_type = type_node is not None and len(type_nodes) == 1
        length = 0
        if type_node is None:
            inventory.issue(
                "attribute_type_not_representable",
                f"{inventory.paths[id(node)]}/Type",
                "Тип реквизита отсутствует или задан вне поддержанного Type.",
                status="unsupported",
            )
        else:
            inventory.mark(type_node)
            if len(type_nodes) > 1:
                inventory.issue(
                    "repeated_xml_node",
                    f"{inventory.paths[id(node)]}/Type",
                    "XML-узел Type повторяется.",
                    status="unsupported",
                )
            type_name = inventory.required_child(type_node, "Type", namespace=_V8)
            qualifiers = inventory.required_child(
                type_node, "StringQualifiers", namespace=_V8
            )
            if type_name is not None:
                inventory.mark(type_name)
                if _text(type_name) != "xs:string":
                    supported_type = False
                    inventory.issue(
                        "unsupported_attribute_type",
                        inventory.paths[id(type_name)],
                        "Первая вертикаль поддерживает только xs:string.",
                        status="unsupported",
                    )
            else:
                supported_type = False
            if qualifiers is not None:
                inventory.mark(qualifiers)
                length_node = inventory.required_child(
                    qualifiers, "Length", namespace=_V8
                )
                allowed = inventory.required_child(
                    qualifiers, "AllowedLength", namespace=_V8
                )
                if length_node is not None:
                    inventory.mark(length_node)
                    try:
                        length = int(_text(length_node))
                    except ValueError:
                        supported_type = False
                        inventory.issue(
                            "invalid_string_length",
                            inventory.paths[id(length_node)],
                            "Длина строки должна быть целым числом.",
                            status="failed",
                        )
                else:
                    supported_type = False
                if allowed is not None:
                    inventory.mark(allowed)
                    if _text(allowed) != "Variable":
                        supported_type = False
                        inventory.issue(
                            "unsupported_allowed_length",
                            inventory.paths[id(allowed)],
                            "Поддерживается только переменная длина строки.",
                            status="unsupported",
                        )
                else:
                    supported_type = False
            else:
                supported_type = False
        if supported_type and length > 0:
            item["type"] = {"kind": "string", "length": length}
        main_nodes = [child for child in node if child.tag == _q("MainAttribute")]
        if main_nodes:
            main = main_nodes[0]
            inventory.mark(main)
            if len(main_nodes) > 1 or _text(main) not in {"true", "false"}:
                inventory.issue(
                    "unsupported_xml_value",
                    inventory.paths[id(main)],
                    "MainAttribute должен быть true либо false.",
                    status="unsupported",
                )
            if _text(main) == "true":
                item["main"] = True
        result.append(item)
    return result


def _commands(inventory: _Inventory, root: ET.Element) -> list[dict[str, object]]:
    container = inventory.optional_container(root, "Commands")
    if container is None:
        return []
    result: list[dict[str, object]] = []
    for node in container:
        if node.tag != _q("Command"):
            continue
        inventory.mark(node, "name", "id")
        title = _localized(inventory, node, "Title")
        item: dict[str, object] = {
            "name": _attribute_value(inventory, node, "name"),
            "title": title,
        }
        _subset_attribute(inventory, node, "id")
        tooltip = _localized(inventory, node, "ToolTip")
        if tooltip != title:
            tooltip_node = next(
                (child for child in node if child.tag == _q("ToolTip")), node
            )
            inventory.issue(
                "unsupported_command_tooltip",
                inventory.paths[id(tooltip_node)],
                "Отдельная от заголовка подсказка команды не входит в спецификацию.",
                status="unsupported",
            )
        action_nodes = [child for child in node if child.tag == _q("Action")]
        action = action_nodes[0] if action_nodes else None
        if action is None:
            inventory.issue(
                "command_action_not_representable",
                f"{inventory.paths[id(node)]}/Action",
                "Команда без Action сохранена только как inventory.",
                status="unsupported",
            )
        else:
            inventory.mark(action)
            if len(action_nodes) > 1:
                inventory.issue(
                    "repeated_xml_node",
                    f"{inventory.paths[id(node)]}/Action",
                    "XML-узел Action повторяется.",
                    status="unsupported",
                )
            item["action"] = _text(action)
        result.append(item)
    return result


def _events(inventory: _Inventory, root: ET.Element) -> list[dict[str, object]]:
    container = inventory.optional_container(root, "Events")
    if container is None:
        return []
    result: list[dict[str, object]] = []
    for node in container:
        if node.tag != _q("Event"):
            continue
        inventory.mark(node, "name")
        event_name = _attribute_value(inventory, node, "name")
        if event_name != "OnCreateAtServer":
            inventory.issue(
                "unsupported_event",
                f"{inventory.paths[id(node)]}/@name",
                f"Событие {event_name or '<пусто>'} сохранено только в inventory.",
                status="unsupported",
            )
        result.append({"event": event_name, "handler": _text(node)})
    return result


def _not_checked_diagnostics(module_bsl: str | None) -> tuple[Diagnostic, ...]:
    return (
        Diagnostic(
            "configuration_links",
            "not_checked",
            "registry_snapshot_not_used",
            "$",
            "Decompiler не получал snapshot Registry.",
        ),
        Diagnostic(
            "bsl_static",
            "not_checked",
            "bsl_check_deferred" if module_bsl is not None else "module_not_provided",
            "$module_bsl",
            (
                "Переданный BSL будет сопоставляться с XML на этапе статических проверок."
                if module_bsl is not None
                else "Module.bsl не передан."
            ),
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


def _rejected(
    code: str,
    path: str,
    message: str,
    *,
    xml_status: str,
    module_bsl: str | None,
) -> FormsResult:
    level = "xml_parse" if xml_status == "failed" else "structural"
    diagnostics: list[Diagnostic] = [
        Diagnostic(level, "failed", code, path, message)
    ]
    if xml_status == "failed":
        diagnostics.append(
            Diagnostic(
                "structural",
                "not_checked",
                "structure_not_checked_after_xml_rejection",
                "$form_xml",
                "Структура не проверялась после отклонения XML.",
            )
        )
    elif xml_status == "not_checked":
        diagnostics.append(
            Diagnostic(
                "xml_parse",
                "not_checked",
                "xml_not_checked_after_input_rejection",
                "$form_xml",
                "XML не разбирался после отклонения входных параметров.",
            )
        )
    diagnostics.extend(_not_checked_diagnostics(module_bsl))
    return FormsResult(
        status="rejected",
        diagnostics=tuple(diagnostics),
        coverage=Coverage(
            xml_parse=xml_status,
            structural="not_checked" if xml_status == "failed" else "failed",
            configuration_links="not_checked",
            bsl_static="not_checked",
            platform_import="not_checked",
            runtime_visual="not_checked",
        ),
    )


def decompile_managed_form(
    form_xml: object,
    *,
    form_name: object,
    module_bsl: object | None = None,
) -> FormsResult:
    """Разобрать Form.xml без записи и без молчаливой потери неизвестных узлов."""

    safe_module = module_bsl if isinstance(module_bsl, str) else None
    if not isinstance(form_xml, str):
        return _rejected(
            "invalid_form_xml",
            "$form_xml",
            "Form.xml должен быть строкой.",
            xml_status="failed",
            module_bsl=safe_module,
        )
    if module_bsl is not None and not isinstance(module_bsl, str):
        return _rejected(
            "invalid_module_bsl",
            "$module_bsl",
            "Module.bsl должен быть строкой либо null.",
            xml_status="not_checked",
            module_bsl=None,
        )
    if not isinstance(form_name, str) or not _IDENTIFIER.fullmatch(form_name):
        return _rejected(
            "invalid_form_name",
            "$form_name",
            "Имя формы должно быть допустимым идентификатором 1С.",
            xml_status="not_checked",
            module_bsl=safe_module,
        )
    try:
        encoded_size = len(form_xml.encode("utf-8"))
    except UnicodeEncodeError:
        return _rejected(
            "invalid_unicode",
            "$form_xml",
            "Form.xml содержит некодируемый Unicode.",
            xml_status="failed",
            module_bsl=safe_module,
        )
    if encoded_size > MAX_FORM_XML_BYTES:
        return _rejected(
            "form_xml_too_large",
            "$form_xml",
            f"Form.xml превышает лимит {MAX_FORM_XML_BYTES} байт.",
            xml_status="failed",
            module_bsl=safe_module,
        )
    if _FORBIDDEN_DOCTYPE.search(form_xml):
        return _rejected(
            "doctype_forbidden",
            "$form_xml",
            "DOCTYPE в Form.xml запрещён.",
            xml_status="failed",
            module_bsl=safe_module,
        )
    if _FORBIDDEN_ENTITY.search(form_xml):
        return _rejected(
            "entity_forbidden",
            "$form_xml",
            "ENTITY в Form.xml запрещён.",
            xml_status="failed",
            module_bsl=safe_module,
        )
    try:
        root = ET.fromstring(form_xml)
    except ET.ParseError:
        return _rejected(
            "malformed_xml",
            "$form_xml",
            "Form.xml не является корректным XML.",
            xml_status="failed",
            module_bsl=safe_module,
        )
    if root.tag != _q("Form"):
        return _rejected(
            "unexpected_root",
            "/Form",
            "Ожидался корень Form в namespace управляемой формы.",
            xml_status="passed",
            module_bsl=safe_module,
        )

    inventory = _Inventory(_path_map(root))
    inventory.mark(root, "version")
    version = _subset_attribute(inventory, root, "version")
    if version != SUPPORTED_FORMAT_VERSION:
        inventory.issue(
            "unsupported_format_version",
            "/Form/@version",
            f"Формат {version or '<пусто>'} читается только как inventory.",
            status="unsupported",
        )

    title = _localized(inventory, root, "Title")
    command_bar = inventory.required_child(root, "AutoCommandBar")
    if command_bar is not None:
        inventory.mark(command_bar, "name", "id")
        name = _subset_attribute(inventory, command_bar, "name")
        command_id = _subset_attribute(inventory, command_bar, "id")
        if name != "ФормаКоманднаяПанель" or command_id != "-1":
            inventory.issue(
                "unsupported_auto_command_bar",
                inventory.paths[id(command_bar)],
                "Поддерживается стандартная AutoCommandBar с ID -1.",
                status="unsupported",
            )

    specification: dict[str, object] = {
        "schema_version": 1,
        "form_name": form_name,
        "format_version": version,
        "title": title,
        "attributes": _attributes(inventory, root),
        "elements": _elements(inventory, root),
        "commands": _commands(inventory, root),
        "events": _events(inventory, root),
    }
    inventory.report_uncovered(root)

    collections = (
        specification["attributes"],
        specification["elements"],
        specification["commands"],
        specification["events"],
    )
    if any(not collection for collection in collections):
        inventory.issue(
            "outside_compiler_subset",
            "/Form",
            "Inventory не образует полную спецификацию compiler первой вертикали.",
            status="unsupported",
        )

    if (
        version == SUPPORTED_FORMAT_VERSION
        and not inventory.unsupported
        and not inventory.failed
    ):
        try:
            specification = managed_form_to_spec(
                parse_managed_form_spec(specification)
            )
        except FormsContractError as error:
            inventory.failed = True
            inventory.diagnostics.extend(error.diagnostics)

    if inventory.failed:
        structural_status = "failed"
        status = "rejected"
        instructions = (
            "Спецификация неполна или противоречива; повторная компиляция запрещена.",
        )
    elif inventory.unsupported:
        structural_status = "unsupported"
        status = "decompiled"
        instructions = (
            "Результат является inventory; allow_lossy недоступен, повторная "
            "компиляция запрещена.",
        )
    else:
        structural_status = "passed"
        status = "decompiled"
        instructions = (
            "Спецификация пригодна для нормализованного roundtrip первой вертикали.",
        )

    diagnostics: list[Diagnostic] = [
        Diagnostic(
            "xml_parse",
            "passed",
            "form_xml_parsed",
            "$form_xml",
            "Form.xml разобран XML parser-ом.",
        ),
        *inventory.diagnostics,
    ]
    if structural_status == "passed":
        diagnostics.append(
            Diagnostic(
                "structural",
                "passed",
                "supported_form_decompiled",
                "/Form",
                "Form.xml полностью входит в подмножество первой вертикали.",
            )
        )
    diagnostics.extend(_not_checked_diagnostics(safe_module))
    return FormsResult(
        status=status,
        specification=specification,
        diagnostics=tuple(diagnostics),
        coverage=Coverage(
            xml_parse="passed",
            structural=structural_status,
            configuration_links="not_checked",
            bsl_static="not_checked",
            platform_import="not_checked",
            runtime_visual="not_checked",
        ),
        instructions=instructions,
    )


__all__ = ["MAX_FORM_XML_BYTES", "decompile_managed_form"]
