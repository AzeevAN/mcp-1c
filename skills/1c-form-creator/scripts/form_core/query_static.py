"""Узкие статические проверки доказанных нативно ошибок текста запроса."""

from __future__ import annotations

import re

from .diagnostics import CheckStatus, Diagnostic


_IDENTIFIER = r"[A-Za-zА-Яа-яЁё_][A-Za-zА-Яа-яЁё0-9_]*"
_NEW_QUERY = re.compile(
    rf"(?i)^\s*(?P<name>{_IDENTIFIER})\s*=\s*(?:Новый|New)\s+"
    r"(?:Запрос|Query)\s*;\s*(?://.*)?$"
)
_QUERY_TEXT_PREFIX = re.compile(
    rf"(?i)^\s*(?P<name>{_IDENTIFIER})\s*\.\s*(?:Текст|Text)\s*="
)
_STATIC_QUERY_TEXT = re.compile(
    rf'(?i)^\s*(?P<name>{_IDENTIFIER})\s*\.\s*(?:Текст|Text)\s*=\s*'
    r'"(?P<query>(?:""|[^"])*)"\s*;\s*(?://.*)?$'
)
_SLICE_LAST = re.compile(r"(?i)(?:СрезПоследних|SliceLast)\s*\(")


def _diagnostic(
    status: CheckStatus,
    code: str,
    line: int,
    message: str,
) -> Diagnostic:
    return Diagnostic(
        "bsl_static",
        status,
        code,
        f"$module_bsl:{line}",
        message,
    )


def _top_level_commas(text: str, opening: int) -> tuple[list[int], bool]:
    """Вернуть запятые аргументов и признак найденной закрывающей скобки."""

    depth = 1
    quoted = False
    commas: list[int] = []
    index = opening + 1
    while index < len(text):
        char = text[index]
        if char == '"':
            if quoted and index + 1 < len(text) and text[index + 1] == '"':
                index += 2
                continue
            quoted = not quoted
        elif not quoted:
            if char == "(":
                depth += 1
            elif char == ")":
                depth -= 1
                if depth == 0:
                    return commas, True
            elif char == "," and depth == 1:
                commas.append(index)
        index += 1
    return commas, False


def _check_slice_last(query: str, *, line: int) -> list[Diagnostic]:
    diagnostics: list[Diagnostic] = []
    for match in _SLICE_LAST.finditer(query):
        commas, closed = _top_level_commas(query, match.end() - 1)
        if not closed:
            diagnostics.append(
                _diagnostic(
                    "warning",
                    "query_virtual_table_arguments_not_checked",
                    line,
                    (
                        "Аргументы СрезПоследних не удалось ограничить "
                        "парной скобкой; текст запроса не проверен."
                    ),
                )
            )
        elif len(commas) > 1:
            diagnostics.append(
                _diagnostic(
                    "failed",
                    "query_virtual_table_condition_separator",
                    line,
                    (
                        "СрезПоследних принимает период и одно условие отбора. "
                        "Условия внутри второго аргумента объединяются И, "
                        "а не дополнительной запятой."
                    ),
                )
            )
    return diagnostics


def check_static_queries(
    module_bsl: str,
    procedures: list[object],
) -> list[Diagnostic]:
    """Проверить только статические однострочные тексты объектов `Запрос`.

    Динамические и многострочные присваивания явно остаются непроверенными.
    Полный синтаксис языка запросов эта функция не валидирует.
    """

    lines = module_bsl.splitlines()
    diagnostics: list[Diagnostic] = []
    for procedure in procedures:
        start = max(procedure.строка - 1, 0)
        end = procedure.конец if procedure.конец else len(lines)
        query_names: set[str] = set()
        for offset, source in enumerate(lines[start:end], start=start + 1):
            new_query = _NEW_QUERY.fullmatch(source)
            if new_query is not None:
                query_names.add(new_query.group("name").casefold())
                continue

            prefix = _QUERY_TEXT_PREFIX.match(source)
            if prefix is None or prefix.group("name").casefold() not in query_names:
                continue
            static = _STATIC_QUERY_TEXT.fullmatch(source)
            if static is None:
                diagnostics.append(
                    _diagnostic(
                        "warning",
                        "query_text_not_checked",
                        offset,
                        (
                            "Проверяются только статические однострочные "
                            "присваивания Запрос.Текст; этот текст не проверен."
                        ),
                    )
                )
                continue
            query = static.group("query").replace('""', '"')
            diagnostics.extend(_check_slice_last(query, line=offset))
    return diagnostics


__all__ = ["check_static_queries"]
