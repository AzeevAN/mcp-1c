"""Ограниченный синтаксический ридер позиционной записи ``form``.

Формат наблюдается как UTF-8-текст со скобочными списками и строками, где
кавычка удваивается. Назначение позиционных полей пока не доказано, поэтому
ридер проверяет грамматику и маркер, но не публикует догадки как элементы,
реквизиты или события формы.
"""

from __future__ import annotations

from dataclasses import dataclass
from types import MappingProxyType
from typing import Mapping


MAX_BYTES = 16 * 1024 * 1024
MAX_DEPTH = 128
MAX_TOKENS = 2_000_000
KNOWN_MARKERS = frozenset({19, 20, 23, 25, 26, 27})


class FormReadError(ValueError):
    """Обезличенный отказ одной записи ``form``."""

    def __init__(self, category: str, reason: str):
        self.category = category
        self.reason = reason
        super().__init__(reason)


@dataclass(frozen=True, slots=True)
class FormReadResult:
    marker: int
    status: str
    category: str
    reason: str
    semantic_fields: Mapping[str, object]
    tokens: int
    max_depth: int

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "semantic_fields", MappingProxyType(dict(self.semantic_fields))
        )


def _error(category: str, reason: str) -> FormReadError:
    return FormReadError(category, reason)


def read_form(payload: bytes) -> FormReadResult:
    """Проверить одну запись без рекурсии и без сохранения дерева.

    Полное позиционное дерево намеренно не материализуется: стек содержит
    только глубину открытых списков, а неизвестные значения живут не дольше
    текущей лексемы. Так предел числа лексем ограничивает время, а не создаёт
    второй многомиллионный объект в памяти.
    """
    if not isinstance(payload, bytes):
        raise TypeError("Запись form должна быть bytes.")
    if len(payload) > MAX_BYTES:
        raise _error("budget_exceeded", "запись form превышает предел размера")
    try:
        text = payload.decode("utf-8-sig")
    except UnicodeDecodeError as error:
        raise _error("invalid_utf8", "запись form не является UTF-8") from error

    max_depth = 0
    tokens = 0
    root_closed = False
    marker: int | None = None
    index = 0
    size = len(text)
    # 0 — список пуст и ждёт первое значение либо `}`;
    # 1 — значение прочитано, допустимы только `,` либо `}`;
    # 2 — запятая прочитана, обязательно следующее значение.
    states: list[int] = []

    def token() -> None:
        nonlocal tokens
        tokens += 1
        if tokens > MAX_TOKENS:
            raise _error(
                "budget_exceeded", "запись form превышает предел лексем"
            )

    def начать_значение(*, строка: bool = False, список: bool = False) -> bool:
        """Занять одно место родительского списка; вернуть root-first."""
        if not states or states[-1] not in (0, 2):
            raise _error(
                "invalid_syntax", "между значениями записи form нет запятой"
            )
        первое_корневое = len(states) == 1 and states[-1] == 0
        if первое_корневое and (строка or список):
            raise _error(
                "invalid_marker",
                "маркер form обязан быть первым целым числом корневой записи",
            )
        states[-1] = 1
        return первое_корневое

    while index < size:
        char = text[index]
        if char.isspace():
            index += 1
            continue
        if root_closed:
            if char == "}":
                raise _error(
                    "extra_closing_brace",
                    "в записи form лишняя закрывающая скобка",
                )
            raise _error("trailing_data", "после корневой записи есть данные")
        if char == "{":
            token()
            if states:
                начать_значение(список=True)
            states.append(0)
            if len(states) > MAX_DEPTH:
                raise _error(
                    "budget_exceeded", "запись form превышает предел глубины"
                )
            max_depth = max(max_depth, len(states))
            index += 1
            continue
        if char == "}":
            token()
            if not states:
                raise _error(
                    "extra_closing_brace", "в записи form лишняя закрывающая скобка"
                )
            if states[-1] == 2:
                raise _error(
                    "invalid_syntax", "после запятой в записи form нет значения"
                )
            states.pop()
            index += 1
            if not states:
                root_closed = True
            continue
        if not states:
            raise _error("invalid_root", "корневая запись form не является списком")
        if char == ",":
            token()
            if states[-1] != 1:
                raise _error(
                    "invalid_syntax", "запятая в записи form стоит без значения"
                )
            states[-1] = 2
            index += 1
            continue
        if char == '"':
            token()
            начать_значение(строка=True)
            index += 1
            while index < size:
                current = text[index]
                if current == '"':
                    if index + 1 < size and text[index + 1] == '"':
                        index += 2
                        continue
                    index += 1
                    break
                index += 1
            else:
                raise _error("truncated", "строковая лексема form оборвана")
            continue

        start = index
        while index < size:
            current = text[index]
            if current.isspace() or current in "{},\"":
                break
            index += 1
        if start == index:
            raise _error("invalid_token", "в записи form недопустимая лексема")
        token()
        первое_корневое = начать_значение()
        if первое_корневое:
            marker_text = text[start:index]
            if (
                len(marker_text) > 64
                or not marker_text.isascii()
                or not marker_text.isdecimal()
                or (len(marker_text) > 1 and marker_text.startswith("0"))
            ):
                raise _error(
                    "invalid_marker",
                    "маркер form не является каноническим целым числом",
                )
            marker = int(marker_text)

    if not root_closed or states:
        raise _error("truncated", "запись form оборвана до закрытия списка")
    if marker is None:
        raise _error("invalid_marker", "в записи form отсутствует маркер")

    if marker in KNOWN_MARKERS:
        category = "known_marker_semantics_incomplete"
        reason = "маркер известен, семантика позиционных полей не доказана"
    else:
        category = "unknown_marker"
        reason = "маркер form не поддержан"
    return FormReadResult(
        marker=marker,
        status="partial",
        category=category,
        reason=reason,
        semantic_fields={},
        tokens=tokens,
        max_depth=max_depth,
    )
