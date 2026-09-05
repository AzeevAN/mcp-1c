"""Контракт нормальных и ошибочных состояний тела модуля."""

from mcp1c.readers.modules import read_module_body


def test_пустое_тело_модуля_прочитано_успешно():
    result = read_module_body(b"\xef\xbb\xbf \r\n\t")

    assert result.state == "empty"
    assert result.text is not None
    assert result.reason == ""


def test_непустое_тело_модуля_прочитано_успешно():
    result = read_module_body("Процедура Выполнить()\nКонецПроцедуры".encode())

    assert result.state == "ready"
    assert "Выполнить" in (result.text or "")


def test_не_utf8_тело_является_реальным_отказом_чтения():
    result = read_module_body(b"\xff")

    assert result.state == "unreadable"
    assert result.text is None
