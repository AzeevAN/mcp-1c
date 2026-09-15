"""Тонкие MCP-обёртки read-only capability Metadata Authoring."""

from __future__ import annotations

import json

from ...capabilities import CapabilityTool
from .checker import check_metadata_artifacts
from .compiler import MetadataAuthoringContractError, compile_metadata_object
from .rules import RuleTopic, get_metadata_authoring_rules


def _json(payload: object) -> str:
    return json.dumps(payload, ensure_ascii=False, indent=2)


def _rules_tool(topic: RuleTopic = "overview") -> str:
    return _json(get_metadata_authoring_rules(topic))


def _check_tool(object_ref: str, artifacts: dict[str, str]) -> str:
    return _json(check_metadata_artifacts(object_ref, artifacts))


def _compile_tool(specification: dict) -> str:
    try:
        return _json(compile_metadata_object(specification))
    except MetadataAuthoringContractError as error:
        return _json(
            {
                "status": "rejected",
                "diagnostics": error.diagnostics,
                "instructions": [
                    "Исправьте specification по diagnostics и повторите compile_metadata_object."
                ],
            }
        )


def load(registry=None) -> tuple[CapabilityTool, ...]:
    """Загрузить три pure-инструмента в обязательном порядке вызова."""

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
            name="compile_metadata_object",
            function=_compile_tool,
            description=(
                "Pure-компиляция закрытой specification schema v1 в текстовые "
                "артефакты Справочника или РегистраСведений. Ничего не пишет, "
                "не импортирует в 1С и не создаёт и не перезаписывает "
                "Configuration.xml; возвращает отдельную инструкцию регистрации. "
                "Сначала получите правила через get_metadata_authoring_rules."
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
                "Перед вызовом получите правила и скомпилируйте bundle через "
                "compile_metadata_object."
            ),
        ),
    )


__all__ = ["load"]
