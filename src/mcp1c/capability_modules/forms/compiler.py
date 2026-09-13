"""Детерминированный compiler доказанного подмножества Form.xml 2.16."""

from __future__ import annotations

from dataclasses import dataclass
from xml.etree import ElementTree as ET
from xml.sax.saxutils import escape, quoteattr

from .diagnostics import Artifact, Coverage, Diagnostic, FormsResult
from .event_catalog import event_signature
from .metadata_types import (
    dynamic_list_xml_table,
    metadata_object_xml_type,
    metadata_reference_xml_type,
)
from .models import (
    BooleanType,
    Button,
    CheckBoxField,
    CompositeType,
    DateType,
    DynamicListType,
    FormAttribute,
    FormEvent,
    InputField,
    LabelDecoration,
    LabelField,
    LocalizedText,
    ManagedForm,
    MetadataObjectType,
    MetadataReferenceType,
    NumberType,
    Page,
    Pages,
    RadioButtonField,
    StringType,
    Table,
    UsualGroup,
    ValueTableColumn,
    ValueTableType,
    managed_form_to_spec,
    parse_managed_form_spec,
)
from .version_catalog import platform_compatibility_note


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


def _emit_events(
    lines: list[str], events: tuple[FormEvent, ...], indent: int
) -> None:
    if not events:
        return
    _append(lines, indent, "<Events>")
    for event in events:
        _append(
            lines,
            indent + 1,
            f"<Event name={quoteattr(event.event)}>{escape(event.handler)}</Event>",
        )
    _append(lines, indent, "</Events>")


def _root_opening(form: ManagedForm) -> str:
    declarations = " ".join(
        f"xmlns{':' + prefix if prefix else ''}={quoteattr(uri)}"
        for prefix, uri in _NAMESPACES
    )
    return f"<Form {declarations} version={quoteattr(form.format_version)}>"


def _emit_input(
    lines: list[str],
    item: InputField,
    allocator: _IdAllocator,
    events_by_owner: dict[str | None, tuple[FormEvent, ...]],
    indent: int,
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
    if item.horizontal_stretch is not None:
        _append(
            lines,
            indent + 1,
            f"<HorizontalStretch>{str(item.horizontal_stretch).lower()}</HorizontalStretch>",
        )
    if item.vertical_stretch is not None:
        _append(
            lines,
            indent + 1,
            f"<VerticalStretch>{str(item.vertical_stretch).lower()}</VerticalStretch>",
        )
    if item.multiline:
        _append(lines, indent + 1, "<MultiLine>true</MultiLine>")
    if item.read_only:
        _append(lines, indent + 1, "<ReadOnly>true</ReadOnly>")
    if item.list_choice_mode:
        _append(lines, indent + 1, "<ListChoiceMode>true</ListChoiceMode>")
    if item.choice_list:
        _append(lines, indent + 1, "<ChoiceList>")
        for choice in item.choice_list:
            _append(lines, indent + 2, "<xr:Item>")
            _append(lines, indent + 3, "<xr:Presentation/>")
            _append(lines, indent + 3, "<xr:CheckState>0</xr:CheckState>")
            _append(
                lines,
                indent + 3,
                '<xr:Value xsi:type="FormChoiceListDesTimeValue">',
            )
            _localized(lines, "Presentation", choice.presentation, indent + 4)
            _append(
                lines,
                indent + 4,
                f'<Value xsi:type="xs:string">{escape(choice.value)}</Value>',
            )
            _append(lines, indent + 3, "</xr:Value>")
            _append(lines, indent + 2, "</xr:Item>")
        _append(lines, indent + 1, "</ChoiceList>")
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
    _emit_events(lines, events_by_owner.get(item.name, ()), indent + 1)
    _append(lines, indent, "</InputField>")


def _emit_check_box(
    lines: list[str],
    item: CheckBoxField,
    allocator: _IdAllocator,
    events_by_owner: dict[str | None, tuple[FormEvent, ...]],
    indent: int,
) -> None:
    element_id = allocator.next()
    _append(
        lines,
        indent,
        f"<CheckBoxField name={quoteattr(item.name)} id={quoteattr(element_id)}>",
    )
    _append(lines, indent + 1, f"<DataPath>{escape(item.data_path)}</DataPath>")
    if item.title is not None:
        _localized(lines, "Title", item.title, indent + 1)
    if item.read_only:
        _append(lines, indent + 1, "<ReadOnly>true</ReadOnly>")
    _append(lines, indent + 1, "<CheckBoxType>Auto</CheckBoxType>")
    context_id = allocator.next()
    _append(
        lines,
        indent + 1,
        f"<ContextMenu name={quoteattr(item.name + 'КонтекстноеМеню')} "
        f"id={quoteattr(context_id)}/>",
    )
    tooltip_id = allocator.next()
    _append(
        lines,
        indent + 1,
        f"<ExtendedTooltip name={quoteattr(item.name + 'РасширеннаяПодсказка')} "
        f"id={quoteattr(tooltip_id)}/>",
    )
    _emit_events(lines, events_by_owner.get(item.name, ()), indent + 1)
    _append(lines, indent, "</CheckBoxField>")


def _emit_label_decoration(
    lines: list[str],
    item: LabelDecoration,
    allocator: _IdAllocator,
    events_by_owner: dict[str | None, tuple[FormEvent, ...]],
    indent: int,
) -> None:
    element_id = allocator.next()
    _append(
        lines,
        indent,
        f"<LabelDecoration name={quoteattr(item.name)} id={quoteattr(element_id)}>",
    )
    if item.horizontal_stretch is not None:
        _append(
            lines,
            indent + 1,
            f"<HorizontalStretch>{str(item.horizontal_stretch).lower()}</HorizontalStretch>",
        )
    if item.vertical_stretch is not None:
        _append(
            lines,
            indent + 1,
            f"<VerticalStretch>{str(item.vertical_stretch).lower()}</VerticalStretch>",
        )
    if item.title is not None:
        _localized(lines, "Title", item.title, indent + 1)
    if item.hyperlink:
        _append(lines, indent + 1, "<Hyperlink>true</Hyperlink>")
    context_id = allocator.next()
    _append(
        lines,
        indent + 1,
        f"<ContextMenu name={quoteattr(item.name + 'КонтекстноеМеню')} "
        f"id={quoteattr(context_id)}/>",
    )
    tooltip_id = allocator.next()
    _append(
        lines,
        indent + 1,
        f"<ExtendedTooltip name={quoteattr(item.name + 'РасширеннаяПодсказка')} "
        f"id={quoteattr(tooltip_id)}/>",
    )
    _emit_events(lines, events_by_owner.get(item.name, ()), indent + 1)
    _append(lines, indent, "</LabelDecoration>")


def _emit_label_field(
    lines: list[str],
    item: LabelField,
    allocator: _IdAllocator,
    events_by_owner: dict[str | None, tuple[FormEvent, ...]],
    indent: int,
) -> None:
    element_id = allocator.next()
    _append(
        lines,
        indent,
        f"<LabelField name={quoteattr(item.name)} id={quoteattr(element_id)}>",
    )
    _append(lines, indent + 1, f"<DataPath>{escape(item.data_path)}</DataPath>")
    if item.title is not None:
        _localized(lines, "Title", item.title, indent + 1)
    if item.horizontal_stretch is not None:
        _append(
            lines,
            indent + 1,
            f"<HorizontalStretch>{str(item.horizontal_stretch).lower()}</HorizontalStretch>",
        )
    if item.vertical_stretch is not None:
        _append(
            lines,
            indent + 1,
            f"<VerticalStretch>{str(item.vertical_stretch).lower()}</VerticalStretch>",
        )
    if item.hyperlink:
        _append(lines, indent + 1, "<Hiperlink>true</Hiperlink>")
    if item.read_only:
        _append(lines, indent + 1, "<ReadOnly>true</ReadOnly>")
    context_id = allocator.next()
    _append(
        lines,
        indent + 1,
        f"<ContextMenu name={quoteattr(item.name + 'КонтекстноеМеню')} "
        f"id={quoteattr(context_id)}/>",
    )
    tooltip_id = allocator.next()
    _append(
        lines,
        indent + 1,
        f"<ExtendedTooltip name={quoteattr(item.name + 'РасширеннаяПодсказка')} "
        f"id={quoteattr(tooltip_id)}/>",
    )
    _emit_events(lines, events_by_owner.get(item.name, ()), indent + 1)
    _append(lines, indent, "</LabelField>")


def _emit_radio_button_field(
    lines: list[str],
    item: RadioButtonField,
    allocator: _IdAllocator,
    events_by_owner: dict[str | None, tuple[FormEvent, ...]],
    indent: int,
) -> None:
    element_id = allocator.next()
    _append(
        lines,
        indent,
        f"<RadioButtonField name={quoteattr(item.name)} "
        f"id={quoteattr(element_id)}>",
    )
    _append(lines, indent + 1, f"<DataPath>{escape(item.data_path)}</DataPath>")
    if item.title is not None:
        _localized(lines, "Title", item.title, indent + 1)
    if item.read_only:
        _append(lines, indent + 1, "<ReadOnly>true</ReadOnly>")
    radio_types = {
        "auto": "Auto",
        "tumbler": "Tumbler",
        "radio_buttons": "RadioButtons",
    }
    _append(
        lines,
        indent + 1,
        f"<RadioButtonType>{radio_types[item.radio_button_type]}</RadioButtonType>",
    )
    if item.columns_count is not None:
        _append(
            lines,
            indent + 1,
            f"<ColumnsCount>{item.columns_count}</ColumnsCount>",
        )
    _append(lines, indent + 1, "<ChoiceList>")
    for choice in item.choice_list:
        _append(lines, indent + 2, "<xr:Item>")
        _append(lines, indent + 3, "<xr:Presentation/>")
        _append(lines, indent + 3, "<xr:CheckState>0</xr:CheckState>")
        _append(
            lines,
            indent + 3,
            '<xr:Value xsi:type="FormChoiceListDesTimeValue">',
        )
        _localized(lines, "Presentation", choice.presentation, indent + 4)
        _append(
            lines,
            indent + 4,
            f'<Value xsi:type="xs:string">{escape(choice.value)}</Value>',
        )
        _append(lines, indent + 3, "</xr:Value>")
        _append(lines, indent + 2, "</xr:Item>")
    _append(lines, indent + 1, "</ChoiceList>")
    for suffix, tag in (
        ("КонтекстноеМеню", "ContextMenu"),
        ("РасширеннаяПодсказка", "ExtendedTooltip"),
    ):
        companion_id = allocator.next()
        _append(
            lines,
            indent + 1,
            f"<{tag} name={quoteattr(item.name + suffix)} "
            f"id={quoteattr(companion_id)}/>",
        )
    _emit_events(lines, events_by_owner.get(item.name, ()), indent + 1)
    _append(lines, indent, "</RadioButtonField>")


def _emit_button(
    lines: list[str],
    item: Button,
    allocator: _IdAllocator,
    events_by_owner: dict[str | None, tuple[FormEvent, ...]],
    indent: int,
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
    if item.command_kind == "custom":
        command_path = f"Form.Command.{item.command}"
    elif item.command_kind == "form_standard":
        command_path = f"Form.StandardCommand.{item.command}"
    else:
        command_path = (
            f"Form.Item.{item.command_owner}.StandardCommand.{item.command}"
        )
    _append(
        lines,
        indent + 1,
        f"<CommandName>{escape(command_path)}</CommandName>",
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


def _emit_group(
    lines: list[str],
    group: UsualGroup,
    allocator: _IdAllocator,
    events_by_owner: dict[str | None, tuple[FormEvent, ...]],
    indent: int,
) -> None:
    group_id = allocator.next()
    _append(
        lines,
        indent,
        f"<UsualGroup name={quoteattr(group.name)} id={quoteattr(group_id)}>",
    )
    _localized(lines, "Title", group.title, indent + 1)
    orientation = {
        "vertical": "Vertical",
        "horizontal": "Horizontal",
        "always_horizontal": "AlwaysHorizontal",
    }[group.orientation]
    _append(lines, indent + 1, f"<Group>{orientation}</Group>")
    _append(lines, indent + 1, "<Behavior>Usual</Behavior>")
    representation = {
        "none": "None",
        "normal_separation": "NormalSeparation",
        "strong_separation": "StrongSeparation",
    }[group.representation]
    _append(lines, indent + 1, f"<Representation>{representation}</Representation>")
    _append(
        lines,
        indent + 1,
        f"<ShowTitle>{str(group.show_title).lower()}</ShowTitle>",
    )
    if group.horizontal_stretch is not None:
        _append(
            lines,
            indent + 1,
            f"<HorizontalStretch>{str(group.horizontal_stretch).lower()}</HorizontalStretch>",
        )
    if group.vertical_stretch is not None:
        _append(
            lines,
            indent + 1,
            f"<VerticalStretch>{str(group.vertical_stretch).lower()}</VerticalStretch>",
        )
    tooltip_id = allocator.next()
    _append(
        lines,
        indent + 1,
        "<ExtendedTooltip "
        f"name={quoteattr(group.name + 'РасширеннаяПодсказка')} "
        f"id={quoteattr(tooltip_id)}/>",
    )
    _append(lines, indent + 1, "<ChildItems>")
    for child in group.children:
        _emit_element(lines, child, allocator, events_by_owner, indent + 2)
    _append(lines, indent + 1, "</ChildItems>")
    _append(lines, indent, "</UsualGroup>")


def _emit_page(
    lines: list[str],
    page: Page,
    allocator: _IdAllocator,
    events_by_owner: dict[str | None, tuple[FormEvent, ...]],
    indent: int,
) -> None:
    page_id = allocator.next()
    _append(
        lines,
        indent,
        f"<Page name={quoteattr(page.name)} id={quoteattr(page_id)}>",
    )
    _localized(lines, "Title", page.title, indent + 1)
    tooltip_id = allocator.next()
    _append(
        lines,
        indent + 1,
        "<ExtendedTooltip "
        f"name={quoteattr(page.name + 'РасширеннаяПодсказка')} "
        f"id={quoteattr(tooltip_id)}/>",
    )
    _append(lines, indent + 1, "<ChildItems>")
    for child in page.children:
        _emit_element(lines, child, allocator, events_by_owner, indent + 2)
    _append(lines, indent + 1, "</ChildItems>")
    _append(lines, indent, "</Page>")


def _emit_pages(
    lines: list[str],
    item: Pages,
    allocator: _IdAllocator,
    events_by_owner: dict[str | None, tuple[FormEvent, ...]],
    indent: int,
) -> None:
    pages_id = allocator.next()
    _append(
        lines,
        indent,
        f"<Pages name={quoteattr(item.name)} id={quoteattr(pages_id)}>",
    )
    _localized(lines, "Title", item.title, indent + 1)
    _append(lines, indent + 1, "<PagesRepresentation>TabsOnTop</PagesRepresentation>")
    if item.horizontal_stretch is not None:
        _append(
            lines,
            indent + 1,
            f"<HorizontalStretch>{str(item.horizontal_stretch).lower()}</HorizontalStretch>",
        )
    if item.vertical_stretch is not None:
        _append(
            lines,
            indent + 1,
            f"<VerticalStretch>{str(item.vertical_stretch).lower()}</VerticalStretch>",
        )
    tooltip_id = allocator.next()
    _append(
        lines,
        indent + 1,
        "<ExtendedTooltip "
        f"name={quoteattr(item.name + 'РасширеннаяПодсказка')} "
        f"id={quoteattr(tooltip_id)}/>",
    )
    _emit_events(lines, events_by_owner.get(item.name, ()), indent + 1)
    _append(lines, indent + 1, "<ChildItems>")
    for page in item.pages:
        _emit_page(lines, page, allocator, events_by_owner, indent + 2)
    _append(lines, indent + 1, "</ChildItems>")
    _append(lines, indent, "</Pages>")


def _emit_addition(
    lines: list[str],
    *,
    tag: str,
    suffix: str,
    source_type: str,
    table: Table,
    allocator: _IdAllocator,
    indent: int,
) -> None:
    name = table.name + suffix
    addition_id = allocator.next()
    _append(
        lines,
        indent,
        f"<{tag} name={quoteattr(name)} id={quoteattr(addition_id)}>",
    )
    _append(lines, indent + 1, "<AdditionSource>")
    _append(lines, indent + 2, f"<Item>{escape(table.name)}</Item>")
    _append(lines, indent + 2, f"<Type>{source_type}</Type>")
    _append(lines, indent + 1, "</AdditionSource>")
    context_id = allocator.next()
    _append(
        lines,
        indent + 1,
        f"<ContextMenu name={quoteattr(name + 'КонтекстноеМеню')} "
        f"id={quoteattr(context_id)}/>",
    )
    tooltip_id = allocator.next()
    _append(
        lines,
        indent + 1,
        f"<ExtendedTooltip name={quoteattr(name + 'РасширеннаяПодсказка')} "
        f"id={quoteattr(tooltip_id)}/>",
    )
    _append(lines, indent, f"</{tag}>")


def _emit_table(
    lines: list[str],
    item: Table,
    allocator: _IdAllocator,
    events_by_owner: dict[str | None, tuple[FormEvent, ...]],
    indent: int,
) -> None:
    table_id = allocator.next()
    _append(
        lines,
        indent,
        f"<Table name={quoteattr(item.name)} id={quoteattr(table_id)}>",
    )
    _append(lines, indent + 1, "<Representation>List</Representation>")
    if item.read_only:
        _append(lines, indent + 1, "<ReadOnly>true</ReadOnly>")
    if item.horizontal_stretch is not None:
        _append(
            lines,
            indent + 1,
            f"<HorizontalStretch>{str(item.horizontal_stretch).lower()}</HorizontalStretch>",
        )
    if item.vertical_stretch is not None:
        _append(
            lines,
            indent + 1,
            f"<VerticalStretch>{str(item.vertical_stretch).lower()}</VerticalStretch>",
        )
    _append(lines, indent + 1, f"<DataPath>{escape(item.data_path)}</DataPath>")
    if item.title is not None:
        _localized(lines, "Title", item.title, indent + 1)
    context_id = allocator.next()
    _append(
        lines,
        indent + 1,
        f"<ContextMenu name={quoteattr(item.name + 'КонтекстноеМеню')} "
        f"id={quoteattr(context_id)}/>",
    )
    command_bar_id = allocator.next()
    _append(
        lines,
        indent + 1,
        f"<AutoCommandBar name={quoteattr(item.name + 'КоманднаяПанель')} "
        f"id={quoteattr(command_bar_id)}/>",
    )
    tooltip_id = allocator.next()
    _append(
        lines,
        indent + 1,
        f"<ExtendedTooltip name={quoteattr(item.name + 'РасширеннаяПодсказка')} "
        f"id={quoteattr(tooltip_id)}/>",
    )
    _emit_addition(
        lines,
        tag="SearchStringAddition",
        suffix="СтрокаПоиска",
        source_type="SearchStringRepresentation",
        table=item,
        allocator=allocator,
        indent=indent + 1,
    )
    _emit_addition(
        lines,
        tag="ViewStatusAddition",
        suffix="СостояниеПросмотра",
        source_type="ViewStatusRepresentation",
        table=item,
        allocator=allocator,
        indent=indent + 1,
    )
    _emit_addition(
        lines,
        tag="SearchControlAddition",
        suffix="УправлениеПоиском",
        source_type="SearchControl",
        table=item,
        allocator=allocator,
        indent=indent + 1,
    )
    _emit_events(lines, events_by_owner.get(item.name, ()), indent + 1)
    _append(lines, indent + 1, "<ChildItems>")
    for column in item.columns:
        _emit_element(lines, column, allocator, events_by_owner, indent + 2)
    _append(lines, indent + 1, "</ChildItems>")
    _append(lines, indent, "</Table>")


def _emit_element(
    lines: list[str],
    item: object,
    allocator: _IdAllocator,
    events_by_owner: dict[str | None, tuple[FormEvent, ...]],
    indent: int,
) -> None:
    if isinstance(item, InputField):
        _emit_input(lines, item, allocator, events_by_owner, indent)
    elif isinstance(item, CheckBoxField):
        _emit_check_box(lines, item, allocator, events_by_owner, indent)
    elif isinstance(item, LabelDecoration):
        _emit_label_decoration(lines, item, allocator, events_by_owner, indent)
    elif isinstance(item, LabelField):
        _emit_label_field(lines, item, allocator, events_by_owner, indent)
    elif isinstance(item, RadioButtonField):
        _emit_radio_button_field(lines, item, allocator, events_by_owner, indent)
    elif isinstance(item, Button):
        _emit_button(lines, item, allocator, events_by_owner, indent)
    elif isinstance(item, Table):
        _emit_table(lines, item, allocator, events_by_owner, indent)
    elif isinstance(item, Pages):
        _emit_pages(lines, item, allocator, events_by_owner, indent)
    elif isinstance(item, UsualGroup):
        _emit_group(lines, item, allocator, events_by_owner, indent)
    else:  # pragma: no cover - typed model does not admit other values
        raise TypeError(f"Неподдержанный элемент: {type(item)!r}")


def _emit_elements(lines: list[str], form: ManagedForm) -> None:
    allocator = _IdAllocator()
    events_by_owner = _events_by_owner(form)
    _append(lines, 1, "<ChildItems>")
    for item in form.elements:
        _emit_element(lines, item, allocator, events_by_owner, 2)
    _append(lines, 1, "</ChildItems>")


def _events_by_owner(
    form: ManagedForm,
) -> dict[str | None, tuple[FormEvent, ...]]:
    result: dict[str | None, list[FormEvent]] = {}
    for event in form.events:
        result.setdefault(event.owner, []).append(event)
    return {owner: tuple(events) for owner, events in result.items()}


def _walk_form_elements(form: ManagedForm):
    def walk(elements):
        for element in elements:
            yield element, "", None
            if isinstance(element, UsualGroup):
                yield from walk(element.children)
            elif isinstance(element, Pages):
                for page in element.pages:
                    yield from walk(page.children)
            elif isinstance(element, Table):
                yield from walk(element.columns)

    yield from walk(form.elements)


def _emit_type(lines: list[str], value: object, indent: int) -> None:
    _append(lines, indent, "<Type>")
    if isinstance(value, CompositeType):
        for variant in value.variants:
            _emit_value_type_name(lines, variant, indent + 1)
        for variant in value.variants:
            _emit_value_qualifiers(lines, variant, indent + 1)
    elif isinstance(value, ValueTableType):
        _append(lines, indent + 1, "<v8:Type>v8:ValueTable</v8:Type>")
    elif isinstance(value, MetadataObjectType):
        xml_type = metadata_object_xml_type(value.object)
        if xml_type is None:  # pragma: no cover - checked by the contract
            raise TypeError(f"Неподдержанный объектный тип: {value.object!r}")
        _append(lines, indent + 1, f"<v8:Type>{escape(xml_type)}</v8:Type>")
    elif isinstance(value, DynamicListType):
        _append(lines, indent + 1, "<v8:Type>cfg:DynamicList</v8:Type>")
    else:
        _emit_value_type_name(lines, value, indent + 1)
        _emit_value_qualifiers(lines, value, indent + 1)
    _append(lines, indent, "</Type>")


def _emit_value_type_name(lines: list[str], value: object, indent: int) -> None:
    if isinstance(value, StringType):
        _append(lines, indent, "<v8:Type>xs:string</v8:Type>")
    elif isinstance(value, BooleanType):
        _append(lines, indent, "<v8:Type>xs:boolean</v8:Type>")
    elif isinstance(value, NumberType):
        _append(lines, indent, "<v8:Type>xs:decimal</v8:Type>")
    elif isinstance(value, DateType):
        _append(lines, indent, "<v8:Type>xs:dateTime</v8:Type>")
    elif isinstance(value, MetadataReferenceType):
        xml_type = metadata_reference_xml_type(value.object)
        if xml_type is None:  # pragma: no cover - checked by the contract
            raise TypeError(f"Неподдержанный ссылочный тип: {value.object!r}")
        _append(lines, indent, f"<v8:Type>{escape(xml_type)}</v8:Type>")
    else:  # pragma: no cover - typed model does not admit other values
        raise TypeError(f"Неподдержанный тип: {type(value)!r}")


def _emit_value_qualifiers(lines: list[str], value: object, indent: int) -> None:
    if isinstance(value, StringType):
        _append(lines, indent, "<v8:StringQualifiers>")
        _append(lines, indent + 1, f"<v8:Length>{value.length}</v8:Length>")
        _append(lines, indent + 1, "<v8:AllowedLength>Variable</v8:AllowedLength>")
        _append(lines, indent, "</v8:StringQualifiers>")
    elif isinstance(value, NumberType):
        _append(lines, indent, "<v8:NumberQualifiers>")
        _append(lines, indent + 1, f"<v8:Digits>{value.digits}</v8:Digits>")
        _append(
            lines,
            indent + 1,
            f"<v8:FractionDigits>{value.fraction_digits}</v8:FractionDigits>",
        )
        sign = "Any" if value.allowed_sign == "any" else "Nonnegative"
        _append(lines, indent + 1, f"<v8:AllowedSign>{sign}</v8:AllowedSign>")
        _append(lines, indent, "</v8:NumberQualifiers>")
    elif isinstance(value, DateType):
        _append(lines, indent, "<v8:DateQualifiers>")
        fractions = "Date" if value.fractions == "date" else "DateTime"
        _append(
            lines,
            indent + 1,
            f"<v8:DateFractions>{fractions}</v8:DateFractions>",
        )
        _append(lines, indent, "</v8:DateQualifiers>")


def _emit_table_column(
    lines: list[str],
    column: ValueTableColumn,
    column_id: int,
    indent: int,
) -> None:
    _append(
        lines,
        indent,
        f"<Column name={quoteattr(column.name)} id={quoteattr(str(column_id))}>",
    )
    if column.title is not None:
        _localized(lines, "Title", column.title, indent + 1)
    _emit_type(lines, column.type, indent + 1)
    _append(lines, indent, "</Column>")


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
    _emit_type(lines, attribute.type, 3)
    if isinstance(attribute.type, ValueTableType):
        _append(lines, 3, "<Columns>")
        for column_id, column in enumerate(attribute.type.columns, 1):
            _emit_table_column(lines, column, column_id, 4)
        _append(lines, 3, "</Columns>")
    if attribute.main:
        _append(lines, 3, "<MainAttribute>true</MainAttribute>")
    if isinstance(attribute.type, DynamicListType):
        _emit_dynamic_list_settings(lines, attribute.type, 3)
    _append(lines, 2, "</Attribute>")


def _emit_dynamic_list_settings(
    lines: list[str], value: DynamicListType, indent: int
) -> None:
    table = dynamic_list_xml_table(value.main_table)
    if table is None:  # pragma: no cover - checked by the contract
        raise TypeError(
            f"Неподдержанная таблица динамического списка: {value.main_table!r}"
        )
    _append(lines, indent, '<Settings xsi:type="DynamicList">')
    _append(lines, indent + 1, "<ManualQuery>false</ManualQuery>")
    dynamic_data_read = "true" if value.dynamic_data_read else "false"
    _append(
        lines,
        indent + 1,
        f"<DynamicDataRead>{dynamic_data_read}</DynamicDataRead>",
    )
    _append(lines, indent + 1, f"<MainTable>{escape(table)}</MainTable>")
    _append(lines, indent + 1, "<ListSettings>")
    for tag, setting_id in (
        ("filter", "dfcece9d-5077-440b-b6b3-45a5cb4538eb"),
        ("order", "88619765-ccb3-46c6-ac52-38e9c992ebd4"),
        ("conditionalAppearance", "b75fecce-942b-4aed-abc9-e6a02e460fb3"),
    ):
        _append(lines, indent + 2, f"<dcsset:{tag}>")
        _append(lines, indent + 3, "<dcsset:viewMode>Normal</dcsset:viewMode>")
        _append(
            lines,
            indent + 3,
            f"<dcsset:userSettingID>{setting_id}</dcsset:userSettingID>",
        )
        _append(lines, indent + 2, f"</dcsset:{tag}>")
    _append(
        lines,
        indent + 2,
        "<dcsset:itemsViewMode>Normal</dcsset:itemsViewMode>",
    )
    _append(
        lines,
        indent + 2,
        "<dcsset:itemsUserSettingID>"
        "911b6018-f537-43e8-a417-da56b22f9aec"
        "</dcsset:itemsUserSettingID>",
    )
    _append(lines, indent + 1, "</ListSettings>")
    _append(lines, indent, "</Settings>")


def _compile_xml(form: ManagedForm) -> str:
    lines = ['<?xml version="1.0" encoding="UTF-8"?>', _root_opening(form)]
    _localized(lines, "Title", form.title, 1)
    _append(lines, 1, '<AutoCommandBar name="ФормаКоманднаяПанель" id="-1"/>')
    _emit_events(lines, _events_by_owner(form).get(None, ()), 1)
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
    emitted: set[str] = set()
    for event in form.events:
        normalized_handler = event.handler.casefold()
        if normalized_handler in emitted:
            continue
        emitted.add(normalized_handler)
        owner_kind = "form"
        if event.owner is not None:
            for element, _path, _table in _walk_form_elements(form):
                if element.name == event.owner:
                    owner_kind = element.kind
                    break
        signature = event_signature(
            owner_kind,
            event.event,
            profile=form.event_profile,
        )
        assert signature is not None
        parameters = ", ".join(signature.parameters)
        lines.extend(
            [
                f"&{signature.directive}",
                f"Процедура {event.handler}({parameters})",
                "",
                "\t// TODO: Реализовать обработчик события формы.",
                "",
                "КонецПроцедуры",
                "",
            ]
        )
    lines.extend(["#КонецОбласти", "", "#Область ОбработчикиКомандФормы", ""])
    for command in form.commands:
        normalized_action = command.action.casefold()
        if normalized_action in emitted:
            continue
        emitted.add(normalized_action)
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
    platform_note = platform_compatibility_note(form.platform_version)
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
        *(
            (
                Diagnostic(
                    "platform_import",
                    "warning",
                    platform_note.code,
                    "$.platform_version",
                    platform_note.message,
                ),
            )
            if platform_note is not None
            else ()
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
