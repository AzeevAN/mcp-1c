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
    ("input_field", "OnChange"): EventSignature("НаКлиенте", ("Элемент",)),
    ("check_box_field", "OnChange"): EventSignature("НаКлиенте", ("Элемент",)),
    ("pages", "OnCurrentPageChange"): EventSignature(
        "НаКлиенте", ("Элемент", "ТекущаяСтраница")
    ),
    ("table", "Selection"): EventSignature(
        "НаКлиенте",
        ("Элемент", "ВыбраннаяСтрока", "Поле", "СтандартнаяОбработка"),
    ),
    ("table", "OnActivateRow"): EventSignature("НаКлиенте", ("Элемент",)),
}


def event_signature(owner_kind: str, event: str) -> EventSignature | None:
    return EVENT_SIGNATURES.get((owner_kind, event))
