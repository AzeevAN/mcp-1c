"""Терпимый bounded-разбор позиционной записи ``form``."""

import pytest

from mcp1c.form_reader import (
    MAX_BYTES,
    MAX_DEPTH,
    MAX_TOKENS,
    FormReadError,
    read_form,
)


@pytest.mark.parametrize("marker", [19, 20, 23, 25, 26, 27])
def test_известный_marker_прочитан_но_позиционные_поля_не_выдуманы(marker):
    result = read_form(f'{{{marker},"строка с ""кавычкой""",{{1,2}}}}'.encode())

    assert result.marker == marker
    assert result.status == "partial"
    assert result.category == "known_marker_semantics_incomplete"
    assert result.semantic_fields == {}


def test_неизвестный_marker_остаётся_частичным_а_не_успешным():
    result = read_form(b"{99,1,2}")

    assert result.marker == 99
    assert result.status == "partial"
    assert result.category == "unknown_marker"


def test_многострочный_base64_остаётся_одной_лексемой():
    result = read_form(
        b"{26,#base64:AgFT\r\nOLAFa7ACtU0K\n"
        b"bO4uca29HkfTrNRsiEms6KKeXFhJtSKWz30v==,1}"
    )

    assert result.marker == 26
    assert result.tokens == 7


def test_base64_не_принимает_постороннюю_лексему():
    with pytest.raises(FormReadError) as caught:
        read_form(b"{26,#base64:AA?AA,1}")

    assert caught.value.category == "invalid_token"


@pytest.mark.parametrize(
    ("payload", "category"),
    [
        (b"{19,{1,2}", "truncated"),
        (b"{19}}", "extra_closing_brace"),
        (b"{19,\xff}", "invalid_utf8"),
    ],
)
def test_битая_запись_даёт_обезличенную_категорию(payload, category):
    with pytest.raises(FormReadError) as caught:
        read_form(payload)

    assert caught.value.category == category
    assert payload[:8].decode("utf-8", "ignore") not in str(caught.value)


def test_глубина_128_принимается_а_129_ограничивается():
    accepted = (
        b"{19," + b"{" * (MAX_DEPTH - 1) + b"0" + b"}" * (MAX_DEPTH - 1) + b"}"
    )
    rejected = (
        b"{19," + b"{" * MAX_DEPTH + b"0" + b"}" * MAX_DEPTH + b"}"
    )

    assert read_form(accepted).max_depth == MAX_DEPTH
    with pytest.raises(FormReadError) as caught:
        read_form(rejected)
    assert caught.value.category == "budget_exceeded"


def _tokens_payload(token_count: int) -> bytes:
    # `{19,{}}` содержит 6 лексем; каждая следующая пара `,0` — ещё две.
    if token_count % 2 == 0:
        return b"{19,{}" + b",0" * ((token_count - 6) // 2) + b"}"
    # `{19,{0}}` содержит 7 лексем.
    return b"{19,{0}" + b",0" * ((token_count - 7) // 2) + b"}"


def test_два_миллиона_лексем_принимаются_следующая_ограничивается():
    accepted = read_form(_tokens_payload(MAX_TOKENS))
    assert accepted.tokens == MAX_TOKENS

    with pytest.raises(FormReadError) as caught:
        read_form(_tokens_payload(MAX_TOKENS + 1))
    assert caught.value.category == "budget_exceeded"


def test_ровно_16_mib_принимаются_следующий_байт_ограничивается():
    accepted = b"{19}" + b" " * (MAX_BYTES - 4)
    assert len(accepted) == MAX_BYTES
    assert read_form(accepted).marker == 19

    with pytest.raises(FormReadError) as caught:
        read_form(accepted + b" ")
    assert caught.value.category == "budget_exceeded"


@pytest.mark.parametrize(
    "payload",
    [
        b"{{19}}",
        b'{"19"}',
        b"{,19}",
        b"{19,}",
        b"{19 20}",
        b"{19,,20}",
        b"{}",
        b"{1_9}",
        "{١٩}".encode(),
        b"{+19}",
        b"{019}",
    ],
)
def test_битая_грамматика_не_маскируется_под_известный_marker(payload):
    with pytest.raises(FormReadError) as caught:
        read_form(payload)

    assert caught.value.category in {"invalid_marker", "invalid_syntax"}


def test_длинная_строка_вместо_marker_отклоняется_без_накопления():
    payload = b'{"' + b"x" * (1024 * 1024) + b'"}'

    with pytest.raises(FormReadError) as caught:
        read_form(payload)

    assert caught.value.category == "invalid_marker"
