"""Маркер startup-capability для встроенных role-tools.

Сами tools замыкают Registry и регистрируются server.py, поэтому модуль не
дублирует их. Непустой capability-маркер нужен, чтобы role_access участвовал в
едином закрытом каталоге настроек и проходил общий loader-контракт.
"""

from __future__ import annotations

from mcp1c.capabilities import CapabilityTool


def load() -> tuple[CapabilityTool, ...]:
    return ()
