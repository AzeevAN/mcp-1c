"""Совместимый публичный вход к модульным ридерам форм."""

from .readers.forms import FormReadError, FormReadResult, KNOWN_MARKERS, read_form
from .readers.list_stream import MAX_BYTES, MAX_DEPTH, MAX_TOKENS

__all__ = [
    "FormReadError",
    "FormReadResult",
    "KNOWN_MARKERS",
    "MAX_BYTES",
    "MAX_DEPTH",
    "MAX_TOKENS",
    "read_form",
]
