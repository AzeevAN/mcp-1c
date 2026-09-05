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
def test_неполная_запись_известного_marker_остаётся_отложенной(marker):
    result = read_form(f'{{{marker},"строка с ""кавычкой""",{{1,2}}}}'.encode())

    assert result.marker == marker
    assert result.status == "deferred"
    assert result.category == "known_marker_semantics_deferred"
    assert result.semantic_fields == {}


def test_неизвестный_marker_остаётся_отложенным():
    result = read_form(b"{99,1,2}")

    assert result.marker == 99
    assert result.status == "deferred"
    assert result.category == "unknown_marker"


def test_многострочный_base64_остаётся_одной_лексемой():
    result = read_form(
        b"{26,#base64:AgFT\r\nOLAFa7ACtU0K\n"
        b"bO4uca29HkfTrNRsiEms6KKeXFhJtSKWz30v==,1}"
    )

    assert result.marker == 26
    assert result.tokens == 7


def _stream(value):
    if isinstance(value, list):
        return "{" + ",".join(_stream(item) for item in value) + "}"
    if isinstance(value, tuple):
        return '"' + value[0].replace('"', '""') + '"'
    return str(value)


@pytest.mark.parametrize(("marker", "tail"), [(25, 15), (26, 16), (27, 17)])
def test_профили_обычной_формы_читают_реквизиты_и_элементы(marker, tail):
    panel = "09ccdc77-ea1a-4a6d-ab1c-3435eada2433"
    input_field = "381ed624-9217-4e63-85db-c4c3cb87daae"
    root_control = [
        panel,
        "0",
        ["14", ("Форма",)],
        [input_field, "1", ["14", ("ПолеВвода",)]],
    ]
    attributes = [
        ["0"],
        "2",
        [
            "1",
            [["1"], "0", "1", ("Значение",), [("Pattern",), [("S",)]]],
        ],
        ["0"],
    ]
    payload = _stream([str(marker), root_control, attributes, *(["0"] * tail)]).encode()

    result = read_form(payload)

    assert result.status == "deferred"
    assert result.category == "event_semantics_deferred"
    assert result.semantic_fields["attributes"] == ("Значение",)
    assert result.semantic_fields["elements"] == ("ПолеВвода",)
    assert result.semantic_fields["control_types"] == ("InputField",)


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
