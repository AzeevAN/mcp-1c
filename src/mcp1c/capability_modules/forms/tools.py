"""Тонкие MCP-обёртки Forms без предметной логики и внешних эффектов."""

from __future__ import annotations

import json

from ...capabilities import CapabilityTool
from .checker import check_managed_form
from .compiler import compile_managed_form
from .decompiler import decompile_managed_form
from .models import ManagedFormSpec
from .rules import RuleTopic, get_managed_form_rules


def _json(payload: object) -> str:
    return json.dumps(payload, ensure_ascii=False, indent=2)


def _rules_tool(topic: RuleTopic = "overview") -> str:
    return _json(get_managed_form_rules(topic))


def _compile_tool(specification: ManagedFormSpec) -> str:
    return _json(compile_managed_form(specification).to_dict())


def _decompile_tool(
    form_xml: str,
    form_name: str,
    module_bsl: str | None = None,
) -> str:
    return _json(
        decompile_managed_form(
            form_xml,
            form_name=form_name,
            module_bsl=module_bsl,
        ).to_dict()
    )


def _check_tool(
    form_xml: str,
    form_name: str,
    module_bsl: str | None = None,
) -> str:
    return _json(
        check_managed_form(
            form_xml,
            form_name=form_name,
            module_bsl=module_bsl,
        ).to_dict()
    )


def load() -> tuple[CapabilityTool, ...]:
    """Загрузить четыре чистых инструмента в принятом порядке работы."""

    return (
        CapabilityTool(
            name="get_managed_form_rules",
            function=_rules_tool,
            description=(
                "Получить один компактный раздел правил управляемых форм. "
                "Начните с topic=overview; инструмент не читает Registry или data/."
            ),
        ),
        CapabilityTool(
            name="compile_managed_form",
            function=_compile_tool,
            description=(
                "Детерминированно собрать Form.xml 2.16 и Form/Module.bsl из "
                "строгой спецификации первой вертикали. Возвращает текстовые "
                "артефакты, ничего не записывает и не выполняет импорт в 1С."
            ),
        ),
        CapabilityTool(
            name="decompile_managed_form",
            function=_decompile_tool,
            description=(
                "Разобрать Form.xml в каноническую спецификацию либо честный "
                "inventory со всеми непокрытыми XML-путями. Не используйте "
                "inventory для обратной компиляции: allow_lossy отсутствует."
            ),
        ),
        CapabilityTool(
            name="check_managed_form",
            function=_check_tool,
            description=(
                "Раздельно проверить XML, структуру, локальные ссылки и, если "
                "передан, Module.bsl. Статический результат не доказывает "
                "Registry, импорт или внешний вид формы в 1С."
            ),
        ),
    )
