"""Тонкие MCP-обёртки read-only capability Metadata Authoring."""

from __future__ import annotations

import json

from ...capabilities import CapabilityTool
from .checker import check_metadata_artifacts
from .compiler import MetadataAuthoringContractError, compile_metadata_object
from .rules import RuleTopic, get_metadata_authoring_rules
from .schema import MetadataSpecification


def _json(payload: object) -> str:
    return json.dumps(payload, ensure_ascii=False, indent=2)


def _rules_tool(topic: RuleTopic = "overview") -> str:
    return _json(get_metadata_authoring_rules(topic))


def _check_tool(
    object_ref: str,
    format_version: str,
    artifacts: dict[str, str] | list[dict[str, str]],
) -> str:
    return _json(
        check_metadata_artifacts(
            object_ref,
            format_version,
            artifacts,
        )
    )


def _compile_tool(specification: MetadataSpecification) -> str:
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
                "Если планируются forms[], обязательно запросите topic=forms: "
                "он публикует обязательную цепочку Forms compile/check перед metadata compile. "
                "Поддержаны Справочник, базовый непроводимый Документ и РегистрСведений; инструмент "
                "ничего не пишет, не импортирует в 1С и не обращается к Registry."
            ),
        ),
        CapabilityTool(
            name="compile_metadata_object",
            function=_compile_tool,
            description=(
                "Если forms[] непуст, сначала обязательно запросите правила "
                "capability Forms и для каждой role выполните "
                "compile_managed_form, затем вызовите check_managed_form на тех же "
                "точных возвращённых строках; только после успеха программно "
                "передайте Form.xml/Module.bsl byte-for-byte и ту же role в forms[]. "
                "Не перепечатывайте, не пересказывайте, не сокращайте, не обрезайте "
                "и не реконструируйте XML; не используйте minimal_shape_reference. "
                "Metadata compile request размером в десятки KB является нормальным. "
                "Metadata checker не подтверждает provenance или semantic role "
                "Form.xml, поэтому ручной XML не является доказательством формы. "
                "Pure-компиляция закрытой specification schema v1 в текстовые "
                "артефакты Справочника, непроводимого Документа или РегистраСведений. Ничего не пишет, "
                "не импортирует в 1С и не читает Configuration.xml. "
                "Для Справочника и Документа доступны role=object/list/choice, "
                "для РегистраСведений role=record; role можно опустить для "
                "совместимости с прежним object/record-контрактом. "
                "format_version обязан передать caller. "
                "До первого вызова получите через get_metadata_authoring_rules "
                "темы overview, тему выбранного вида объекта и, если forms не "
                "пуст, artifacts."
            ),
        ),
        CapabilityTool(
            name="check_metadata_artifacts",
            function=_check_tool,
            description=(
                "Read-only проверка переданных текстовых артефактов Справочника, "
                "непроводимого Документа или РегистраСведений: descriptor, generated types, "
                "формы и XML namespaces. Ничего не пишет и не импортирует в 1С; "
                "успех статической проверки не доказывает нативный импорт. "
                "Checker проверяет owner-relative Default*Form, но не выводит "
                "semantic role из Form.xml: это делает check_managed_form. "
                "После compile_metadata_object передайте result.object_ref, "
                "result.format_version и result.artifacts. Configuration.xml "
                "не поддерживается; принимается только точный безопасный "
                "owner-relative комплект, без лишних файлов и сегментов . или ..; "
                "регистрацию объекта применяет caller. "
                "Старый словарь path → text тоже поддержан."
            ),
        ),
    )


__all__ = ["load"]
