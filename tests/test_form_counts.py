"""Повреждение счётчика локально одной форме, а не всему индексу."""

import pytest

from mcp1c.form_reader import read_form, FormReadError
from mcp1c.modules_index import Формы
from module_samples import v8_container_bytes
from test_form_reader import _stream


@pytest.mark.parametrize("count", ["²", "1" * 4301, "-1", "+1", "01", "١"], ids=["unicode", "long", "negative", "plus", "leading-zero", "arabic"])
def test_неканонический_счётчик_локализован(tmp_path, count):
    payload = _stream(["25", ["0"], [["0"], "2", [count], ["0"]], *(["0"] * 15)]).encode()
    with pytest.raises(FormReadError) as caught:
        read_form(payload)
    assert caught.value.category == "invalid_count"
    for name, form in (("Broken", payload), ("Good", b"{19}")):
        (tmp_path / f"CommonForm.{name}.Form").write_bytes(v8_container_bytes([("form", form)]))
    forms = Формы.построить(tmp_path)
    assert forms.состав("ОбщаяФорма.Broken").битая
    assert not forms.состав("ОбщаяФорма.Good").битая
    assert forms.непрочитанных == 1
    restored = Формы._из_состояния(forms._состояние())
    assert restored.состав("ОбщаяФорма.Broken").битая
    assert not restored.состав("ОбщаяФорма.Good").битая


def test_правильный_но_несогласованный_счётчик_не_считается_прочитанным():
    payload = _stream(["25", ["0"], [["0"], "2", ["1"], ["0"]], *(["0"] * 15)]).encode()
    result = read_form(payload)
    assert result.semantic_fields == {}
    assert result.category == "known_marker_semantics_deferred"
