"""Безопасный демонстрационный модуль для проверки capability-шва."""

from __future__ import annotations

import json

from ..capabilities import CapabilityTool


def diagnostics_status() -> str:
    """Вернуть только статический статус самого демонстрационного модуля."""
    return json.dumps(
        {
            "capability": "diagnostics",
            "state": "ready",
            "data_access": False,
            "write_access": False,
        },
        ensure_ascii=False,
        indent=2,
    )


def load() -> tuple[CapabilityTool, ...]:
    """Инициализировать ровно один инструмент без Registry и файлов данных."""
    return (
        CapabilityTool(
            name="diagnostics_status",
            function=diagnostics_status,
            description=(
                "Проверить, что демонстрационный capability-модуль подключён. "
                "Возвращает только статический статус, не читает Registry, "
                "локальные проекты или data/ и не изменяет данные."
            ),
        ),
    )
