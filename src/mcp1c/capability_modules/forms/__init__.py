"""Контракт opt-in capability управляемых форм.

Пакет пока не регистрируется в startup-каталоге: сначала фиксируются модели,
диагностика и RED-корпус, затем отдельными вертикалями добавляются операции.
"""

from .diagnostics import Artifact, Coverage, Diagnostic, FormsResult
from .models import FormsContractError, ManagedForm, ManagedFormSpec
from .checker import check_managed_form
from .compiler import compile_managed_form
from .decompiler import decompile_managed_form
from .rules import FormsRuleQueryError, RULE_TOPICS, get_managed_form_rules


__all__ = [
    "Artifact",
    "check_managed_form",
    "compile_managed_form",
    "Coverage",
    "Diagnostic",
    "decompile_managed_form",
    "FormsContractError",
    "FormsResult",
    "FormsRuleQueryError",
    "ManagedForm",
    "ManagedFormSpec",
    "RULE_TOPICS",
    "get_managed_form_rules",
]
