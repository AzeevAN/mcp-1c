"""Pure API capability статической проверки артефактов метаданных."""

from __future__ import annotations

from .checker import check_metadata_artifacts
from .compiler import MetadataAuthoringContractError, compile_metadata_object
from .rules import RULE_TOPICS, get_metadata_authoring_rules


def load(registry=None):
    """Лениво загрузить только транспортные MCP-обёртки."""

    from .tools import load as load_tools

    return load_tools(registry)


__all__ = [
    "RULE_TOPICS",
    "MetadataAuthoringContractError",
    "check_metadata_artifacts",
    "compile_metadata_object",
    "get_metadata_authoring_rules",
    "load",
]
