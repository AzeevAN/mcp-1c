"""Тонкие MCP-обёртки Forms без предметной логики и внешних эффектов."""

from __future__ import annotations

import json

from ...capabilities import CapabilityTool
from .checker import check_managed_form
from .compiler import compile_managed_form
from .decompiler import MAX_FORM_XML_BYTES, decompile_managed_form
from .limits import (
    MAX_CONCURRENT_OPERATIONS,
    MAX_MODULE_BYTES,
    MAX_PENDING_OPERATIONS,
    MAX_RESULT_BYTES,
    MAX_SPECIFICATION_BYTES,
    FormsExecutionGate,
    FormsToolBusyError,
    FormsToolInputError,
    FormsToolResultError,
    FormsToolTimeoutError,
)
from .models import ManagedFormSpec
from .rules import RuleTopic, get_managed_form_rules


_GATE = FormsExecutionGate()


def _json(payload: object) -> str:
    result = json.dumps(payload, ensure_ascii=False, indent=2)
    if len(result.encode("utf-8")) > MAX_RESULT_BYTES:
        raise FormsToolResultError(
            f"Ответ Forms превышает лимит {MAX_RESULT_BYTES} байт."
        )
    return result


def _input(value: str, name: str, limit: int) -> None:
    if len(value.encode("utf-8")) > limit:
        raise FormsToolInputError(f"{name} превышает лимит {limit} байт.")


def _rules_tool(topic: RuleTopic = "overview") -> str:
    return _json(get_managed_form_rules(topic))


async def _compile_tool(specification: ManagedFormSpec) -> str:
    encoded = json.dumps(
        specification,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    if len(encoded) > MAX_SPECIFICATION_BYTES:
        raise FormsToolInputError(
            f"specification превышает лимит {MAX_SPECIFICATION_BYTES} байт."
        )
    return await _GATE.run(
        lambda: _json(compile_managed_form(specification).to_dict())
    )


async def _decompile_tool(
    form_xml: str,
    form_name: str,
    module_bsl: str | None = None,
    platform_version: str | None = None,
) -> str:
    _input(form_xml, "form_xml", MAX_FORM_XML_BYTES)
    if module_bsl is not None:
        _input(module_bsl, "module_bsl", MAX_MODULE_BYTES)
    return await _GATE.run(
        lambda: _json(
            decompile_managed_form(
                form_xml,
                form_name=form_name,
                module_bsl=module_bsl,
                platform_version=platform_version,
            ).to_dict()
        )
    )


async def _check_tool(
    form_xml: str,
    form_name: str,
    module_bsl: str | None = None,
    platform_version: str | None = None,
) -> str:
    _input(form_xml, "form_xml", MAX_FORM_XML_BYTES)
    if module_bsl is not None:
        _input(module_bsl, "module_bsl", MAX_MODULE_BYTES)
    return await _GATE.run(
        lambda: _json(
            check_managed_form(
                form_xml,
                form_name=form_name,
                module_bsl=module_bsl,
                platform_version=platform_version,
            ).to_dict()
        )
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
                "строгой спецификации поддержанного слоя. Если версия целевой "
                "платформы известна, агент задаёт platform_version; неизвестная "
                "или отсутствующая версия даёт предупреждение вместо отказа. Компоновка "
                "задаётся явно; compiler не переставляет элементы. Возвращает текстовые "
                "артефакты, ничего не записывает и не выполняет импорт в 1С. "
                "Размер specification ограничен 256 КиБ."
            ),
        ),
        CapabilityTool(
            name="decompile_managed_form",
            function=_decompile_tool,
            description=(
                "Разобрать Form.xml в каноническую спецификацию либо честный "
                "inventory со всеми непокрытыми XML-путями. Не используйте "
                "inventory для обратной компиляции: allow_lossy отсутствует. "
                "Передайте platform_version для выбора того же профиля событий; "
                "неподтверждённая версия будет явно помечена предупреждением. "
                "Form.xml и Module.bsl ограничены 2 МиБ каждый."
            ),
        ),
        CapabilityTool(
            name="check_managed_form",
            function=_check_tool,
            description=(
                "Раздельно проверить XML, структуру, локальные ссылки и, если "
                "передан, Module.bsl. Статический результат не доказывает "
                "Registry, импорт или внешний вид формы в 1С; platform_version "
                "выбирает версионный профиль событий и сообщает степень "
                "доказанности предупреждением. Form.xml и "
                "Module.bsl ограничены 2 МиБ каждый."
            ),
        ),
    )
