"""Общий безопасный контракт сравнения токенов HTTP и дашборда."""

from __future__ import annotations

import hmac


def same_token(given: str, expected: str) -> bool:
    """Сравнить непустой ожидаемый токен в постоянном времени.

    `hmac.compare_digest` на строках с кириллицей бросает `TypeError`, а токен
    задаёт человек. Сравниваем UTF-8-байты: длина при этом всё равно утекает,
    но она утекает и в HTTP-представлении секрета.
    """
    if not expected:
        return False
    return hmac.compare_digest(given.encode("utf-8"), expected.encode("utf-8"))
