"""Изолированные ридеры физических форматов source B."""

from .forms import FormReadError, FormReadResult, read_form
from .modules import ModuleBodyResult, read_module_body

__all__ = [
    "FormReadError",
    "FormReadResult",
    "ModuleBodyResult",
    "read_form",
    "read_module_body",
]
