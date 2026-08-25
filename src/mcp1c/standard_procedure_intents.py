"""Осторожное распознавание типовых событий 1С в пользовательской фразе.

Каталог намеренно мал и глобален для процесса. Он переводит только
однозначную типовую формулировку в каноническое имя процедуры; адрес модуля
здесь не выбирается и пользовательские соглашения сюда не попадают.
"""

from __future__ import annotations

from dataclasses import dataclass

from .search import normalize, tokenize


@dataclass(frozen=True, slots=True)
class StandardProcedureIntent:
    name: str
    # OR из шаблонов, шаблон — AND из групп, группа — OR маркеров.
    # Маркер с ``=`` сравнивается целиком, остальные — как начала слов.
    patterns: tuple[tuple[tuple[str, ...], ...], ...]
    priority: int = 0


STANDARD_PROCEDURE_INTENTS: tuple[StandardProcedureIntent, ...] = (
    StandardProcedureIntent(
        "ОбработкаПроверкиЗаполнения",
        ((
            ("провер", "контрол"),
            ("заполн", "обязательн", "реквизит"),
            ("объект", "документ", "обработ", "событ"),
        ),),
        priority=20,
    ),
    StandardProcedureIntent(
        "ОбработкаПроведения",
        (
            (
                ("провед", "провест", "провод"),
                ("документ",),
                ("движен", "формир", "обработ", "код", "логик", "=где", "=что"),
            ),
            (("проведен",), ("движен", "регистр")),
        ),
    ),
    StandardProcedureIntent(
        "ПередЗаписью",
        ((
            ("=перед", "=до"),
            ("запи", "сохран"),
            ("объект", "документ", "обработ", "событ", "логик"),
        ),),
    ),
    StandardProcedureIntent(
        "ПриЗаписи",
        ((
            ("=при", "=во"),
            ("запи", "сохран"),
            ("объект", "документ", "обработ", "событ", "логик"),
        ),),
    ),
    StandardProcedureIntent(
        "ОбработкаУдаленияПроведения",
        ((
            ("отмен", "удал", "распров"),
            ("провед", "провест", "движен", "распров"),
            ("документ", "обработ", "событ"),
        ),),
        priority=20,
    ),
    StandardProcedureIntent(
        "ОбработкаЗаполнения",
        ((
            ("заполн",),
            ("нов", "объект", "документ", "основан", "создан"),
            ("обработ", "объект", "документ"),
        ),),
    ),
    StandardProcedureIntent(
        "ПриОткрытии",
        ((("=при", "=после"), ("откры",), ("форм",)),),
    ),
    StandardProcedureIntent(
        "ПередОткрытием",
        ((("=перед", "=до"), ("откры",), ("форм",)),),
    ),
    StandardProcedureIntent(
        "ПриСозданииНаСервере",
        ((("созда", "инициал"), ("сервер",), ("форм",)),),
    ),
    StandardProcedureIntent(
        "ПриЗакрытии",
        ((("=при", "=после"), ("закры",), ("форм",)),),
    ),
    StandardProcedureIntent(
        "ОбработкаВыбора",
        ((
            ("выбор", "выбр", "выбир"),
            ("значен", "поле", "форм"),
            ("обработ", "перехват", "=при"),
        ),),
    ),
    StandardProcedureIntent(
        "ОбработкаОповещения",
        ((
            ("оповещ", "уведом"),
            ("форм",),
            ("обработ", "получ", "вызыва", "=при"),
        ),),
    ),
)


_BY_NORMALIZED_NAME = {
    normalize(intent.name).replace(" ", ""): intent
    for intent in STANDARD_PROCEDURE_INTENTS
}


def _group_matches(tokens: set[str], group: tuple[str, ...]) -> bool:
    for marker in group:
        if marker.startswith("="):
            if marker[1:] in tokens:
                return True
        elif any(token.startswith(marker) for token in tokens):
            return True
    return False


def recognize_standard_procedure_intent(query: str) -> str | None:
    """Вернуть одно типовое имя либо отказаться при пустоте или ничьей."""
    exact = _BY_NORMALIZED_NAME.get(normalize(query).replace(" ", ""))
    if exact is not None:
        return exact.name

    tokens = set(tokenize(query))
    candidates: list[tuple[int, int, str]] = []
    for intent in STANDARD_PROCEDURE_INTENTS:
        specificity = max(
            (
                len(pattern)
                for pattern in intent.patterns
                if all(_group_matches(tokens, group) for group in pattern)
            ),
            default=0,
        )
        if specificity:
            candidates.append((intent.priority, specificity, intent.name))
    candidates.sort(reverse=True)
    if not candidates:
        return None
    if len(candidates) > 1 and candidates[0][:2] == candidates[1][:2]:
        return None
    return candidates[0][2]
