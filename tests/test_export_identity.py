"""Правило «конфигурация или расширение»: порядок признаков — контракт."""

from __future__ import annotations

import itertools

import pytest

from mcp1c.export_identity import ExportVerdict, classify

_EXTENSION = {
    "ObjectBelonging": "Adopted",
    "ConfigurationExtensionPurpose": "Customization",
    "NamePrefix": "рсш_",
}


def test_полное_расширение():
    assert classify({**_EXTENSION, "Name": "Доп"}) is ExportVerdict.EXTENSION


@pytest.mark.parametrize("mode", ["Version8_3_21", "DontUse"])
def test_конфигурация_по_режиму_совместимости(mode):
    assert classify({"Name": "УТ", "CompatibilityMode": mode}) is (
        ExportVerdict.CONFIGURATION
    )


def test_сильный_признак_закрывает_ветку_конфигурации():
    """Регрессия: все признаки расширения плюс CompatibilityMode — не
    конфигурация, иначе расширение заменило бы её данные."""
    assert classify({**_EXTENSION, "CompatibilityMode": "Version8_3_21"}) is (
        ExportVerdict.INCOMPLETE_EXTENSION
    )


def test_без_признаков_отказ():
    assert classify({"Name": "X", "NamePrefix": "x_"}) is ExportVerdict.NO_SIGNAL
    assert classify({}) is ExportVerdict.NO_SIGNAL


def test_все_сочетания_признаков():
    tags = (
        "ObjectBelonging",
        "ConfigurationExtensionPurpose",
        "NamePrefix",
        "CompatibilityMode",
    )
    for present in itertools.product((False, True), repeat=len(tags)):
        flags = dict(zip(tags, present))
        properties = {tag: "v" for tag, on in flags.items() if on}
        strong = flags["ObjectBelonging"] or flags["ConfigurationExtensionPurpose"]
        if strong:
            expected = (
                ExportVerdict.EXTENSION
                if flags["ObjectBelonging"]
                and flags["ConfigurationExtensionPurpose"]
                and flags["NamePrefix"]
                and not flags["CompatibilityMode"]
                else ExportVerdict.INCOMPLETE_EXTENSION
            )
        elif flags["CompatibilityMode"]:
            expected = ExportVerdict.CONFIGURATION
        else:
            expected = ExportVerdict.NO_SIGNAL
        assert classify(properties) is expected, flags
