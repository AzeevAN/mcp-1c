from __future__ import annotations

import pytest

from mcp1c.capability_modules.forms.event_catalog import event_signature
from mcp1c.capability_modules.forms.version_catalog import (
    DEFAULT_PLATFORM_PROFILE,
    platform_profile,
    platform_resolution,
)


@pytest.mark.parametrize(
    "version",
    ["8.3.23", "8.3.23.1997", "8.3.24", "8.3.25", "8.3.26", "8.3.27.2130"],
)
def test_современный_профиль_покрывает_весь_принятый_интервал(version):
    profile = platform_profile(version)

    assert profile is not None
    assert profile.name == "managed_form_2_16_modern"
    assert profile.event_profile == "modern"
    assert profile.formats == ("2.16",)
    assert profile.support == "compiler"


def test_профиль_8_3_5_известен_по_справке_но_не_выдаётся_за_compiler_support():
    profile = platform_profile("8.3.5.1570")

    assert profile is not None
    assert profile.name == "managed_form_8_3_5_documented"
    assert profile.event_profile == "8.3.5"
    assert profile.formats == ()
    assert profile.support == "documentation_only"


@pytest.mark.parametrize(
    ("version", "event_profile"),
    [
        ("8.3.16", "8.3.5"),
        ("8.3.22", "8.3.5"),
        ("8.3.28", "modern"),
        ("9.0.1", "modern"),
    ],
)
def test_версия_вне_доказанных_интервалов_получает_непроверенный_fallback(
    version, event_profile
):
    resolution = platform_resolution(version)

    assert resolution is not None
    assert resolution.confidence == "unverified"
    assert resolution.profile.event_profile == event_profile
    assert platform_profile(version) is resolution.profile


def test_отсутствующая_версия_сохраняет_совместимость_с_текущим_контрактом():
    assert platform_profile(None) is DEFAULT_PLATFORM_PROFILE
    assert platform_resolution(None).confidence == "unspecified"


@pytest.mark.parametrize(
    ("version", "confidence"),
    [
        ("8.3.23.1997", "confirmed"),
        ("8.3.23.1000", "inferred"),
        ("8.3.24", "inferred"),
        ("8.3.26.15", "confirmed"),
        ("8.3.27.2130", "confirmed"),
    ],
)
def test_профиль_отделяет_подтверждение_от_выведенной_совместимости(
    version, confidence
):
    assert platform_resolution(version).confidence == confidence


@pytest.mark.parametrize("version", ["", "8.3", "8.3.x", " 8.3.23", "8.3.23.1.2"])
def test_некорректное_написание_версии_не_угадывается(version):
    assert platform_profile(version) is None


def test_справочный_профиль_8_3_5_сохраняет_отличающиеся_сигнатуры():
    before_close = event_signature("form", "BeforeClose", profile="8.3.5")
    start_choice = event_signature("input_field", "StartChoice", profile="8.3.5")
    auto_complete = event_signature("input_field", "AutoComplete", profile="8.3.5")
    url_processing = event_signature(
        "label_decoration", "URLProcessing", profile="8.3.5"
    )
    label_field_url = event_signature(
        "label_field", "URLProcessing", profile="8.3.5"
    )

    assert before_close is not None
    assert before_close.parameters == ("Отказ", "СтандартнаяОбработка")
    assert start_choice is not None
    assert start_choice.parameters == (
        "Элемент",
        "ДанныеВыбора",
        "СтандартнаяОбработка",
    )
    assert auto_complete is not None
    assert auto_complete.parameters[3] == "Параметры"
    assert url_processing is not None
    assert url_processing.parameters == (
        "Элемент",
        "НавигационнаяСсылка",
        "СтандартнаяОбработка",
    )
    assert label_field_url is not None
    assert label_field_url.parameters[1] == "НавигационнаяСсылка"


def test_современный_профиль_не_смешивает_сигнатуру_8_3_5():
    before_close = event_signature("form", "BeforeClose", profile="modern")
    start_choice = event_signature("input_field", "StartChoice", profile="modern")
    url_processing = event_signature(
        "label_decoration", "URLProcessing", profile="modern"
    )
    label_field_click = event_signature("label_field", "Click", profile="modern")

    assert before_close is not None
    assert before_close.parameters == (
        "Отказ",
        "ЗавершениеРаботы",
        "ТекстПредупреждения",
        "СтандартнаяОбработка",
    )
    assert url_processing is not None
    assert url_processing.parameters[1] == (
        "НавигационнаяСсылкаФорматированнойСтроки"
    )
    assert label_field_click is not None
    assert label_field_click.parameters == ("Элемент", "СтандартнаяОбработка")
    assert start_choice is not None
    assert "ВыборДобавлением" in start_choice.parameters
