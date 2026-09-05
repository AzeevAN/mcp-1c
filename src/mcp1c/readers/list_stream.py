"""Bounded-разбор списочного потока 1С без знания семантики формы."""

from __future__ import annotations

from dataclasses import dataclass


MAX_BYTES = 16 * 1024 * 1024
MAX_DEPTH = 128
MAX_TOKENS = 2_000_000
MAX_SEMANTIC_TOKENS = 250_000
_BASE64_PREFIX = "#base64:"
_BASE64_ALPHABET = frozenset(
    "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/="
)


class ListStreamError(ValueError):
    def __init__(self, category: str, reason: str):
        self.category = category
        self.reason = reason
        super().__init__(reason)


@dataclass(frozen=True, slots=True)
class ListStreamScan:
    marker: int
    tokens: int
    max_depth: int
    text: str


def _error(category: str, reason: str) -> ListStreamError:
    return ListStreamError(category, reason)


def scan_list_stream(payload: bytes) -> ListStreamScan:
    """Проверить грамматику целиком с постоянной памятью по числу лексем."""
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
    # 0 — список ждёт первое значение; 1 — значение прочитано;
    # 2 — после запятой обязательно следующее значение.
    states: list[int] = []

    def token() -> None:
        nonlocal tokens
        tokens += 1
        if tokens > MAX_TOKENS:
            raise _error("budget_exceeded", "запись form превышает предел лексем")

    def start_value(*, string: bool = False, sequence: bool = False) -> bool:
        if not states or states[-1] not in (0, 2):
            raise _error("invalid_syntax", "между значениями записи form нет запятой")
        root_first = len(states) == 1 and states[-1] == 0
        if root_first and (string or sequence):
            raise _error(
                "invalid_marker",
                "маркер form обязан быть первым целым числом корневой записи",
            )
        states[-1] = 1
        return root_first

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
                start_value(sequence=True)
            states.append(0)
            if len(states) > MAX_DEPTH:
                raise _error("budget_exceeded", "запись form превышает предел глубины")
            max_depth = max(max_depth, len(states))
            index += 1
            continue
        if char == "}":
            token()
            if not states:
                raise _error(
                    "extra_closing_brace",
                    "в записи form лишняя закрывающая скобка",
                )
            if states[-1] == 2:
                raise _error("invalid_syntax", "после запятой в записи form нет значения")
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
                raise _error("invalid_syntax", "запятая в записи form стоит без значения")
            states[-1] = 2
            index += 1
            continue
        if char == '"':
            token()
            start_value(string=True)
            index += 1
            while index < size:
                if text[index] == '"':
                    if index + 1 < size and text[index + 1] == '"':
                        index += 2
                        continue
                    index += 1
                    break
                index += 1
            else:
                raise _error("truncated", "строковая лексема form оборвана")
            continue
        if text.startswith(_BASE64_PREFIX, index):
            token()
            if start_value():
                raise _error(
                    "invalid_marker",
                    "маркер form обязан быть первым целым числом корневой записи",
                )
            index += len(_BASE64_PREFIX)
            while index < size and text[index] not in "{},\"":
                current = text[index]
                if not current.isspace() and current not in _BASE64_ALPHABET:
                    raise _error(
                        "invalid_token",
                        "base64-лексема form содержит недопустимый символ",
                    )
                index += 1
            continue

        start = index
        while (
            index < size
            and not text[index].isspace()
            and text[index] not in "{},\""
        ):
            index += 1
        if start == index:
            raise _error("invalid_token", "в записи form недопустимая лексема")
        token()
        root_first = start_value()
        if root_first:
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
    return ListStreamScan(marker, tokens, max_depth, text)


def materialize_list_stream(scan: ListStreamScan) -> list[object]:
    """Материализовать уже проверенный поток для семантического ридера."""
    if scan.tokens > MAX_SEMANTIC_TOKENS:
        raise _error(
            "semantic_budget_exceeded",
            "семантический разбор form превышает предел лексем",
        )
    root: list[object] | None = None
    stack: list[list[object]] = []
    text = scan.text
    index = 0
    size = len(text)
    while index < size:
        char = text[index]
        if char.isspace() or char == ",":
            index += 1
            continue
        if char == "{":
            value: list[object] = []
            if stack:
                stack[-1].append(value)
            else:
                root = value
            stack.append(value)
            index += 1
            continue
        if char == "}":
            stack.pop()
            index += 1
            continue
        if char == '"':
            index += 1
            parts: list[str] = []
            start = index
            while index < size:
                if text[index] != '"':
                    index += 1
                    continue
                parts.append(text[start:index])
                if index + 1 < size and text[index + 1] == '"':
                    parts.append('"')
                    index += 2
                    start = index
                    continue
                index += 1
                break
            stack[-1].append("".join(parts))
            continue
        start = index
        if text.startswith(_BASE64_PREFIX, index):
            index += len(_BASE64_PREFIX)
            while index < size and text[index] not in "{},\"":
                index += 1
        else:
            while (
                index < size
                and not text[index].isspace()
                and text[index] not in "{},\""
            ):
                index += 1
        stack[-1].append(text[start:index].strip())
    assert root is not None and not stack
    return root


__all__ = [
    "ListStreamError",
    "ListStreamScan",
    "MAX_BYTES",
    "MAX_DEPTH",
    "MAX_SEMANTIC_TOKENS",
    "MAX_TOKENS",
    "materialize_list_stream",
    "scan_list_stream",
]
