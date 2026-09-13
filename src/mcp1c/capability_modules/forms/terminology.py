"""Двуязычный поисковый индекс публичной спецификации Forms."""

from __future__ import annotations

import copy
import re
from dataclasses import dataclass
from typing import Literal, TypeAlias


TermCategory: TypeAlias = Literal["element", "property", "attribute_type"]
MAX_TERM_QUERY_LENGTH = 160
_SEPARATORS = re.compile(r"[_\-]+")
_CAMEL_BOUNDARY = re.compile(r"(?<=[a-z0-9])(?=[A-Z])")
_SPACES = re.compile(r"\s+")


@dataclass(frozen=True, slots=True)
class FormTerm:
    canonical: str
    category: TermCategory
    russian: tuple[str, ...]
    english: tuple[str, ...]
    scopes: tuple[str, ...] = ()
    values: dict[str, tuple[str, ...]] | None = None

    def to_dict(self, *, include_scopes: bool = True) -> dict[str, object]:
        result: dict[str, object] = {
            "canonical": self.canonical,
            "category": self.category,
            "russian": list(self.russian),
            "english": list(self.english),
        }
        if include_scopes and self.scopes:
            result["scopes"] = list(self.scopes)
        if self.values is not None:
            result["values"] = {
                name: list(aliases) for name, aliases in self.values.items()
            }
        return result


def _element(canonical: str, russian: tuple[str, ...], english: str) -> FormTerm:
    return FormTerm(canonical, "element", russian, (english,))


def _property(
    canonical: str,
    russian: tuple[str, ...],
    english: str,
    scopes: tuple[str, ...],
    values: dict[str, tuple[str, ...]] | None = None,
) -> FormTerm:
    return FormTerm(canonical, "property", russian, (english,), scopes, values)


ELEMENT_TERMS: tuple[FormTerm, ...] = (
    _element("usual_group", ("обычная группа", "группа"), "usual group"),
    _element("input_field", ("поле ввода",), "input field"),
    _element("check_box_field", ("поле флажка", "флажок"), "check box field"),
    _element(
        "label_decoration",
        ("декорация-надпись", "статическая надпись"),
        "label decoration",
    ),
    _element("label_field", ("поле надписи", "связанная надпись"), "label field"),
    _element(
        "radio_button_field",
        ("поле переключателя", "переключатель"),
        "radio button field",
    ),
    _element("button", ("кнопка",), "button"),
    _element("command_bar", ("панель команд", "командная панель"), "command bar"),
    _element("popup", ("подменю", "всплывающее меню"), "popup"),
    _element("button_group", ("группа кнопок",), "button group"),
    _element("pages", ("страницы", "группа страниц"), "pages"),
    _element("table", ("таблица", "поле таблицы"), "table"),
)


ATTRIBUTE_TYPE_TERMS: tuple[FormTerm, ...] = (
    FormTerm("string", "attribute_type", ("строка",), ("string",)),
    FormTerm("boolean", "attribute_type", ("булево", "логический тип"), ("boolean",)),
    FormTerm("number", "attribute_type", ("число",), ("number",)),
    FormTerm("date", "attribute_type", ("дата",), ("date",)),
    FormTerm(
        "metadata_reference",
        "attribute_type",
        ("ссылка на объект метаданных", "ссылка метаданных"),
        ("metadata reference",),
    ),
    FormTerm("composite", "attribute_type", ("составной тип",), ("composite",)),
    FormTerm("value_table", "attribute_type", ("таблица значений",), ("value table",)),
    FormTerm(
        "metadata_object",
        "attribute_type",
        ("объект метаданных",),
        ("metadata object",),
    ),
    FormTerm("dynamic_list", "attribute_type", ("динамический список",), ("dynamic list",)),
)


PROPERTY_TERMS: tuple[FormTerm, ...] = (
    _property("schema_version", ("версия схемы",), "schema version", ("form",)),
    _property("form_name", ("имя формы",), "form name", ("form",)),
    _property("format_version", ("версия формата",), "format version", ("form",)),
    _property("platform_version", ("версия платформы",), "platform version", ("form",)),
    _property("title", ("заголовок",), "title", ("form", "attribute", "element", "command")),
    _property("attributes", ("реквизиты", "реквизиты формы"), "attributes", ("form",)),
    _property("elements", ("элементы", "элементы формы"), "elements", ("form",)),
    _property("commands", ("команды", "команды формы"), "commands", ("form",)),
    _property("events", ("события", "события формы"), "events", ("form",)),
    _property("ru", ("русский текст", "русская локализация"), "russian text", ("localized_text",)),
    _property("kind", ("вид", "канонический вид"), "kind", ("type", "element")),
    _property("length", ("длина",), "length", ("string",)),
    _property("digits", ("разрядность", "количество цифр"), "digits", ("number",)),
    _property(
        "fraction_digits",
        ("разрядность дробной части", "знаков после запятой"),
        "fraction digits",
        ("number",),
    ),
    _property(
        "allowed_sign",
        ("допустимый знак",),
        "allowed sign",
        ("number",),
        {"any": ("любой", "any"), "nonnegative": ("неотрицательный", "nonnegative")},
    ),
    _property(
        "fractions",
        ("состав даты", "части даты"),
        "date fractions",
        ("date",),
        {"date": ("дата", "date"), "date_time": ("дата и время", "date time")},
    ),
    _property("object", ("объект метаданных",), "metadata object", ("metadata_reference", "metadata_object")),
    _property("variants", ("варианты типа",), "type variants", ("composite",)),
    _property("columns", ("колонки",), "columns", ("value_table", "table")),
    _property("type", ("тип", "тип значения"), "type", ("attribute", "value_table_column")),
    _property("main_table", ("основная таблица",), "main table", ("dynamic_list",)),
    _property(
        "dynamic_data_read",
        ("динамическое считывание данных",),
        "dynamic data read",
        ("dynamic_list",),
    ),
    _property("name", ("имя",), "name", ("attribute", "element", "command")),
    _property("main", ("главный реквизит", "основной реквизит"), "main attribute", ("attribute",)),
    _property("value", ("значение",), "value", ("choice_list_item",)),
    _property("presentation", ("представление значения",), "value presentation", ("choice_list_item",)),
    _property("data_path", ("путь к данным",), "data path", ("input_field", "check_box_field", "label_field", "radio_button_field", "table")),
    _property("multiline", ("многострочный режим", "многострочное поле"), "multiline", ("input_field",)),
    _property("read_only", ("только чтение",), "read only", ("input_field", "check_box_field", "label_field", "radio_button_field", "table")),
    _property("list_choice_mode", ("режим выбора из списка",), "list choice mode", ("input_field",)),
    _property("choice_list", ("список выбора",), "choice list", ("input_field", "radio_button_field")),
    _property("horizontal_stretch", ("растягивать по горизонтали", "горизонтальное растяжение"), "horizontal stretch", ("element",)),
    _property("vertical_stretch", ("растягивать по вертикали", "вертикальное растяжение"), "vertical stretch", ("element",)),
    _property("hyperlink", ("гиперссылка",), "hyperlink", ("label_decoration", "label_field")),
    _property(
        "radio_button_type",
        ("вид переключателя",),
        "radio button type",
        ("radio_button_field",),
        {
            "auto": ("автоматически", "auto"),
            "tumbler": ("тумблер", "tumbler"),
            "radio_buttons": ("переключатели", "radio buttons"),
        },
    ),
    _property("columns_count", ("количество колонок",), "columns count", ("radio_button_field",)),
    _property("command", ("команда кнопки",), "button command", ("button",)),
    _property(
        "command_kind",
        ("вид команды",),
        "command kind",
        ("button",),
        {
            "custom": ("пользовательская", "custom"),
            "form_standard": ("стандартная команда формы", "form standard"),
            "item_standard": ("стандартная команда элемента", "item standard"),
        },
    ),
    _property("command_owner", ("владелец команды",), "command owner", ("button",)),
    _property("default", ("кнопка по умолчанию",), "default button", ("button",)),
    _property("children", ("дочерние элементы",), "children", ("container",)),
    _property(
        "representation",
        ("представление", "вариант отображения"),
        "representation",
        ("usual_group", "pages", "button_group"),
        {
            "none": ("без оформления", "none"),
            "normal_separation": ("обычное выделение", "normal separation"),
            "strong_separation": ("сильное выделение", "strong separation"),
            "tabs_on_top": ("закладки сверху", "tabs on top"),
            "usual": ("обычное", "usual"),
            "compact": ("компактное", "compact"),
        },
    ),
    _property(
        "horizontal_location",
        ("положение по горизонтали", "горизонтальное положение"),
        "horizontal location",
        ("command_bar",),
        {
            "auto": ("автоматически", "auto"),
            "left": ("слева", "left"),
            "center": ("по центру", "center"),
            "right": ("справа", "right"),
        },
    ),
    _property("pages", ("страницы группы",), "page items", ("pages",)),
    _property(
        "orientation",
        ("ориентация",),
        "orientation",
        ("usual_group",),
        {
            "vertical": ("вертикально", "vertical"),
            "horizontal": ("горизонтально", "horizontal"),
            "always_horizontal": ("всегда горизонтально", "always horizontal"),
        },
    ),
    _property("show_title", ("показывать заголовок",), "show title", ("usual_group",)),
    _property("action", ("действие команды", "обработчик команды"), "command action", ("command",)),
    _property("owner", ("владелец события",), "event owner", ("event",)),
    _property("event", ("событие",), "event", ("event",)),
    _property("handler", ("обработчик события",), "event handler", ("event",)),
)


FORM_TERMS: tuple[FormTerm, ...] = ELEMENT_TERMS + ATTRIBUTE_TYPE_TERMS + PROPERTY_TERMS


def normalize_term(value: str) -> str:
    """Нормализовать русский, английский и snake_case одинаковым образом."""

    separated = _CAMEL_BOUNDARY.sub(" ", value)
    normalized = _SEPARATORS.sub(" ", separated.casefold().replace("ё", "е"))
    return _SPACES.sub(" ", normalized).strip()


def _aliases(term: FormTerm) -> tuple[str, ...]:
    aliases = (term.canonical, *term.russian, *term.english)
    if term.values is not None:
        aliases += tuple(
            alias
            for canonical, values in term.values.items()
            for alias in (canonical, *values)
        )
    return aliases


def _matched_values(term: FormTerm, normalized_query: str) -> list[str]:
    if term.values is None:
        return []
    result: list[str] = []
    for canonical, aliases in term.values.items():
        candidates = (canonical, *aliases)
        if any(normalize_term(value) == normalized_query for value in candidates):
            result.append(canonical)
    return result


def search_form_terms(query: str) -> list[dict[str, object]]:
    """Найти термин без стемминга и недоказанных догадок."""

    normalized_query = normalize_term(query)
    exact = [
        term
        for term in FORM_TERMS
        if any(normalize_term(alias) == normalized_query for alias in _aliases(term))
    ]
    candidates = exact
    if not candidates:
        query_tokens = set(normalized_query.split())
        candidates = [
            term
            for term in FORM_TERMS
            if query_tokens
            and query_tokens.issubset(
                {
                    token
                    for alias in _aliases(term)
                    for token in normalize_term(alias).split()
                }
            )
        ]

    result: list[dict[str, object]] = []
    for term in candidates:
        item = term.to_dict()
        matched_values = _matched_values(term, normalized_query)
        if matched_values:
            item["matched_values"] = matched_values
        result.append(copy.deepcopy(item))
    return result


def all_form_terms() -> dict[str, dict[str, dict[str, object]]]:
    """Вернуть независимую копию полного двуязычного индекса."""

    # Категория и canonical вынесены в ключи, чтобы полный каталог оставался
    # компактным. Точная область свойства возвращается в целевом поиске.
    result: dict[str, dict[str, dict[str, object]]] = {
        "element": {},
        "property": {},
        "attribute_type": {},
    }
    for term in FORM_TERMS:
        item = term.to_dict(include_scopes=False)
        del item["canonical"]
        del item["category"]
        result[term.category][term.canonical] = item
    return result


__all__ = [
    "FORM_TERMS",
    "MAX_TERM_QUERY_LENGTH",
    "FormTerm",
    "TermCategory",
    "all_form_terms",
    "normalize_term",
    "search_form_terms",
]
