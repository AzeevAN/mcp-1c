"""Непрозрачные курсоры продолжения: JSON в base64url без выравнивания.

Курсор — часть публичного ответа инструментов: агент получает его в одном
вызове и возвращает в следующем. Упаковка и строгая распаковка жили
четырьмя копиями, расходившимися в мелочах (проверка длины, `validate`,
перечень перехватываемых исключений). Здесь — только транспорт; что лежит
внутри и как сверяется отпечаток, решает вызывающий: у каждого курсора своя
схема и свой текст ошибки.
"""

from __future__ import annotations

import base64
import binascii
import json

# Предел длины принятого курсора: настоящий курсор — сотня-другая символов,
# а разбирать мегабайтную строку ради отказа незачем.
MAX_CURSOR_CHARS = 2048

# Всё, чем строгая распаковка может отказать на испорченном курсоре.
# `RecursionError` — глубоко вложенный JSON.
DECODE_ERRORS = (ValueError, TypeError, binascii.Error, RecursionError)


def encode(
    payload: object, *, sort_keys: bool = False, ensure_ascii: bool = True
) -> str:
    """Упаковать состояние курсора.

    Параметры сериализации оставлены вызывающему: у существующих курсоров
    они разные, а выдача должна совпадать байт в байт с прежней.
    """
    raw = json.dumps(
        payload,
        ensure_ascii=ensure_ascii,
        sort_keys=sort_keys,
        separators=(",", ":"),
    ).encode("utf-8")
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def decode(cursor: object, *, max_chars: int = MAX_CURSOR_CHARS) -> object:
    """Строго распаковать курсор; на любом дефекте — исключение из
    `DECODE_ERRORS`.

    Строго — значит без молчаливого пропуска посторонних символов
    (`validate=True`): курсор с мусором отклоняется, а не читается как
    другой.
    """
    if not isinstance(cursor, str) or not 1 <= len(cursor) <= max_chars:
        raise ValueError("курсор вне допустимой длины")
    return json.loads(
        base64.b64decode(
            cursor + "=" * (-len(cursor) % 4),
            altchars=b"-_",
            validate=True,
        )
    )
