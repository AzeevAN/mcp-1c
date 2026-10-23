"""Формулы id источников: это контракт на диске и в HTTP, менять нельзя."""

from mcp1c import source_ids


def test_формы_идентификаторов_совпадают_с_форматом_реестра():
    assert source_ids.modules_id("УТ") == "УТ:modules"
    assert source_ids.extension_prefix("УТ") == "УТ:ext:"
    assert source_ids.extension_modules_id("УТ", "Доп") == "УТ:ext:Доп"


def test_разбор_идентификатора_кода():
    assert source_ids.configuration_of_modules("УТ:modules") == "УТ"
    assert source_ids.configuration_of_modules("УТ") == "УТ"
    assert source_ids.is_code_source_id("УТ:modules")
    assert source_ids.is_code_source_id("УТ:ext:Доп")
    assert not source_ids.is_code_source_id("УТ")
