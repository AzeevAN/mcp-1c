"""Тонкие MCP-обёртки read-only capability Metadata Authoring."""

from __future__ import annotations

import json

from ...capabilities import CapabilityTool
from .checker import check_metadata_artifacts
from .rules import RuleTopic, get_metadata_authoring_rules


def _json(payload: object) -> str:
    return json.dumps(payload, ensure_ascii=False, indent=2)


def _rules_tool(topic: RuleTopic = "overview") -> str:
    return _json(get_metadata_authoring_rules(topic))


def _check_tool(object_ref: str, artifacts: dict[str, str]) -> str:
    return _json(check_metadata_artifacts(object_ref, artifacts))


def load(registry=None) -> tuple[CapabilityTool, ...]:
    """Загрузить два pure-инструмента в обязательном порядке вызова."""

    del registry
    return (
        CapabilityTool(
            name="get_metadata_authoring_rules",
            function=_rules_tool,
            description=(
                "Сначала получите компактные правила создания метаданных. "
                "Поддержаны только Справочник и РегистрСведений; инструмент "
                "ничего не пишет, не импортирует в 1С и не обращается к Registry."
            ),
        ),
        CapabilityTool(
            name="check_metadata_artifacts",
            function=_check_tool,
            description=(
                "Read-only проверка переданных текстовых артефактов Справочника "
                "или РегистраСведений: descriptor, generated types, регистрацию, "
                "формы и XML namespaces. Ничего не пишет и не импортирует в 1С; "
                "успех статической проверки не доказывает нативный импорт. "
                "Перед вызовом получите правила через get_metadata_authoring_rules."
            ),
        ),
    )


__all__ = ["load"]
