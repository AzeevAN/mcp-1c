"""Локальный компилятор и checker метаданных без MCP runtime."""

from __future__ import annotations

import sys
from pathlib import Path

# Установленные skills располагаются рядом; form_core нужен для форм в объекте.
_form_scripts = Path(__file__).resolve().parents[3] / "1c-form-creator" / "scripts"
if not (_form_scripts / "form_core").is_dir():
    raise ImportError("Установите рядом skill 1c-form-creator: его form_core нужен для проверки форм метаданных")
if str(_form_scripts) not in sys.path:
    sys.path.insert(0, str(_form_scripts))

from .checker import check_metadata_artifacts
from .compiler import MetadataAuthoringContractError, compile_metadata_object
from .rules import get_metadata_authoring_rules
from .schema import METADATA_SPECIFICATION_SCHEMA

__all__ = [
    "MetadataAuthoringContractError",
    "METADATA_SPECIFICATION_SCHEMA",
    "check_metadata_artifacts",
    "compile_metadata_object",
    "get_metadata_authoring_rules",
]
