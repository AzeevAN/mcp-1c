"""Контракт opt-in capability управляемых форм.

Пакет пока не регистрируется в startup-каталоге: сначала фиксируются модели,
диагностика и RED-корпус, затем отдельными вертикалями добавляются операции.
"""

from .diagnostics import Artifact, Coverage, Diagnostic, FormsResult
from .models import FormsContractError, ManagedForm, ManagedFormSpec


__all__ = [
    "Artifact",
    "Coverage",
    "Diagnostic",
    "FormsContractError",
    "FormsResult",
    "ManagedForm",
    "ManagedFormSpec",
]
