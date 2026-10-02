"""Предметный API локального компилятора управляемых форм."""

from __future__ import annotations

from .diagnostics import Artifact, Coverage, Diagnostic, FormsResult
from .models import FormsContractError, ManagedForm, ManagedFormSpec
from .checker import check_managed_form
from .compiler import compile_managed_form
from .decompiler import decompile_managed_form


__all__ = [
    "Artifact",
    "check_managed_form",
    "compile_managed_form",
    "Coverage",
    "Diagnostic",
    "decompile_managed_form",
    "FormsContractError",
    "FormsResult",
    "ManagedForm",
    "ManagedFormSpec",
]
