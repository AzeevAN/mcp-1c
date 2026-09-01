"""Доказанный декларативный срез XML-форм 1С.

Один parser используют и постоянный индекс форм, и converter source B.
Позиционные контейнеры ``Form.bin``/``.Form`` сюда не входят: их bounded-
проверка живёт в ``form_reader``, а семантика остаётся отдельной задачей.
"""

from __future__ import annotations

import xml.etree.ElementTree as ET
from dataclasses import dataclass


_NON_FORM_ELEMENTS = frozenset({"Events", "ExtendedTooltip", "ContextMenu"})


class FormStructureError(ValueError):
    """XML не соответствует доказанному контракту формы."""


@dataclass(frozen=True, slots=True)
class ParsedFormDescriptor:
    uuid: str | None
    name: str | None
    synonym: str | None
    form_type: str | None


@dataclass(frozen=True, slots=True)
class ParsedFormEvent:
    handler: str
    event: str
    element: str | None


@dataclass(frozen=True, slots=True)
class ParsedFormStructure:
    attributes: tuple[str, ...]
    elements: tuple[str, ...]
    events: tuple[ParsedFormEvent, ...]


def local_name(element: ET.Element) -> str:
    return element.tag.rsplit("}", 1)[-1]


def direct_child(element: ET.Element, name: str) -> ET.Element | None:
    return next((item for item in element if local_name(item) == name), None)


def parse_form_descriptor(root: ET.Element) -> ParsedFormDescriptor:
    """Прочитать подписанные UUID, имя, синоним и вид формы."""
    node = root
    if local_name(root) == "MetaDataObject":
        node = next(
            (
                item
                for item in root
                if local_name(item) in {"Form", "CommonForm"}
            ),
            root,
        )
    properties = direct_child(node, "Properties")

    def text(name: str) -> str | None:
        if properties is None:
            return None
        item = direct_child(properties, name)
        value = "" if item is None else (item.text or "").strip()
        return value or None

    synonym: str | None = None
    if properties is not None:
        synonym_node = direct_child(properties, "Synonym")
        if synonym_node is not None:
            synonym = next(
                (
                    (item.text or "").strip()
                    for item in synonym_node.iter()
                    if local_name(item) == "content" and (item.text or "").strip()
                ),
                None,
            )
    uuid = next(
        (
            value.strip()
            for name, value in node.attrib.items()
            if name.rsplit("}", 1)[-1].lower() == "uuid" and value.strip()
        ),
        None,
    )
    return ParsedFormDescriptor(uuid, text("Name"), synonym, text("FormType"))


def parse_form_xml(root: ET.Element) -> ParsedFormStructure:
    """Извлечь только подписанный декларативный срез ``Form.xml``.

    Реквизиты берутся только из прямых ``Attributes/Attribute``:
    вложенные колонки табличных частей не становятся отдельными
    реквизитами. ``ExtendedTooltip`` и ``ContextMenu`` не попадают
    в список элементов, но события из их служебных поддеревьев
    сохраняют ближайшего настоящего владельца.
    """
    if local_name(root) != "Form":
        raise FormStructureError("корень XML-структуры формы должен быть Form")

    attributes_node = direct_child(root, "Attributes")
    attributes = tuple(
        item.get("name", "")
        for item in (attributes_node if attributes_node is not None else ())
        if local_name(item) == "Attribute" and item.get("name") is not None
    )

    elements: list[str] = []
    events: list[ParsedFormEvent] = []

    def collect_events(node: ET.Element, owner: str | None) -> None:
        for event in node:
            if local_name(event) != "Event":
                continue
            handler = (event.text or "").strip()
            event_name = event.get("name", "").strip()
            if handler and event_name:
                events.append(ParsedFormEvent(handler, event_name, owner))

    def walk(
        node: ET.Element,
        owner: str | None,
        collect_element_names: bool = True,
    ) -> None:
        for item in node:
            kind = local_name(item)
            if kind in {"ExtendedTooltip", "ContextMenu"}:
                # Служебные поддеревья не являются пользовательскими
                # элементами, но их события всё ещё относятся к владельцу.
                walk(item, owner, False)
                continue
            if kind == "Events":
                collect_events(item, owner)
                continue
            current_owner = item.get("name") or owner
            if (
                collect_element_names
                and kind not in _NON_FORM_ELEMENTS
                and item.get("name") is not None
            ):
                elements.append(item.get("name", ""))
            walk(item, current_owner, collect_element_names)

    for item in root:
        kind = local_name(item)
        if kind == "Events":
            collect_events(item, None)
        elif kind == "ChildItems":
            walk(item, None)
    return ParsedFormStructure(tuple(attributes), tuple(elements), tuple(events))


__all__ = [
    "FormStructureError",
    "ParsedFormDescriptor",
    "ParsedFormEvent",
    "ParsedFormStructure",
    "direct_child",
    "local_name",
    "parse_form_descriptor",
    "parse_form_xml",
]
