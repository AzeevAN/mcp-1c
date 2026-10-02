"""Курсоры продолжения: формат совпадает с прежним, распаковка строгая."""

from __future__ import annotations

import base64
import json

import pytest

from mcp1c import cursor


def _old_encode(payload, **options) -> str:
    raw = json.dumps(payload, separators=(",", ":"), **options).encode("utf-8")
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


@pytest.mark.parametrize(
    "options",
    [{}, {"sort_keys": True}, {"sort_keys": True, "ensure_ascii": False}],
)
def test_упаковка_байт_в_байт_как_прежде(options):
    payload = {"v": 1, "sha256": "ab" * 32, "offset": 7, "item": "Запрос"}

    assert cursor.encode(payload, **options) == _old_encode(payload, **options)


def test_распаковка_возвращает_упакованное():
    payload = {"v": 1, "sha256": "00", "template": 3, "method": 0}

    assert cursor.decode(cursor.encode(payload)) == payload


@pytest.mark.parametrize(
    "value",
    ["", "x" * (cursor.MAX_CURSOR_CHARS + 1), None, 5, "!!!!", "eyJ2Ijox", "e30*"],
)
def test_испорченный_курсор_отклоняется(value):
    with pytest.raises(cursor.DECODE_ERRORS):
        cursor.decode(value)
