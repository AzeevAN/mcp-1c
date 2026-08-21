"""Строгая двусторонняя грамматика имён плоской выгрузки."""

from __future__ import annotations

import pytest

from mcp1c.module_address import (
    FlatNameError,
    ключ_адреса,
    разобрать_плоское_имя,
)


MATRIX = [
    ("Document.Объект.Form.Основная.Form", "Документ.Объект.Форма.Основная", "object_form_container"),
    ("Document.Объект.Form.Основная.Form.Module.txt", "Документ.Объект.Форма.Основная", "object_form_text"),
    ("Document.Объект.ObjectModule.txt", "Документ.Объект.МодульОбъекта", "object_module"),
    ("CommonModule.Сервис.Module.txt", "ОбщийМодуль.Сервис", "module"),
    ("Catalog.Объект.ManagerModule.txt", "Справочник.Объект.МодульМенеджера", "manager_module"),
    ("InformationRegister.Регистр.RecordSetModule.txt", "РегистрСведений.Регистр.МодульНабораЗаписей", "recordset_module"),
    ("CommonForm.Основная.Form", "ОбщаяФорма.Основная", "common_form_container"),
    ("CommonForm.Основная.Form.Module.txt", "ОбщаяФорма.Основная", "common_form_text"),
    ("CommonCommand.Открыть.CommandModule.txt", "ОбщаяКоманда.Открыть", "common_command"),
    ("Catalog.Объект.Command.Открыть.CommandModule.txt", "Справочник.Объект.Команда.Открыть", "object_command"),
    ("Constant.Значение.ValueManagerModule.txt", "Константа.Значение.МодульМенеджераЗначения", "value_manager_module"),
    ("Configuration.ManagedApplicationModule.txt", "Конфигурация.МодульУправляемогоПриложения", "configuration"),
]


@pytest.mark.parametrize("filename,address,pattern", MATRIX)
def test_двенадцать_доказанных_форм_имени(filename, address, pattern):
    parsed = разобрать_плоское_имя(filename)

    assert parsed.address == address
    assert parsed.pattern == pattern
    assert parsed.filename() == filename


KIND_EXAMPLES = {
    "AccumulationRegister": ("AccumulationRegister.Пример.RecordSetModule.txt", "РегистрНакопления"),
    "Catalog": ("Catalog.Пример.ObjectModule.txt", "Справочник"),
    "ChartOfCharacteristicTypes": ("ChartOfCharacteristicTypes.Пример.ObjectModule.txt", "ПланВидовХарактеристик"),
    "CommonCommand": ("CommonCommand.Пример.CommandModule.txt", "ОбщаяКоманда"),
    "CommonForm": ("CommonForm.Пример.Form", "ОбщаяФорма"),
    "CommonModule": ("CommonModule.Пример.Module.txt", "ОбщийМодуль"),
    "Configuration": ("Configuration.ManagedApplicationModule.txt", "Конфигурация"),
    "Constant": ("Constant.Пример.ValueManagerModule.txt", "Константа"),
    "DataProcessor": ("DataProcessor.Пример.ObjectModule.txt", "Обработка"),
    "Document": ("Document.Пример.ObjectModule.txt", "Документ"),
    "DocumentJournal": ("DocumentJournal.Пример.Form.Основная.Form", "ЖурналДокументов"),
    "Enum": ("Enum.Пример.ManagerModule.txt", "Перечисление"),
    "ExchangePlan": ("ExchangePlan.Пример.ObjectModule.txt", "ПланОбмена"),
    "FilterCriterion": ("FilterCriterion.Пример.Form.Основная.Form", "КритерийОтбора"),
    "HTTPService": ("HTTPService.Пример.Module.txt", "HTTPСервис"),
    "InformationRegister": ("InformationRegister.Пример.RecordSetModule.txt", "РегистрСведений"),
    "Report": ("Report.Пример.ObjectModule.txt", "Отчет"),
    "WebService": ("WebService.Пример.Module.txt", "WebСервис"),
}

ALLOWED_PATTERNS = {
    "AccumulationRegister": {"manager_module", "object_form_container", "recordset_module"},
    "Catalog": {"manager_module", "object_command", "object_form_container", "object_form_text", "object_module"},
    "ChartOfCharacteristicTypes": {"object_form_container", "object_form_text", "object_module"},
    "CommonCommand": {"common_command"},
    "CommonForm": {"common_form_container", "common_form_text"},
    "CommonModule": {"compiled", "module"},
    "Configuration": {"configuration"},
    "Constant": {"value_manager_module"},
    "DataProcessor": {"manager_module", "object_command", "object_form_container", "object_form_text", "object_module"},
    "Document": {"manager_module", "object_form_container", "object_form_text", "object_module"},
    "DocumentJournal": {"object_form_container", "object_form_text"},
    "Enum": {"manager_module", "object_form_container"},
    "ExchangePlan": {"manager_module", "object_command", "object_form_container", "object_form_text", "object_module"},
    "FilterCriterion": {"object_form_container"},
    "HTTPService": {"module"},
    "InformationRegister": {"manager_module", "object_command", "object_form_container", "object_form_text", "recordset_module"},
    "Report": {"manager_module", "object_command", "object_form_container", "object_form_text", "object_module"},
    "WebService": {"module"},
}


def _filename_for(kind: str, pattern: str) -> str:
    templates = {
        "manager_module": f"{kind}.Пример.ManagerModule.txt",
        "object_form_container": f"{kind}.Пример.Form.Основная.Form",
        "recordset_module": f"{kind}.Пример.RecordSetModule.txt",
        "object_command": f"{kind}.Пример.Command.Открыть.CommandModule.txt",
        "object_form_text": f"{kind}.Пример.Form.Основная.Form.Module.txt",
        "object_module": f"{kind}.Пример.ObjectModule.txt",
        "common_command": f"{kind}.Пример.CommandModule.txt",
        "common_form_container": f"{kind}.Пример.Form",
        "common_form_text": f"{kind}.Пример.Form.Module.txt",
        "compiled": f"{kind}.Пример.Module",
        "module": f"{kind}.Пример.Module.txt",
        "configuration": f"{kind}.ManagedApplicationModule.txt",
        "value_manager_module": f"{kind}.Пример.ValueManagerModule.txt",
    }
    return templates[pattern]


ALL_PATTERNS = frozenset().union(*ALLOWED_PATTERNS.values())


@pytest.mark.parametrize("kind,example", KIND_EXAMPLES.items())
def test_все_восемнадцать_видов_адресуются(kind, example):
    filename, public_kind = example
    parsed = разобрать_плоское_имя(filename)

    assert parsed.address.startswith(f"{public_kind}.")
    assert parsed.filename() == filename


@pytest.mark.parametrize(
    "kind,pattern",
    [
        (kind, pattern)
        for kind, patterns in ALLOWED_PATTERNS.items()
        for pattern in sorted(patterns)
    ],
)
def test_ровно_сорок_девять_подтверждённых_сочетаний_принимаются(kind, pattern):
    assert sum(map(len, ALLOWED_PATTERNS.values())) == 49
    parsed = разобрать_плоское_имя(_filename_for(kind, pattern))

    assert parsed.pattern == pattern


@pytest.mark.parametrize(
    "kind,pattern",
    [
        (kind, pattern)
        for kind, patterns in ALLOWED_PATTERNS.items()
        for pattern in sorted(ALL_PATTERNS - patterns)
    ],
)
def test_недоказанные_сочетания_вида_и_pattern_отвергаются(kind, pattern):
    with pytest.raises(FlatNameError) as caught:
        разобрать_плоское_имя(_filename_for(kind, pattern))

    assert caught.value.category == "unsupported_flat_name"


@pytest.mark.parametrize(
    "filename",
    [
        "CommonCommand.Имя.Module.txt",
        "CommonForm.Имя.Module.txt",
        "Constant.Имя.Form.Форма.Form",
        "HTTPService.Имя.Form.Форма.Form",
        "WebService.Имя.ObjectModule.txt",
        "FilterCriterion.Имя.Module.txt",
        "Document.Имя.Command.Открыть.CommandModule.txt",
    ],
)
def test_недоказанный_декартов_продукт_вида_и_pattern_отвергается(filename):
    with pytest.raises(FlatNameError) as caught:
        разобрать_плоское_имя(filename)

    assert caught.value.category == "unsupported_flat_name"


@pytest.mark.parametrize(
    "filename",
    [
        # По одному соседу с пропущенным и лишним компонентом для каждого
        # из двенадцати доказанных pattern.
        "Document.Объект.Form.Form",
        "Document.Объект.Form.Основная.Form.Extra",
        "Document.Объект.Form.Основная.Module.txt",
        "Document.Объект.Form.Основная.Form.Module.Extra.txt",
        "Document.ObjectModule.txt",
        "Document.Объект.Extra.ObjectModule.txt",
        "CommonModule.Module.txt",
        "CommonModule.Сервис.Extra.Module.txt",
        "Catalog.ManagerModule.txt",
        "Catalog.Объект.Extra.ManagerModule.txt",
        "InformationRegister.RecordSetModule.txt",
        "InformationRegister.Регистр.Extra.RecordSetModule.txt",
        "CommonForm.Form",
        "CommonForm.Основная.Form.Extra",
        "CommonForm.Form.Module.txt",
        "CommonForm.Основная.Extra.Form.Module.txt",
        "CommonCommand.CommandModule.txt",
        "CommonCommand.Открыть.Extra.CommandModule.txt",
        "Catalog.Объект.Command.CommandModule.txt",
        "Catalog.Объект.Command.Открыть.Extra.CommandModule.txt",
        "Constant.ValueManagerModule.txt",
        "Constant.Значение.Extra.ValueManagerModule.txt",
        "ManagedApplicationModule.txt",
        "Configuration.Имя.ManagedApplicationModule.txt",
    ],
)
def test_каждый_pattern_имеет_соседний_отрицательный_пример(filename):
    with pytest.raises(FlatNameError):
        разобрать_плоское_имя(filename)


@pytest.mark.parametrize(
    "filename,category",
    [
        ("Unknown.Имя.Module.txt", "unsupported_flat_kind"),
        ("Document..ObjectModule.txt", "unsupported_flat_name"),
        ("Document.Имя.UnknownModule.txt", "unsupported_flat_name"),
        ("Document.Имя.Extra.ObjectModule.txt", "unsupported_flat_name"),
        ("Document.Имя.Form..Form", "unsupported_flat_name"),
        ("Document.Имя.Form.Форма.Form.Extra", "unsupported_flat_name"),
        ("CommonForm.Имя.Form.Extra", "unsupported_flat_name"),
        ("CommonCommand.Имя.commandModule.txt", "unsupported_flat_name"),
        ("Configuration.Имя.ManagedApplicationModule.txt", "unsupported_flat_name"),
        ("Configuration.ManagedApplicationModule.TXT", "unsupported_flat_name"),
    ],
)
def test_соседние_неизвестные_формы_не_угадываются(filename, category):
    with pytest.raises(FlatNameError) as caught:
        разобрать_плоское_имя(filename)

    assert caught.value.category == category
    assert filename not in str(caught.value)


def test_casefold_ключ_делает_коллизию_наблюдаемой_без_смены_адреса():
    upper = разобрать_плоское_имя("Document.Заказ.ObjectModule.txt")
    lower = разобрать_плоское_имя("Document.заказ.ObjectModule.txt")

    assert upper.address != lower.address
    assert ключ_адреса(upper.address) == ключ_адреса(lower.address)


def test_скомпилированный_общий_модуль_входит_в_ту_же_грамматику():
    parsed = разобрать_плоское_имя("CommonModule.Закрытый.Module")

    assert parsed.address == "ОбщийМодуль.Закрытый"
    assert parsed.pattern == "compiled"
    assert parsed.compiled is True
    assert parsed.filename() == "CommonModule.Закрытый.Module"
