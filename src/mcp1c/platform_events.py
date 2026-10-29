"""События, которые платформа вызывает в модуле объекта сама.

У `ОбработкаПроведения` в модуле документа нет ни одного вызова в коде: её
вызывает платформа, найдя процедуру по имени. Без пометки такая процедура
в оглавлении и в `get_callers` выглядит неиспользуемой.

Список событий — из справки: событие `ПередЗаписью` с родителем
`ДокументОбъект.<Имя документа>` относится к модулю объекта любого
документа. Свой каталог не заводим: справка знает и версию появления
события. Модули форм сюда не входят — их события назначаются явно, и для
обычных форм связь по имени процедуры не доказана
(`FORM-CONTAINER-EVENT-SEMANTICS`).
"""

from __future__ import annotations

from typing import Callable

from .syntax_model import SyntaxIndex, SyntaxItem

# Суффикс адреса модуля → окончание типа в справке.
_MODULE_TYPES = {
    "МодульОбъекта": "Объект",
    "МодульМенеджера": "Менеджер",
    "МодульНабораЗаписей": "НаборЗаписей",
}


def event_catalog(syntax: SyntaxIndex) -> dict[str, tuple[SyntaxItem, ...]]:
    """События справки по типу-родителю: `ДокументОбъект` → события.

    Строится один раз при загрузке справки: на вызов перебирать 25 тысяч
    элементов незачем.
    """
    catalog: dict[str, list[SyntaxItem]] = {}
    for item in syntax.items.values():
        if item.kind != "event" or ".<" not in item.parent_ru:
            continue
        owner = item.parent_ru.split(".", 1)[0]
        catalog.setdefault(owner, []).append(item)
    return {owner: tuple(items) for owner, items in catalog.items()}


def module_events(
    catalog: dict[str, tuple[SyntaxItem, ...]],
    module_address: str,
    keep: Callable[[SyntaxItem], bool],
) -> frozenset[str]:
    """Имена событий платформы для модуля `Вид.Имя.МодульОбъекта`."""
    parts = module_address.split(".")
    if len(parts) != 3:
        return frozenset()
    suffix = _MODULE_TYPES.get(parts[2])
    if suffix is None:
        return frozenset()
    return frozenset(
        item.name_ru for item in catalog.get(parts[0] + suffix, ()) if keep(item)
    )


def platform_events(
    syntax: SyntaxIndex | None,
    module_address: str,
    keep: Callable[[SyntaxItem], bool],
) -> frozenset[str]:
    """То же по справке целиком; без справки событий не знаем — пусто."""
    if syntax is None:
        return frozenset()
    return module_events(event_catalog(syntax), module_address, keep)
