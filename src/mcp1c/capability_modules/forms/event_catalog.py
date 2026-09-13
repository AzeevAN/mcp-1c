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


def event_signature(owner_kind: str, event: str) -> EventSignature | None:
    return EVENT_SIGNATURES.get((owner_kind, event))
