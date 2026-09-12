"""Закрытая спецификация первой вертикали управляемой формы.

`TypedDict` формирует точную MCP JSON Schema, frozen dataclass используется
предметным кодом. Ручная проверка нужна и вне MCP-транспорта: compiler и
checker должны одинаково отклонять неоднозначный вход.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Literal, NotRequired, TypeAlias, TypedDict

from .diagnostics import Diagnostic


SPECIFICATION_VERSION = 1
SUPPORTED_FORMAT_VERSION = "2.16"
_IDENTIFIER = re.compile(r"[A-Za-zА-Яа-яЁё_][A-Za-zА-Яа-яЁё0-9_]*\Z")


class LocalizedTextSpec(TypedDict):
    ru: str


class StringTypeSpec(TypedDict):
    kind: Literal["string"]
    length: int


class FormAttributeSpec(TypedDict):
    name: str
    type: StringTypeSpec
    title: NotRequired[LocalizedTextSpec]
    main: NotRequired[bool]


class InputFieldSpec(TypedDict):
    kind: Literal["input_field"]
    name: str
    data_path: str
    title: NotRequired[LocalizedTextSpec]


class ButtonSpec(TypedDict):
    kind: Literal["button"]
    name: str
    command: str
    default: NotRequired[bool]
    title: NotRequired[LocalizedTextSpec]


GroupChildSpec: TypeAlias = InputFieldSpec | ButtonSpec


class UsualGroupSpec(TypedDict):
    kind: Literal["usual_group"]
    name: str
    title: LocalizedTextSpec
    children: list[GroupChildSpec]


class FormCommandSpec(TypedDict):
    name: str
    title: LocalizedTextSpec
    action: str


class FormEventSpec(TypedDict):
    event: Literal["OnCreateAtServer"]
    handler: str


class ManagedFormSpec(TypedDict):
    schema_version: Literal[1]
    form_name: str
    format_version: Literal["2.16"]
    title: LocalizedTextSpec
    attributes: list[FormAttributeSpec]
    elements: list[UsualGroupSpec]
    commands: list[FormCommandSpec]
    events: list[FormEventSpec]


@dataclass(frozen=True, slots=True)
class LocalizedText:
    ru: str


@dataclass(frozen=True, slots=True)
class StringType:
    length: int


@dataclass(frozen=True, slots=True)
class FormAttribute:
    name: str
    type: StringType
    title: LocalizedText | None = None
    main: bool = False


@dataclass(frozen=True, slots=True)
class InputField:
    name: str
    data_path: str
    title: LocalizedText | None = None


@dataclass(frozen=True, slots=True)
class Button:
    name: str
    command: str
    default: bool = False
    title: LocalizedText | None = None


GroupChild: TypeAlias = InputField | Button


@dataclass(frozen=True, slots=True)
class UsualGroup:
    name: str
    title: LocalizedText
    children: tuple[GroupChild, ...]


@dataclass(frozen=True, slots=True)
class FormCommand:
    name: str
    title: LocalizedText
    action: str


@dataclass(frozen=True, slots=True)
class FormEvent:
    event: Literal["OnCreateAtServer"]
    handler: str


@dataclass(frozen=True, slots=True)
class ManagedForm:
    schema_version: Literal[1]
    form_name: str
    format_version: Literal["2.16"]
    title: LocalizedText
    attributes: tuple[FormAttribute, ...]
    elements: tuple[UsualGroup, ...]
    commands: tuple[FormCommand, ...]
    events: tuple[FormEvent, ...]


class FormsContractError(ValueError):
    """Спецификация не входит в доказанное подмножество первой вертикали."""

    def __init__(self, diagnostics: tuple[Diagnostic, ...]) -> None:
        self.diagnostics = diagnostics
        super().__init__(
            "спецификация формы отклонена: "
            + "; ".join(item.message for item in diagnostics)
        )


class _Reader:
    def __init__(self) -> None:
        self.diagnostics: list[Diagnostic] = []

    def issue(self, code: str, path: str, message: str) -> None:
        self.diagnostics.append(
            Diagnostic(
                level="structural",
                status="failed",
                code=code,
                path=path,
                message=message,
            )
        )

    def object(self, value: object, path: str) -> Mapping[str, object]:
        if not isinstance(value, Mapping):
            self.issue("invalid_type", path, "Ожидался объект.")
            return {}
        return value

    def exact_keys(
        self,
        value: Mapping[str, object],
        path: str,
        *,
        required: frozenset[str],
        optional: frozenset[str] = frozenset(),
    ) -> None:
        for key in sorted(required.difference(value)):
            self.issue("missing_key", f"{path}.{key}", "Обязательное поле отсутствует.")
        unknown = [key for key in value if key not in required | optional]
        for key in sorted(unknown, key=lambda item: repr(item)):
            key_path = key if isinstance(key, str) else repr(key)
            self.issue(
                "unknown_key",
                f"{path}.{key_path}",
                "Неизвестное поле запрещено.",
            )

    def string(self, value: object, path: str, *, identifier: bool = False) -> str:
        if not isinstance(value, str) or not value.strip():
            self.issue("invalid_string", path, "Ожидалась непустая строка.")
            return ""
        if value != value.strip():
            self.issue("surrounding_whitespace", path, "Пробелы по краям запрещены.")
        if identifier and not _IDENTIFIER.fullmatch(value):
            self.issue("invalid_identifier", path, "Недопустимый идентификатор 1С.")
        return value

    def boolean(self, value: object, path: str) -> bool:
        if type(value) is not bool:
            self.issue("invalid_type", path, "Ожидалось логическое значение.")
            return False
        return value

    def array(self, value: object, path: str) -> list[object]:
        if not isinstance(value, list):
            self.issue("invalid_type", path, "Ожидался массив.")
            return []
        if not value:
            self.issue("empty_collection", path, "Пустой массив не поддержан.")
        return value


def _localized(reader: _Reader, value: object, path: str) -> LocalizedText:
    item = reader.object(value, path)
    reader.exact_keys(item, path, required=frozenset({"ru"}))
    return LocalizedText(reader.string(item.get("ru"), f"{path}.ru"))


def _optional_localized(
    reader: _Reader, item: Mapping[str, object], path: str
) -> LocalizedText | None:
    if "title" not in item:
        return None
    return _localized(reader, item.get("title"), f"{path}.title")


def _string_type(reader: _Reader, value: object, path: str) -> StringType:
    item = reader.object(value, path)
    reader.exact_keys(item, path, required=frozenset({"kind", "length"}))
    if item.get("kind") != "string":
        reader.issue(
            "unsupported_attribute_type",
            f"{path}.kind",
            "Первая вертикаль поддерживает только string.",
        )
    length = item.get("length")
    if type(length) is not int or length <= 0:
        reader.issue(
            "invalid_string_length",
            f"{path}.length",
            "Длина строки должна быть положительным целым числом.",
        )
        return StringType(1)
    return StringType(length)


def _attribute(reader: _Reader, value: object, path: str) -> FormAttribute:
    item = reader.object(value, path)
    reader.exact_keys(
        item,
        path,
        required=frozenset({"name", "type"}),
        optional=frozenset({"title", "main"}),
    )
    return FormAttribute(
        name=reader.string(item.get("name"), f"{path}.name", identifier=True),
        type=_string_type(reader, item.get("type"), f"{path}.type"),
        title=_optional_localized(reader, item, path),
        main=(
            reader.boolean(item["main"], f"{path}.main")
            if "main" in item
            else False
        ),
    )


def _input_field(reader: _Reader, item: Mapping[str, object], path: str) -> InputField:
    reader.exact_keys(
        item,
        path,
        required=frozenset({"kind", "name", "data_path"}),
        optional=frozenset({"title"}),
    )
    return InputField(
        name=reader.string(item.get("name"), f"{path}.name", identifier=True),
        data_path=reader.string(
            item.get("data_path"), f"{path}.data_path", identifier=True
        ),
        title=_optional_localized(reader, item, path),
    )


def _button(reader: _Reader, item: Mapping[str, object], path: str) -> Button:
    reader.exact_keys(
        item,
        path,
        required=frozenset({"kind", "name", "command"}),
        optional=frozenset({"default", "title"}),
    )
    return Button(
        name=reader.string(item.get("name"), f"{path}.name", identifier=True),
        command=reader.string(
            item.get("command"), f"{path}.command", identifier=True
        ),
        default=(
            reader.boolean(item["default"], f"{path}.default")
            if "default" in item
            else False
        ),
        title=_optional_localized(reader, item, path),
    )


def _group(reader: _Reader, value: object, path: str) -> UsualGroup:
    item = reader.object(value, path)
    reader.exact_keys(
        item,
        path,
        required=frozenset({"kind", "name", "title", "children"}),
    )
    kind = item.get("kind")
    if kind != "usual_group":
        reader.issue(
            "unsupported_element_kind",
            f"{path}.kind",
            "На верхнем уровне поддержан только usual_group.",
        )
    children: list[GroupChild] = []
    for index, raw_child in enumerate(
        reader.array(item.get("children"), f"{path}.children")
    ):
        child_path = f"{path}.children[{index}]"
        child = reader.object(raw_child, child_path)
        child_kind = child.get("kind")
        if child_kind == "input_field":
            children.append(_input_field(reader, child, child_path))
        elif child_kind == "button":
            children.append(_button(reader, child, child_path))
        else:
            reader.issue(
                "unsupported_element_kind",
                f"{child_path}.kind",
                "В группе поддержаны только input_field и button.",
            )
    return UsualGroup(
        name=reader.string(item.get("name"), f"{path}.name", identifier=True),
        title=_localized(reader, item.get("title"), f"{path}.title"),
        children=tuple(children),
    )


def _command(reader: _Reader, value: object, path: str) -> FormCommand:
    item = reader.object(value, path)
    reader.exact_keys(
        item,
        path,
        required=frozenset({"name", "title", "action"}),
    )
    return FormCommand(
        name=reader.string(item.get("name"), f"{path}.name", identifier=True),
        title=_localized(reader, item.get("title"), f"{path}.title"),
        action=reader.string(item.get("action"), f"{path}.action", identifier=True),
    )


def _event(reader: _Reader, value: object, path: str) -> FormEvent:
    item = reader.object(value, path)
    reader.exact_keys(item, path, required=frozenset({"event", "handler"}))
    event = item.get("event")
    if event != "OnCreateAtServer":
        reader.issue(
            "unsupported_event",
            f"{path}.event",
            "Первая вертикаль поддерживает только OnCreateAtServer.",
        )
    return FormEvent(
        event="OnCreateAtServer",
        handler=reader.string(
            item.get("handler"), f"{path}.handler", identifier=True
        ),
    )


def _duplicates(
    reader: _Reader,
    values: list[tuple[str, str]],
    *,
    code: str,
    message: str,
) -> None:
    seen: set[str] = set()
    for value, path in values:
        if value and value in seen:
            reader.issue(code, path, message)
        seen.add(value)


def parse_managed_form_spec(payload: object) -> ManagedForm:
    """Проверить и нормализовать спецификацию доказанной первой вертикали."""

    reader = _Reader()
    root = reader.object(payload, "$")
    reader.exact_keys(
        root,
        "$",
        required=frozenset(
            {
                "schema_version",
                "form_name",
                "format_version",
                "title",
                "attributes",
                "elements",
                "commands",
                "events",
            }
        ),
    )

    if type(root.get("schema_version")) is not int or root.get(
        "schema_version"
    ) != SPECIFICATION_VERSION:
        reader.issue(
            "unsupported_schema_version",
            "$.schema_version",
            "Поддерживается только schema_version=1.",
        )
    if root.get("format_version") != SUPPORTED_FORMAT_VERSION:
        reader.issue(
            "unsupported_format_version",
            "$.format_version",
            "Compiler первой вертикали поддерживает только формат 2.16.",
        )

    form_name = reader.string(root.get("form_name"), "$.form_name", identifier=True)
    title = _localized(reader, root.get("title"), "$.title")

    attributes = tuple(
        _attribute(reader, value, f"$.attributes[{index}]")
        for index, value in enumerate(reader.array(root.get("attributes"), "$.attributes"))
    )
    elements = tuple(
        _group(reader, value, f"$.elements[{index}]")
        for index, value in enumerate(reader.array(root.get("elements"), "$.elements"))
    )
    commands = tuple(
        _command(reader, value, f"$.commands[{index}]")
        for index, value in enumerate(reader.array(root.get("commands"), "$.commands"))
    )
    events = tuple(
        _event(reader, value, f"$.events[{index}]")
        for index, value in enumerate(reader.array(root.get("events"), "$.events"))
    )

    _duplicates(
        reader,
        [(item.name, f"$.attributes[{index}].name") for index, item in enumerate(attributes)],
        code="duplicate_attribute_name",
        message="Имя реквизита повторяется.",
    )
    _duplicates(
        reader,
        [(item.name, f"$.commands[{index}].name") for index, item in enumerate(commands)],
        code="duplicate_command_name",
        message="Имя команды повторяется.",
    )
    element_names: list[tuple[str, str]] = []
    for group_index, group in enumerate(elements):
        element_names.append((group.name, f"$.elements[{group_index}].name"))
        element_names.extend(
            (
                child.name,
                f"$.elements[{group_index}].children[{child_index}].name",
            )
            for child_index, child in enumerate(group.children)
        )
    _duplicates(
        reader,
        element_names,
        code="duplicate_element_name",
        message="Имя элемента повторяется.",
    )

    if sum(item.main for item in attributes) > 1:
        reader.issue(
            "multiple_main_attributes",
            "$.attributes",
            "Главным может быть не более одного реквизита.",
        )

    attribute_names = {item.name for item in attributes}
    command_names = {item.name for item in commands}
    for group_index, group in enumerate(elements):
        for child_index, child in enumerate(group.children):
            child_path = f"$.elements[{group_index}].children[{child_index}]"
            if isinstance(child, InputField) and child.data_path not in attribute_names:
                reader.issue(
                    "unresolved_data_path",
                    f"{child_path}.data_path",
                    "DataPath не разрешается в реквизит формы.",
                )
            if isinstance(child, Button) and child.command not in command_names:
                reader.issue(
                    "unresolved_command",
                    f"{child_path}.command",
                    "Кнопка ссылается на неизвестную команду.",
                )

    event_handlers = {item.handler for item in events}
    for index, command in enumerate(commands):
        if command.action in event_handlers:
            reader.issue(
                "handler_signature_conflict",
                f"$.commands[{index}].action",
                "Один обработчик нельзя сгенерировать с сигнатурами события и команды.",
            )

    _duplicates(
        reader,
        [(item.event, f"$.events[{index}].event") for index, item in enumerate(events)],
        code="duplicate_event",
        message="Событие формы повторяется.",
    )

    if reader.diagnostics:
        raise FormsContractError(tuple(reader.diagnostics))

    return ManagedForm(
        schema_version=1,
        form_name=form_name,
        format_version="2.16",
        title=title,
        attributes=attributes,
        elements=elements,
        commands=commands,
        events=events,
    )


def managed_form_to_spec(form: ManagedForm) -> dict[str, object]:
    """Вернуть каноническую публичную спецификацию без внутренних ID."""

    attributes: list[dict[str, object]] = []
    for attribute in form.attributes:
        item: dict[str, object] = {
            "name": attribute.name,
            "type": {"kind": "string", "length": attribute.type.length},
        }
        if attribute.title is not None:
            item["title"] = {"ru": attribute.title.ru}
        if attribute.main:
            item["main"] = True
        attributes.append(item)

    elements: list[dict[str, object]] = []
    for group in form.elements:
        children: list[dict[str, object]] = []
        for child in group.children:
            if isinstance(child, InputField):
                child_item: dict[str, object] = {
                    "kind": "input_field",
                    "name": child.name,
                    "data_path": child.data_path,
                }
            else:
                child_item = {
                    "kind": "button",
                    "name": child.name,
                    "command": child.command,
                }
                if child.default:
                    child_item["default"] = True
            if child.title is not None:
                child_item["title"] = {"ru": child.title.ru}
            children.append(child_item)
        elements.append(
            {
                "kind": "usual_group",
                "name": group.name,
                "title": {"ru": group.title.ru},
                "children": children,
            }
        )

    return {
        "schema_version": form.schema_version,
        "form_name": form.form_name,
        "format_version": form.format_version,
        "title": {"ru": form.title.ru},
        "attributes": attributes,
        "elements": elements,
        "commands": [
            {
                "name": command.name,
                "title": {"ru": command.title.ru},
                "action": command.action,
            }
            for command in form.commands
        ],
        "events": [
            {"event": event.event, "handler": event.handler} for event in form.events
        ],
    }


__all__ = [
    "Button",
    "ButtonSpec",
    "FormAttribute",
    "FormAttributeSpec",
    "FormCommand",
    "FormCommandSpec",
    "FormEvent",
    "FormEventSpec",
    "FormsContractError",
    "GroupChild",
    "GroupChildSpec",
    "InputField",
    "InputFieldSpec",
    "LocalizedText",
    "LocalizedTextSpec",
    "ManagedForm",
    "ManagedFormSpec",
    "SPECIFICATION_VERSION",
    "SUPPORTED_FORMAT_VERSION",
    "StringType",
    "StringTypeSpec",
    "UsualGroup",
    "UsualGroupSpec",
    "managed_form_to_spec",
    "parse_managed_form_spec",
]
