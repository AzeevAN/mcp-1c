"""Безопасный decompiler доказанного подмножества Form.xml."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from xml.etree import ElementTree as ET

from .command_catalog import ITEM_STANDARD_COMMANDS, standard_command_supported
from .diagnostics import Coverage, Diagnostic, FormsResult
from .event_catalog import event_signature
from .metadata_types import (
    dynamic_list_registry_ref,
    metadata_object_registry_ref,
    metadata_reference_registry_ref,
)
from .models import (
    SUPPORTED_FORMAT_VERSION,
    FormsContractError,
    managed_form_to_spec,
    parse_managed_form_spec,
)
from .version_catalog import (
    normalized_platform_version,
    platform_compatibility_note,
    platform_profile,
)


# Предел не даёт синхронному stdlib parser принимать неограниченный вход;
# повышать его можно только после отдельного замера времени, RSS и ответа.
MAX_FORM_XML_BYTES = 2 * 1024 * 1024
_LOGFORM = "http://v8.1c.ru/8.3/xcf/logform"
_V8 = "http://v8.1c.ru/8.1/data/core"
_DCSSET = "http://v8.1c.ru/8.1/data-composition-system/settings"
_XR = "http://v8.1c.ru/8.3/xcf/readable"
_XSI = "http://www.w3.org/2001/XMLSchema-instance"
_IDENTIFIER = re.compile(r"[A-Za-zА-Яа-яЁё_][A-Za-zА-Яа-яЁё0-9_]*\Z")
_DATA_PATH = re.compile(
    r"[A-Za-zА-Яа-яЁё_][A-Za-zА-Яа-яЁё0-9_]*"
    r"(?:\.[A-Za-zА-Яа-яЁё_][A-Za-zА-Яа-яЁё0-9_]*)*\Z"
)
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
        "NumberQualifiers",
        "DateQualifiers",
        "Length",
        "AllowedLength",
        "Digits",
        "FractionDigits",
        "AllowedSign",
        "DateFractions",
        "DefaultButton",
        "CommandName",
        "DataPath",
        "MultiLine",
        "ReadOnly",
        "ListChoiceMode",
        "HorizontalStretch",
        "VerticalStretch",
        "HorizontalLocation",
        "ChoiceList",
        "Presentation",
        "Value",
        "CheckState",
        "CheckBoxType",
        "Hyperlink",
        "Hiperlink",
        "PagesRepresentation",
        "AdditionSource",
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


def _optional_true(
    inventory: _Inventory,
    parent: ET.Element,
    local: str,
) -> bool:
    return _optional_boolean(inventory, parent, local) is True


def _optional_boolean(
    inventory: _Inventory,
    parent: ET.Element,
    local: str,
) -> bool | None:
    matches = [child for child in parent if child.tag == _q(local)]
    if not matches:
        return None
    node = matches[0]
    inventory.mark(node)
    if len(matches) > 1 or _text(node) not in {"true", "false"}:
        inventory.issue(
            "unsupported_xml_value",
            inventory.paths[id(node)],
            f"{local} должен быть true либо false.",
            status="unsupported",
        )
    return _text(node) == "true"


def _choice_list(
    inventory: _Inventory,
    parent: ET.Element,
) -> list[dict[str, object]]:
    containers = [child for child in parent if child.tag == _q("ChoiceList")]
    if not containers:
        return []
    container = containers[0]
    inventory.mark(container)
    if len(containers) > 1:
        inventory.issue(
            "repeated_xml_node",
            f"{inventory.paths[id(parent)]}/ChoiceList",
            "ChoiceList повторяется.",
            status="unsupported",
        )
    result: list[dict[str, object]] = []
    for node in container:
        if node.tag != _q("Item", _XR):
            continue
        inventory.mark(node)
        presentation_stub = inventory.required_child(
            node, "Presentation", namespace=_XR
        )
        check = inventory.required_child(node, "CheckState", namespace=_XR)
        value_container = inventory.required_child(node, "Value", namespace=_XR)
        if presentation_stub is not None:
            inventory.mark(presentation_stub)
        if check is not None:
            inventory.mark(check)
            if _text(check) != "0":
                inventory.issue(
                    "unsupported_choice_check_state",
                    inventory.paths[id(check)],
                    "Поддерживается CheckState=0.",
                    status="unsupported",
                )
        if value_container is None:
            continue
        xsi_type = _q("type", _XSI)
        inventory.mark(value_container, xsi_type)
        if value_container.get(xsi_type) != "FormChoiceListDesTimeValue":
            inventory.issue(
                "unsupported_choice_value_type",
                inventory.paths[id(value_container)],
                "Поддерживается FormChoiceListDesTimeValue.",
                status="unsupported",
            )
        presentation = _localized(
            inventory, value_container, "Presentation"
        )
        value = inventory.required_child(value_container, "Value")
        if value is not None:
            inventory.mark(value, xsi_type)
            if value.get(xsi_type) != "xs:string":
                inventory.issue(
                    "unsupported_choice_scalar_type",
                    inventory.paths[id(value)],
                    "Базовый ChoiceList поддерживает строковые значения.",
                    status="unsupported",
                )
        result.append(
            {"value": _text(value), "presentation": presentation}
        )
    return result


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
    if not _DATA_PATH.fullmatch(_text(data_path)):
        inventory.issue(
            "unsupported_data_path",
            inventory.paths[id(data_path)] if data_path is not None else inventory.paths[id(node)],
            "DataPath сохранён в inventory, но не входит в compiler первой вертикали.",
            status="unsupported",
        )
    title = _optional_localized(inventory, node, "Title")
    if title is not None:
        item["title"] = title
    if _optional_true(inventory, node, "MultiLine"):
        item["multiline"] = True
    if _optional_true(inventory, node, "ReadOnly"):
        item["read_only"] = True
    if _optional_true(inventory, node, "ListChoiceMode"):
        item["list_choice_mode"] = True
    horizontal_stretch = _optional_boolean(inventory, node, "HorizontalStretch")
    if horizontal_stretch is not None:
        item["horizontal_stretch"] = horizontal_stretch
    vertical_stretch = _optional_boolean(inventory, node, "VerticalStretch")
    if vertical_stretch is not None:
        item["vertical_stretch"] = vertical_stretch
    choices = _choice_list(inventory, node)
    if choices:
        item["choice_list"] = choices
    _companion(inventory, node, "ContextMenu")
    _companion(inventory, node, "ExtendedTooltip")
    return item


def _check_box_field(
    inventory: _Inventory, node: ET.Element
) -> dict[str, object]:
    inventory.mark(node, "name", "id")
    item: dict[str, object] = {
        "kind": "check_box_field",
        "name": _attribute_value(inventory, node, "name"),
    }
    _subset_attribute(inventory, node, "id")
    data_path = inventory.required_child(node, "DataPath")
    if data_path is not None:
        inventory.mark(data_path)
    item["data_path"] = _text(data_path)
    if not _DATA_PATH.fullmatch(_text(data_path)):
        inventory.issue(
            "unsupported_data_path",
            (
                inventory.paths[id(data_path)]
                if data_path is not None
                else inventory.paths[id(node)]
            ),
            "DataPath сохранён в inventory, но не входит в compiler первой вертикали.",
            status="unsupported",
        )
    title = _optional_localized(inventory, node, "Title")
    if title is not None:
        item["title"] = title
    if _optional_true(inventory, node, "ReadOnly"):
        item["read_only"] = True
    _service_node(inventory, node, "CheckBoxType", "Auto")
    _companion(inventory, node, "ContextMenu")
    _companion(inventory, node, "ExtendedTooltip")
    return item


def _label_decoration(
    inventory: _Inventory, node: ET.Element
) -> dict[str, object]:
    inventory.mark(node, "name", "id")
    item: dict[str, object] = {
        "kind": "label_decoration",
        "name": _attribute_value(inventory, node, "name"),
    }
    _subset_attribute(inventory, node, "id")
    horizontal_stretch = _optional_boolean(inventory, node, "HorizontalStretch")
    if horizontal_stretch is not None:
        item["horizontal_stretch"] = horizontal_stretch
    vertical_stretch = _optional_boolean(inventory, node, "VerticalStretch")
    if vertical_stretch is not None:
        item["vertical_stretch"] = vertical_stretch
    title = _optional_localized(inventory, node, "Title")
    if title is not None:
        item["title"] = title
    if _optional_true(inventory, node, "Hyperlink"):
        item["hyperlink"] = True
    _companion(inventory, node, "ContextMenu")
    _companion(inventory, node, "ExtendedTooltip")
    return item


def _label_field(inventory: _Inventory, node: ET.Element) -> dict[str, object]:
    inventory.mark(node, "name", "id")
    item: dict[str, object] = {
        "kind": "label_field",
        "name": _attribute_value(inventory, node, "name"),
    }
    _subset_attribute(inventory, node, "id")
    data_path = inventory.required_child(node, "DataPath")
    if data_path is not None:
        inventory.mark(data_path)
    item["data_path"] = _text(data_path)
    if not _DATA_PATH.fullmatch(_text(data_path)):
        inventory.issue(
            "unsupported_data_path",
            (
                inventory.paths[id(data_path)]
                if data_path is not None
                else inventory.paths[id(node)]
            ),
            "DataPath сохранён в inventory, но не входит в compiler первой вертикали.",
            status="unsupported",
        )
    title = _optional_localized(inventory, node, "Title")
    if title is not None:
        item["title"] = title
    if _optional_true(inventory, node, "Hiperlink"):
        item["hyperlink"] = True
    if _optional_true(inventory, node, "ReadOnly"):
        item["read_only"] = True
    horizontal_stretch = _optional_boolean(inventory, node, "HorizontalStretch")
    if horizontal_stretch is not None:
        item["horizontal_stretch"] = horizontal_stretch
    vertical_stretch = _optional_boolean(inventory, node, "VerticalStretch")
    if vertical_stretch is not None:
        item["vertical_stretch"] = vertical_stretch
    _companion(inventory, node, "ContextMenu")
    _companion(inventory, node, "ExtendedTooltip")
    return item


def _radio_button_field(
    inventory: _Inventory, node: ET.Element
) -> dict[str, object]:
    inventory.mark(node, "name", "id")
    item: dict[str, object] = {
        "kind": "radio_button_field",
        "name": _attribute_value(inventory, node, "name"),
    }
    _subset_attribute(inventory, node, "id")
    data_path = inventory.required_child(node, "DataPath")
    if data_path is not None:
        inventory.mark(data_path)
    item["data_path"] = _text(data_path)
    title = _optional_localized(inventory, node, "Title")
    if title is not None:
        item["title"] = title
    if _optional_true(inventory, node, "ReadOnly"):
        item["read_only"] = True
    type_node = inventory.required_child(node, "RadioButtonType")
    if type_node is not None:
        inventory.mark(type_node)
    radio_type = {
        "Auto": "auto",
        "Tumbler": "tumbler",
        "RadioButtons": "radio_buttons",
    }.get(_text(type_node))
    if radio_type is None:
        inventory.issue(
            "unsupported_xml_value",
            (
                inventory.paths[id(type_node)]
                if type_node is not None
                else inventory.paths[id(node)]
            ),
            "Неподдержанный RadioButtonType.",
            status="unsupported",
        )
    elif radio_type != "auto":
        item["radio_button_type"] = radio_type
    columns = [child for child in node if child.tag == _q("ColumnsCount")]
    if columns:
        inventory.mark(columns[0])
        try:
            item["columns_count"] = int(_text(columns[0]))
        except ValueError:
            inventory.issue(
                "unsupported_xml_value",
                inventory.paths[id(columns[0])],
                "ColumnsCount должен быть целым числом.",
                status="unsupported",
            )
    choices = _choice_list(inventory, node)
    item["choice_list"] = choices
    if len(choices) < 2:
        inventory.issue(
            "missing_or_repeated_xml_node",
            inventory.paths[id(node)] + "/ChoiceList",
            "Ожидались хотя бы два варианта ChoiceList.",
            status="unsupported",
        )
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
    custom_prefix = "Form.Command."
    form_prefix = "Form.StandardCommand."
    item_prefix = "Form.Item."
    if command_name.startswith(custom_prefix):
        item["command"] = command_name.removeprefix(custom_prefix)
    elif command_name.startswith(form_prefix):
        item["command"] = command_name.removeprefix(form_prefix)
        item["command_kind"] = "form_standard"
        if not standard_command_supported("form", str(item["command"])):
            inventory.issue(
                "unsupported_standard_command",
                inventory.paths[id(command)],
                "Стандартная команда формы не входит в закрытый каталог.",
                status="unsupported",
            )
    elif (
        command_name.startswith(item_prefix)
        and ".StandardCommand." in command_name
    ):
        owner, command_value = command_name.removeprefix(item_prefix).rsplit(
            ".StandardCommand.", 1
        )
        item["command"] = command_value
        item["command_kind"] = "item_standard"
        item["command_owner"] = owner
        if not any(
            command_value in commands
            for commands in ITEM_STANDARD_COMMANDS.values()
        ):
            inventory.issue(
                "unsupported_standard_command",
                inventory.paths[id(command)],
                "Стандартная команда элемента не входит в закрытый каталог.",
                status="unsupported",
            )
    else:
        inventory.issue(
            "unsupported_command_path",
            (
                inventory.paths[id(command)]
                if command is not None
                else inventory.paths[id(node)]
            ),
            "Ссылка команды не входит в поддержанное подмножество.",
            status="unsupported",
        )
        item["command"] = command_name
    title = _optional_localized(inventory, node, "Title")
    if title is not None:
        item["title"] = title
    _companion(inventory, node, "ExtendedTooltip")
    return item


def _command_bar(
    inventory: _Inventory, node: ET.Element
) -> dict[str, object]:
    inventory.mark(node, "name", "id")
    result: dict[str, object] = {
        "kind": "command_bar",
        "name": _attribute_value(inventory, node, "name"),
    }
    _subset_attribute(inventory, node, "id")
    title = _optional_localized(inventory, node, "Title")
    if title is not None:
        result["title"] = title
    locations = [
        child for child in node if child.tag == _q("HorizontalLocation")
    ]
    if locations:
        location_node = locations[0]
        inventory.mark(location_node)
        location = {
            "Auto": "auto",
            "Left": "left",
            "Center": "center",
            "Right": "right",
        }.get(_text(location_node))
        if len(locations) > 1 or location is None:
            inventory.issue(
                "unsupported_xml_value",
                inventory.paths[id(location_node)],
                "Неподдержанное горизонтальное положение CommandBar.",
                status="unsupported",
            )
        elif location != "auto":
            result["horizontal_location"] = location
    horizontal_stretch = _optional_boolean(inventory, node, "HorizontalStretch")
    if horizontal_stretch is not None:
        result["horizontal_stretch"] = horizontal_stretch
    vertical_stretch = _optional_boolean(inventory, node, "VerticalStretch")
    if vertical_stretch is not None:
        result["vertical_stretch"] = vertical_stretch
    _companion(inventory, node, "ExtendedTooltip")
    container = inventory.required_child(node, "ChildItems")
    children: list[dict[str, object]] = []
    if container is not None:
        inventory.mark(container)
        for child in container:
            if child.tag == _q("Button"):
                children.append(_button(inventory, child))
    if not children:
        inventory.issue(
            "command_bar_buttons_required",
            (
                inventory.paths[id(container)]
                if container is not None
                else inventory.paths[id(node)] + "/ChildItems"
            ),
            "Первый слой CommandBar требует хотя бы одну прямую кнопку.",
            status="unsupported",
        )
    result["children"] = children
    return result


def _children(
    inventory: _Inventory, parent: ET.Element
) -> list[dict[str, object]]:
    container = inventory.required_child(parent, "ChildItems")
    if container is None:
        return []
    inventory.mark(container)
    result: list[dict[str, object]] = []
    for node in container:
        item = _element(inventory, node)
        if item is not None:
            result.append(item)
    return result


def _group(inventory: _Inventory, node: ET.Element) -> dict[str, object]:
    inventory.mark(node, "name", "id")
    result: dict[str, object] = {
        "kind": "usual_group",
        "name": _attribute_value(inventory, node, "name"),
        "title": _localized(inventory, node, "Title"),
    }
    _subset_attribute(inventory, node, "id")
    group = inventory.required_child(node, "Group")
    if group is not None:
        inventory.mark(group)
    orientation = {
        "Vertical": "vertical",
        "Horizontal": "horizontal",
        "AlwaysHorizontal": "always_horizontal",
    }.get(_text(group))
    if orientation is None:
        inventory.issue(
            "unsupported_xml_value",
            inventory.paths[id(group)] if group is not None else inventory.paths[id(node)],
            "Неподдержанная ориентация UsualGroup.",
            status="unsupported",
        )
    elif orientation != "vertical":
        result["orientation"] = orientation
    _service_node(inventory, node, "Behavior", "Usual")
    representation_node = inventory.required_child(node, "Representation")
    if representation_node is not None:
        inventory.mark(representation_node)
    representation = {
        "None": "none",
        "NormalSeparation": "normal_separation",
        "StrongSeparation": "strong_separation",
    }.get(_text(representation_node))
    if representation is None:
        inventory.issue(
            "unsupported_xml_value",
            (
                inventory.paths[id(representation_node)]
                if representation_node is not None
                else inventory.paths[id(node)]
            ),
            "Неподдержанное представление UsualGroup.",
            status="unsupported",
        )
    elif representation != "normal_separation":
        result["representation"] = representation
    show_title = inventory.required_child(node, "ShowTitle")
    if show_title is not None:
        inventory.mark(show_title)
    if _text(show_title) not in {"true", "false"}:
        inventory.issue(
            "unsupported_xml_value",
            (
                inventory.paths[id(show_title)]
                if show_title is not None
                else inventory.paths[id(node)]
            ),
            "ShowTitle должен быть true либо false.",
            status="unsupported",
        )
    elif _text(show_title) == "false":
        result["show_title"] = False
    horizontal_stretch = _optional_boolean(inventory, node, "HorizontalStretch")
    if horizontal_stretch is not None:
        result["horizontal_stretch"] = horizontal_stretch
    vertical_stretch = _optional_boolean(inventory, node, "VerticalStretch")
    if vertical_stretch is not None:
        result["vertical_stretch"] = vertical_stretch
    _companion(inventory, node, "ExtendedTooltip")
    result["children"] = _children(inventory, node)
    return result


def _page(inventory: _Inventory, node: ET.Element) -> dict[str, object]:
    inventory.mark(node, "name", "id")
    result: dict[str, object] = {
        "name": _attribute_value(inventory, node, "name"),
        "title": _localized(inventory, node, "Title"),
    }
    _subset_attribute(inventory, node, "id")
    _companion(inventory, node, "ExtendedTooltip")
    result["children"] = _children(inventory, node)
    return result


def _pages(inventory: _Inventory, node: ET.Element) -> dict[str, object]:
    inventory.mark(node, "name", "id")
    result: dict[str, object] = {
        "kind": "pages",
        "name": _attribute_value(inventory, node, "name"),
        "title": _localized(inventory, node, "Title"),
        "representation": "tabs_on_top",
    }
    _subset_attribute(inventory, node, "id")
    _service_node(inventory, node, "PagesRepresentation", "TabsOnTop")
    horizontal_stretch = _optional_boolean(inventory, node, "HorizontalStretch")
    if horizontal_stretch is not None:
        result["horizontal_stretch"] = horizontal_stretch
    vertical_stretch = _optional_boolean(inventory, node, "VerticalStretch")
    if vertical_stretch is not None:
        result["vertical_stretch"] = vertical_stretch
    _companion(inventory, node, "ExtendedTooltip")
    container = inventory.required_child(node, "ChildItems")
    pages: list[dict[str, object]] = []
    if container is not None:
        inventory.mark(container)
        for child in container:
            if child.tag == _q("Page"):
                pages.append(_page(inventory, child))
    result["pages"] = pages
    return result


def _addition(
    inventory: _Inventory,
    node: ET.Element,
    *,
    expected_type: str,
    expected_item: str,
) -> None:
    inventory.mark(node, "name", "id")
    _subset_attribute(inventory, node, "name")
    _subset_attribute(inventory, node, "id")
    source = inventory.required_child(node, "AdditionSource")
    if source is not None:
        inventory.mark(source)
        item = inventory.required_child(source, "Item")
        type_node = inventory.required_child(source, "Type")
        if item is not None:
            inventory.mark(item)
            if _text(item) != expected_item:
                inventory.issue(
                    "unsupported_addition_source",
                    inventory.paths[id(item)],
                    "AdditionSource ссылается не на свою таблицу.",
                    status="unsupported",
                )
        if type_node is not None:
            inventory.mark(type_node)
            if _text(type_node) != expected_type:
                inventory.issue(
                    "unsupported_addition_type",
                    inventory.paths[id(type_node)],
                    "Неподдержанный тип AdditionSource.",
                    status="unsupported",
                )
    _companion(inventory, node, "ContextMenu")
    _companion(inventory, node, "ExtendedTooltip")


def _table(inventory: _Inventory, node: ET.Element) -> dict[str, object]:
    inventory.mark(node, "name", "id")
    name = _attribute_value(inventory, node, "name")
    result: dict[str, object] = {
        "kind": "table",
        "name": name,
    }
    _subset_attribute(inventory, node, "id")
    _service_node(inventory, node, "Representation", "List")
    if _optional_true(inventory, node, "ReadOnly"):
        result["read_only"] = True
    horizontal_stretch = _optional_boolean(inventory, node, "HorizontalStretch")
    if horizontal_stretch is not None:
        result["horizontal_stretch"] = horizontal_stretch
    vertical_stretch = _optional_boolean(inventory, node, "VerticalStretch")
    if vertical_stretch is not None:
        result["vertical_stretch"] = vertical_stretch
    data_path = inventory.required_child(node, "DataPath")
    if data_path is not None:
        inventory.mark(data_path)
    result["data_path"] = _text(data_path)
    title = _optional_localized(inventory, node, "Title")
    if title is not None:
        result["title"] = title
    _companion(inventory, node, "ContextMenu")
    _companion(inventory, node, "AutoCommandBar")
    _companion(inventory, node, "ExtendedTooltip")
    additions = (
        ("SearchStringAddition", "SearchStringRepresentation"),
        ("ViewStatusAddition", "ViewStatusRepresentation"),
        ("SearchControlAddition", "SearchControl"),
    )
    for tag, expected_type in additions:
        addition = inventory.required_child(node, tag)
        if addition is not None:
            _addition(
                inventory,
                addition,
                expected_type=expected_type,
                expected_item=name,
            )
    container = inventory.required_child(node, "ChildItems")
    columns: list[dict[str, object]] = []
    if container is not None:
        inventory.mark(container)
        for child in container:
            if child.tag == _q("InputField"):
                columns.append(_input_field(inventory, child))
            elif child.tag == _q("LabelField"):
                columns.append(_label_field(inventory, child))
    result["columns"] = columns
    return result


def _element(
    inventory: _Inventory, node: ET.Element
) -> dict[str, object] | None:
    if node.tag == _q("InputField"):
        return _input_field(inventory, node)
    if node.tag == _q("CheckBoxField"):
        return _check_box_field(inventory, node)
    if node.tag == _q("LabelDecoration"):
        return _label_decoration(inventory, node)
    if node.tag == _q("LabelField"):
        return _label_field(inventory, node)
    if node.tag == _q("RadioButtonField"):
        return _radio_button_field(inventory, node)
    if node.tag == _q("Button"):
        return _button(inventory, node)
    if node.tag == _q("CommandBar"):
        return _command_bar(inventory, node)
    if node.tag == _q("UsualGroup"):
        return _group(inventory, node)
    if node.tag == _q("Pages"):
        return _pages(inventory, node)
    if node.tag == _q("Table"):
        return _table(inventory, node)
    return None


def _elements(inventory: _Inventory, root: ET.Element) -> list[dict[str, object]]:
    container = inventory.optional_container(root, "ChildItems")
    if container is None:
        return []
    result: list[dict[str, object]] = []
    for node in container:
        item = _element(inventory, node)
        if item is not None:
            result.append(item)
    return result


def _integer_text(
    inventory: _Inventory,
    parent: ET.Element,
    local: str,
    *,
    namespace: str = _V8,
) -> int:
    node = inventory.required_child(parent, local, namespace=namespace)
    if node is None:
        return 0
    inventory.mark(node)
    try:
        return int(_text(node))
    except ValueError:
        inventory.issue(
            "invalid_integer",
            inventory.paths[id(node)],
            f"{local} должен быть целым числом.",
            status="failed",
        )
        return 0


def _type(
    inventory: _Inventory,
    parent: ET.Element,
    *,
    allow_value_table: bool,
) -> dict[str, object] | None:
    container = inventory.required_child(parent, "Type")
    if container is None:
        inventory.issue(
            "attribute_type_not_representable",
            f"{inventory.paths[id(parent)]}/Type",
            "Тип реквизита отсутствует или задан вне поддержанного Type.",
            status="unsupported",
        )
        return None
    inventory.mark(container)
    type_names = [node for node in container if node.tag == _q("Type", _V8)]
    if not type_names:
        inventory.issue(
            "missing_or_repeated_xml_node",
            f"{inventory.paths[id(container)]}/Type",
            "Ожидался хотя бы один XML-узел Type.",
            status="unsupported",
        )
        return None
    for type_name in type_names:
        inventory.mark(type_name)
    if len(type_names) > 1:
        variants = [
            _atomic_type(inventory, container, type_name)
            for type_name in type_names
        ]
        if any(variant is None for variant in variants):
            return None
        return {"kind": "composite", "variants": variants}

    type_name = type_names[0]
    name = _text(type_name)
    if name == "v8:ValueTable" and allow_value_table:
        columns_node = inventory.required_child(parent, "Columns")
        columns: list[dict[str, object]] = []
        if columns_node is not None:
            inventory.mark(columns_node)
            for column in columns_node:
                if column.tag != _q("Column"):
                    continue
                inventory.mark(column, "name", "id")
                item: dict[str, object] = {
                    "name": _attribute_value(inventory, column, "name"),
                }
                _subset_attribute(inventory, column, "id")
                title = _optional_localized(inventory, column, "Title")
                if title is not None:
                    item["title"] = title
                column_type = _type(inventory, column, allow_value_table=False)
                if column_type is not None:
                    item["type"] = column_type
                columns.append(item)
        return {"kind": "value_table", "columns": columns}
    metadata_object = metadata_object_registry_ref(name)
    if metadata_object is not None and allow_value_table:
        return {"kind": "metadata_object", "object": metadata_object}
    if name == "cfg:DynamicList" and allow_value_table:
        return _dynamic_list_type(inventory, parent)
    return _atomic_type(inventory, container, type_name)


def _dynamic_list_type(
    inventory: _Inventory, parent: ET.Element
) -> dict[str, object] | None:
    settings = inventory.required_child(parent, "Settings")
    if settings is None:
        return None
    xsi_type = _q("type", _XSI)
    inventory.mark(settings, xsi_type)
    if settings.get(xsi_type) != "DynamicList":
        inventory.issue(
            "unsupported_dynamic_list_settings",
            inventory.paths[id(settings)],
            "Ожидались Settings типа DynamicList.",
            status="unsupported",
        )
    manual = inventory.required_child(settings, "ManualQuery")
    if manual is not None:
        inventory.mark(manual)
        if _text(manual) != "false":
            inventory.issue(
                "manual_dynamic_list_not_supported",
                inventory.paths[id(manual)],
                "Ручной запрос динамического списка пока сохраняется "
                "только в inventory.",
                status="unsupported",
            )
            return None
    dynamic_data_read = _optional_boolean(
        inventory, settings, "DynamicDataRead"
    )
    if dynamic_data_read is None:
        inventory.issue(
            "missing_dynamic_data_read",
            f"{inventory.paths[id(settings)]}/DynamicDataRead",
            "Для поддержанного динамического списка нужен DynamicDataRead.",
            status="unsupported",
        )
        return None
    main_table_node = inventory.required_child(settings, "MainTable")
    if main_table_node is None:
        return None
    inventory.mark(main_table_node)
    main_table = dynamic_list_registry_ref(_text(main_table_node))
    if main_table is None:
        inventory.issue(
            "unsupported_dynamic_list_main_table",
            inventory.paths[id(main_table_node)],
            "Поддерживается прямая MainTable из закрытого каталога Registry.",
            status="unsupported",
        )
        return None
    if not _default_list_settings(inventory, settings):
        return None
    return {
        "kind": "dynamic_list",
        "main_table": main_table,
        "dynamic_data_read": dynamic_data_read,
    }


def _default_list_settings(
    inventory: _Inventory, settings: ET.Element
) -> bool:
    container = inventory.required_child(settings, "ListSettings")
    if container is None:
        return False
    inventory.mark(container)
    expected = (
        ("filter", "dfcece9d-5077-440b-b6b3-45a5cb4538eb"),
        ("order", "88619765-ccb3-46c6-ac52-38e9c992ebd4"),
        ("conditionalAppearance", "b75fecce-942b-4aed-abc9-e6a02e460fb3"),
    )
    supported = True
    for tag, setting_id in expected:
        node = inventory.required_child(container, tag, namespace=_DCSSET)
        if node is None:
            supported = False
            continue
        inventory.mark(node)
        view = inventory.required_child(node, "viewMode", namespace=_DCSSET)
        identifier = inventory.required_child(
            node, "userSettingID", namespace=_DCSSET
        )
        if view is not None:
            inventory.mark(view)
        if identifier is not None:
            inventory.mark(identifier)
        if _text(view) != "Normal" or _text(identifier) != setting_id:
            supported = False
    items_view = inventory.required_child(
        container, "itemsViewMode", namespace=_DCSSET
    )
    items_id = inventory.required_child(
        container, "itemsUserSettingID", namespace=_DCSSET
    )
    if items_view is not None:
        inventory.mark(items_view)
    if items_id is not None:
        inventory.mark(items_id)
    if _text(items_view) != "Normal" or _text(items_id) != (
        "911b6018-f537-43e8-a417-da56b22f9aec"
    ):
        supported = False
    if not supported:
        inventory.issue(
            "nondefault_dynamic_list_settings",
            inventory.paths[id(container)],
            "Нестандартные настройки динамического списка сохранены "
            "только в inventory.",
            status="unsupported",
        )
    return supported


def _atomic_type(
    inventory: _Inventory,
    container: ET.Element,
    type_name: ET.Element,
) -> dict[str, object] | None:
    name = _text(type_name)
    if name == "xs:string":
        qualifiers = inventory.required_child(
            container, "StringQualifiers", namespace=_V8
        )
        if qualifiers is None:
            return None
        inventory.mark(qualifiers)
        length = _integer_text(inventory, qualifiers, "Length")
        allowed = inventory.required_child(
            qualifiers, "AllowedLength", namespace=_V8
        )
        if allowed is not None:
            inventory.mark(allowed)
            if _text(allowed) != "Variable":
                inventory.issue(
                    "unsupported_allowed_length",
                    inventory.paths[id(allowed)],
                    "Поддерживается переменная длина строки.",
                    status="unsupported",
                )
        return {"kind": "string", "length": length}
    if name == "xs:boolean":
        return {"kind": "boolean"}
    if name == "xs:decimal":
        qualifiers = inventory.required_child(
            container, "NumberQualifiers", namespace=_V8
        )
        if qualifiers is None:
            return None
        inventory.mark(qualifiers)
        digits = _integer_text(inventory, qualifiers, "Digits")
        fractions = _integer_text(inventory, qualifiers, "FractionDigits")
        sign = inventory.required_child(
            qualifiers, "AllowedSign", namespace=_V8
        )
        if sign is not None:
            inventory.mark(sign)
        sign_value = {
            "Any": "any",
            "Nonnegative": "nonnegative",
        }.get(_text(sign))
        if sign_value is None:
            inventory.issue(
                "unsupported_allowed_sign",
                (
                    inventory.paths[id(sign)]
                    if sign is not None
                    else inventory.paths[id(qualifiers)]
                ),
                "Неподдержанное ограничение знака числа.",
                status="unsupported",
            )
            sign_value = "any"
        return {
            "kind": "number",
            "digits": digits,
            "fraction_digits": fractions,
            "allowed_sign": sign_value,
        }
    if name == "xs:dateTime":
        qualifiers = inventory.required_child(
            container, "DateQualifiers", namespace=_V8
        )
        if qualifiers is None:
            return None
        inventory.mark(qualifiers)
        fractions = inventory.required_child(
            qualifiers, "DateFractions", namespace=_V8
        )
        if fractions is not None:
            inventory.mark(fractions)
        value = {"Date": "date", "DateTime": "date_time"}.get(
            _text(fractions)
        )
        if value is None:
            inventory.issue(
                "unsupported_date_fractions",
                (
                    inventory.paths[id(fractions)]
                    if fractions is not None
                    else inventory.paths[id(qualifiers)]
                ),
                "Неподдержанная точность даты.",
                status="unsupported",
            )
            value = "date_time"
        return {"kind": "date", "fractions": value}
    metadata_reference = metadata_reference_registry_ref(name)
    if metadata_reference is not None:
        return {"kind": "metadata_reference", "object": metadata_reference}
    inventory.issue(
        "unsupported_attribute_type",
        inventory.paths[id(type_name)],
        f"Тип {name or '<пусто>'} сохранён только в inventory.",
        status="unsupported",
    )
    return None


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
        value_type = _type(inventory, node, allow_value_table=True)
        if value_type is not None:
            item["type"] = value_type
        if _optional_true(inventory, node, "MainAttribute"):
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


def _events_for_owner(
    inventory: _Inventory,
    node: ET.Element,
    *,
    owner: str | None,
    owner_kind: str,
    event_profile: str,
) -> list[dict[str, object]]:
    container = inventory.optional_container(node, "Events")
    if container is None:
        return []
    result: list[dict[str, object]] = []
    for event_node in container:
        if event_node.tag != _q("Event"):
            continue
        inventory.mark(event_node, "name")
        event_name = _attribute_value(inventory, event_node, "name")
        if event_signature(
            owner_kind,
            event_name,
            profile=event_profile,
        ) is None:
            inventory.issue(
                "unsupported_event",
                f"{inventory.paths[id(event_node)]}/@name",
                f"Событие {event_name or '<пусто>'} сохранено только в inventory.",
                status="unsupported",
            )
        result.append(
            {
                **({"owner": owner} if owner is not None else {}),
                "event": event_name,
                "handler": _text(event_node),
            }
        )
    return result


def _events(
    inventory: _Inventory,
    root: ET.Element,
    *,
    event_profile: str,
) -> list[dict[str, object]]:
    result = _events_for_owner(
        inventory,
        root,
        owner=None,
        owner_kind="form",
        event_profile=event_profile,
    )
    owner_kinds = {
        _q("InputField"): "input_field",
        _q("CheckBoxField"): "check_box_field",
        _q("LabelDecoration"): "label_decoration",
        _q("LabelField"): "label_field",
        _q("RadioButtonField"): "radio_button_field",
        _q("Pages"): "pages",
        _q("Table"): "table",
    }
    for node in root.iter():
        owner_kind = owner_kinds.get(node.tag)
        if owner_kind is None:
            continue
        result.extend(
            _events_for_owner(
                inventory,
                node,
                owner=_attribute_value(inventory, node, "name"),
                owner_kind=owner_kind,
                event_profile=event_profile,
            )
        )
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
    platform_version: object | None = None,
) -> FormsResult:
    """Разобрать Form.xml без записи и без молчаливой потери неизвестных узлов."""

    safe_module = module_bsl if isinstance(module_bsl, str) else None
    safe_platform_version: str | None = None
    profile = platform_profile(None)
    if platform_version is not None:
        if not isinstance(platform_version, str) or (
            normalized_platform_version(platform_version) is None
        ):
            return _rejected(
                "invalid_platform_version",
                "$platform_version",
                "Версия платформы должна иметь вид 8.3.23 или 8.3.23.1997.",
                xml_status="not_checked",
                module_bsl=safe_module,
            )
        safe_platform_version = platform_version
        profile = platform_profile(platform_version)
    assert profile is not None
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
        **(
            {"platform_version": safe_platform_version}
            if safe_platform_version is not None
            else {}
        ),
        "title": title,
        "attributes": _attributes(inventory, root),
        "elements": _elements(inventory, root),
        "commands": _commands(inventory, root),
        "events": _events(
            inventory,
            root,
            event_profile=profile.event_profile,
        ),
    }
    inventory.report_uncovered(root)

    collections = (
        specification["attributes"],
        specification["elements"],
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
    platform_note = platform_compatibility_note(safe_platform_version)
    if platform_note is not None:
        diagnostics.append(
            Diagnostic(
                "platform_import",
                "warning",
                platform_note.code,
                "$platform_version",
                platform_note.message,
            )
        )
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
