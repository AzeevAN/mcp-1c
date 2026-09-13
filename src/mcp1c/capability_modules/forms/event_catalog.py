"""Закрытый каталог доказанных событий управляемой формы."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class EventSignature:
    directive: str
    parameters: tuple[str, ...]


EVENT_SIGNATURES: dict[tuple[str, str], EventSignature] = {
    ("form", "OnCreateAtServer"): EventSignature(
        "НаСервере", ("Отказ", "СтандартнаяОбработка")
    ),
    ("form", "OnOpen"): EventSignature("НаКлиенте", ("Отказ",)),
    ("form", "NotificationProcessing"): EventSignature(
        "НаКлиенте", ("ИмяСобытия", "Параметр", "Источник")
    ),
    ("form", "ExternalEvent"): EventSignature(
        "НаКлиенте", ("Источник", "Событие", "Данные")
    ),
    ("form", "FillCheckProcessingAtServer"): EventSignature(
        "НаСервере", ("Отказ", "ПроверяемыеРеквизиты")
    ),
    ("form", "BeforeClose"): EventSignature(
        "НаКлиенте",
        (
            "Отказ",
            "ЗавершениеРаботы",
            "ТекстПредупреждения",
            "СтандартнаяОбработка",
        ),
    ),
    ("form", "OnClose"): EventSignature("НаКлиенте", ("ЗавершениеРаботы",)),
    ("form", "ChoiceProcessing"): EventSignature(
        "НаКлиенте", ("ВыбранноеЗначение", "ИсточникВыбора")
    ),
    ("form", "OnReadAtServer"): EventSignature(
        "НаСервере", ("ТекущийОбъект",)
    ),
    ("form", "BeforeWrite"): EventSignature(
        "НаКлиенте", ("Отказ", "ПараметрыЗаписи")
    ),
    ("form", "BeforeWriteAtServer"): EventSignature(
        "НаСервере", ("Отказ", "ТекущийОбъект", "ПараметрыЗаписи")
    ),
    ("form", "OnWriteAtServer"): EventSignature(
        "НаСервере", ("Отказ", "ТекущийОбъект", "ПараметрыЗаписи")
    ),
    ("form", "AfterWriteAtServer"): EventSignature(
        "НаСервере", ("ТекущийОбъект", "ПараметрыЗаписи")
    ),
    ("form", "AfterWrite"): EventSignature(
        "НаКлиенте", ("ПараметрыЗаписи",)
    ),
    ("input_field", "OnChange"): EventSignature("НаКлиенте", ("Элемент",)),
    ("input_field", "StartChoice"): EventSignature(
        "НаКлиенте",
        ("Элемент", "ДанныеВыбора", "ВыборДобавлением", "СтандартнаяОбработка"),
    ),
    ("input_field", "Clearing"): EventSignature(
        "НаКлиенте", ("Элемент", "СтандартнаяОбработка")
    ),
    ("input_field", "ChoiceProcessing"): EventSignature(
        "НаКлиенте",
        (
            "Элемент",
            "ВыбранноеЗначение",
            "ДополнительныеДанные",
            "ВыборДобавлением",
            "СтандартнаяОбработка",
        ),
    ),
    ("input_field", "AutoComplete"): EventSignature(
        "НаКлиенте",
        (
            "Элемент",
            "Текст",
            "ДанныеВыбора",
            "ПараметрыПолученияДанных",
            "Ожидание",
            "СтандартнаяОбработка",
        ),
    ),
    ("input_field", "TextEditEnd"): EventSignature(
        "НаКлиенте",
        (
            "Элемент",
            "Текст",
            "ДанныеВыбора",
            "ПараметрыПолученияДанных",
            "СтандартнаяОбработка",
        ),
    ),
    ("input_field", "Opening"): EventSignature(
        "НаКлиенте", ("Элемент", "СтандартнаяОбработка")
    ),
    ("check_box_field", "OnChange"): EventSignature("НаКлиенте", ("Элемент",)),
    ("pages", "OnCurrentPageChange"): EventSignature(
        "НаКлиенте", ("Элемент", "ТекущаяСтраница")
    ),
    ("table", "Selection"): EventSignature(
        "НаКлиенте",
        ("Элемент", "ВыбраннаяСтрока", "Поле", "СтандартнаяОбработка"),
    ),
    ("table", "OnActivateRow"): EventSignature("НаКлиенте", ("Элемент",)),
    ("table", "ChoiceProcessing"): EventSignature(
        "НаКлиенте", ("Элемент", "ВыбранноеЗначение", "СтандартнаяОбработка")
    ),
    ("table", "OnStartEdit"): EventSignature(
        "НаКлиенте", ("Элемент", "НоваяСтрока", "Копирование")
    ),
    ("table", "BeforeAddRow"): EventSignature(
        "НаКлиенте",
        ("Элемент", "Отказ", "Копирование", "Родитель", "ЭтоГруппа", "Параметр"),
    ),
    ("table", "BeforeRowChange"): EventSignature(
        "НаКлиенте", ("Элемент", "Отказ")
    ),
    ("table", "BeforeDeleteRow"): EventSignature(
        "НаКлиенте", ("Элемент", "Отказ")
    ),
    ("table", "AfterDeleteRow"): EventSignature("НаКлиенте", ("Элемент",)),
    ("table", "OnChange"): EventSignature("НаКлиенте", ("Элемент",)),
    ("table", "OnEditEnd"): EventSignature(
        "НаКлиенте", ("Элемент", "НоваяСтрока", "ОтменаРедактирования")
    ),
}


OBJECT_FORM_EVENTS = frozenset(
    {
        "OnReadAtServer",
        "BeforeWrite",
        "BeforeWriteAtServer",
        "OnWriteAtServer",
        "AfterWriteAtServer",
        "AfterWrite",
    }
)


DOCUMENTED_8_3_5_EVENT_SIGNATURES: dict[tuple[str, str], EventSignature] = {
    ("form", "BeforeClose"): EventSignature(
        "НаКлиенте", ("Отказ", "СтандартнаяОбработка")
    ),
    ("form", "OnClose"): EventSignature("НаКлиенте", ()),
    ("form", "ChoiceProcessing"): EventSignature(
        "НаКлиенте", ("ВыбранноеЗначение", "ИсточникВыбора")
    ),
    ("input_field", "StartChoice"): EventSignature(
        "НаКлиенте", ("Элемент", "ДанныеВыбора", "СтандартнаяОбработка")
    ),
    ("input_field", "Clearing"): EventSignature(
        "НаКлиенте", ("Элемент", "СтандартнаяОбработка")
    ),
    ("input_field", "ChoiceProcessing"): EventSignature(
        "НаКлиенте", ("Элемент", "ВыбранноеЗначение", "СтандартнаяОбработка")
    ),
    ("input_field", "AutoComplete"): EventSignature(
        "НаКлиенте",
        (
            "Элемент",
            "Текст",
            "ДанныеВыбора",
            "Параметры",
            "Ожидание",
            "СтандартнаяОбработка",
        ),
    ),
    ("input_field", "TextEditEnd"): EventSignature(
        "НаКлиенте",
        ("Элемент", "Текст", "ДанныеВыбора", "Параметры", "СтандартнаяОбработка"),
    ),
    ("input_field", "Opening"): EventSignature(
        "НаКлиенте", ("Элемент", "СтандартнаяОбработка")
    ),
    ("table", "ChoiceProcessing"): EventSignature(
        "НаКлиенте", ("Элемент", "ВыбранноеЗначение", "СтандартнаяОбработка")
    ),
    ("table", "OnStartEdit"): EventSignature(
        "НаКлиенте", ("Элемент", "НоваяСтрока", "Копирование")
    ),
    ("table", "BeforeAddRow"): EventSignature(
        "НаКлиенте",
        ("Элемент", "Отказ", "Копирование", "Родитель", "ЭтоГруппа", "Параметр"),
    ),
    ("table", "BeforeRowChange"): EventSignature(
        "НаКлиенте", ("Элемент", "Отказ")
    ),
    ("table", "BeforeDeleteRow"): EventSignature(
        "НаКлиенте", ("Элемент", "Отказ")
    ),
    ("table", "AfterDeleteRow"): EventSignature("НаКлиенте", ("Элемент",)),
    ("table", "OnChange"): EventSignature("НаКлиенте", ("Элемент",)),
    ("table", "OnEditEnd"): EventSignature(
        "НаКлиенте", ("Элемент", "НоваяСтрока", "ОтменаРедактирования")
    ),
}

EVENT_PROFILES = {
    "modern": EVENT_SIGNATURES,
    # Старый профиль хранит только доказанные отличия сигнатур; остальные
    # события закрытого каталога используют общую сигнатуру.
    "8.3.5": {**EVENT_SIGNATURES, **DOCUMENTED_8_3_5_EVENT_SIGNATURES},
}


def event_signature(
    owner_kind: str,
    event: str,
    *,
    profile: str = "modern",
) -> EventSignature | None:
    catalog = EVENT_PROFILES.get(profile)
    if catalog is None:
        return None
    return catalog.get((owner_kind, event))
