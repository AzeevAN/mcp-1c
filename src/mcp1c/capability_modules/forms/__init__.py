"""Контракт opt-in capability управляемых форм.

Пакет пока не регистрируется в startup-каталоге: сначала фиксируются модели,
диагностика и RED-корпус, затем отдельными вертикалями добавляются операции.
"""

from .diagnostics import Artifact, Coverage, Diagnostic, FormsResult
from .models import FormsContractError, ManagedForm, ManagedFormSpec
from .rules import FormsRuleQueryError, RULE_TOPICS, get_managed_form_rules


__all__ = [
    "Artifact",
    "Coverage",
    "Diagnostic",
    "FormsContractError",
    "FormsResult",
    "FormsRuleQueryError",
    "ManagedForm",
    "ManagedFormSpec",
    "RULE_TOPICS",
    "get_managed_form_rules",
]
