"""Предметный API opt-in capability управляемых форм."""

from __future__ import annotations

from .diagnostics import Artifact, Coverage, Diagnostic, FormsResult
from .models import FormsContractError, ManagedForm, ManagedFormSpec
from .checker import check_managed_form
from .compiler import compile_managed_form
from .decompiler import decompile_managed_form
from .rules import FormsRuleQueryError, RULE_TOPICS, get_managed_form_rules


def load(registry=None):
    """Лениво загрузить только транспортные обёртки capability."""

    from .tools import load as load_tools

    return load_tools(registry)


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
    "load",
]
