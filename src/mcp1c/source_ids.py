"""Формулы идентификаторов источников кода.

Идентификатор источника — часть контракта на диске (`registry.json`, имена
кэшей, журналы покрытия) и в HTTP-ответах дашборда. Раньше каждая формула
жила строковым литералом в полутора десятках мест: правка одного места
молча расходилась с остальными. Здесь они собраны в одну точку; модуль не
зависит ни от чего, чтобы его можно было звать откуда угодно без циклов
импорта.

Формы:

- `<Конфигурация>` — сама конфигурация (структура);
- `<Конфигурация>:modules` — корпус кода основной конфигурации;
- `<Конфигурация>:ext:<Ключ>` — корпус кода расширения; ключ — имя
  расширения после `safe_names.safe_name`, чистку делает вызывающий.
"""

from __future__ import annotations

MODULES_SUFFIX = ":modules"
EXTENSION_SEPARATOR = ":ext:"


def modules_id(configuration: str) -> str:
    """Id корпуса кода основной конфигурации."""
    return f"{configuration}{MODULES_SUFFIX}"


def extension_prefix(configuration: str) -> str:
    """Общий префикс id корпусов кода расширений конфигурации."""
    return f"{configuration}{EXTENSION_SEPARATOR}"


def extension_modules_id(configuration: str, extension_key: str) -> str:
    """Id корпуса кода расширения по уже очищенному ключу расширения."""
    return extension_prefix(configuration) + extension_key


def configuration_of_modules(source_id: str) -> str:
    """Имя конфигурации из id `<Имя>:modules`; другой id возвращается как есть."""
    return source_id.removesuffix(MODULES_SUFFIX)


def is_code_source_id(source_id: str) -> bool:
    """Id называет корпус кода: основной конфигурации или расширения."""
    return source_id.endswith(MODULES_SUFFIX) or EXTENSION_SEPARATOR in source_id
