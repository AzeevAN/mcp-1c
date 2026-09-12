"""Компактные правила первой вертикали, загружаемые только по запросу.

Наблюдения обезличены: реальный корпус доказывает форму правила, но не входит
ни в пакет, ни в ответ инструмента. Уровень ``corpus_invariant`` означает
инвариант доступного корпуса, а не обещание нативного импорта платформой.
"""

from __future__ import annotations

import copy
from dataclasses import dataclass
from typing import Literal, TypeAlias


RuleTopic: TypeAlias = Literal[
    "overview",
    "specification",
    "elements",
    "attributes",
    "commands_events",
    "diagnostics",
]
RuleStatus: TypeAlias = Literal["required", "supported", "boundary"]
EvidenceLevel: TypeAlias = Literal[
    "corpus_invariant",
    "observed_pattern",
    "contract_decision",
]

RULE_TOPICS: tuple[RuleTopic, ...] = (
    "overview",
    "specification",
    "elements",
    "attributes",
    "commands_events",
    "diagnostics",
)


class FormsRuleQueryError(ValueError):
    """Запрошена тема вне закрытого набора первой вертикали."""


@dataclass(frozen=True, slots=True)
class FormRule:
    code: str
    status: RuleStatus
    summary: str
    evidence_level: EvidenceLevel
    value: object | None = None

    def to_dict(self) -> dict[str, object]:
        result: dict[str, object] = {
            "code": self.code,
            "status": self.status,
            "summary": self.summary,
            "evidence_level": self.evidence_level,
        }
        if self.value is not None:
            # Ответ принадлежит вызывающему: его правка не должна менять
            # следующий запрос к неизменяемому реестру правил.
            result["value"] = copy.deepcopy(self.value)
        return result


_RULES: dict[RuleTopic, tuple[FormRule, ...]] = {
    "overview": (
        FormRule(
            "managed_configurator_files_only",
            "required",
            "Поддержаны только текстовые Ext/Form.xml и Ext/Form/Module.bsl.",
            "contract_decision",
        ),
        FormRule(
            "explicit_format_version",
            "required",
            "Версия формата обязательна; compiler первой вертикали принимает 2.16.",
            "corpus_invariant",
            "2.16",
        ),
        FormRule(
            "text_result_without_write",
            "required",
            "Результат возвращается как текст и не записывается в проект или 1С.",
            "contract_decision",
        ),
        FormRule(
            "recommended_call_order",
            "supported",
            "Сначала правила, затем compile, check и decompile-roundtrip.",
            "contract_decision",
            [
                "get_managed_form_rules",
                "compile_managed_form",
                "check_managed_form",
                "decompile_managed_form",
            ],
        ),
    ),
    "specification": (
        FormRule(
            "schema_version_one",
            "required",
            "Спецификация использует schema_version=1.",
            "contract_decision",
            1,
        ),
        FormRule(
            "unknown_keys_rejected",
            "required",
            "Неизвестные ключи отклоняются до генерации.",
            "contract_decision",
        ),
        FormRule(
            "caller_ids_forbidden",
            "required",
            "ID не входят в публичную спецификацию и назначаются детерминированно.",
            "contract_decision",
        ),
        FormRule(
            "identifier_syntax",
            "required",
            "Имена используют буквы, цифры и подчёркивание и не начинаются с цифры.",
            "contract_decision",
        ),
    ),
    "elements": (
        FormRule(
            "root_usual_group",
            "supported",
            "На верхнем уровне первой вертикали поддержан usual_group.",
            "observed_pattern",
        ),
        FormRule(
            "group_children",
            "supported",
            "Внутри группы поддержаны input_field и button.",
            "observed_pattern",
            ["input_field", "button"],
        ),
        FormRule(
            "input_field_companions",
            "required",
            "InputField получает наблюдаемые служебные дочерние элементы.",
            "corpus_invariant",
            ["ContextMenu", "ExtendedTooltip"],
        ),
        FormRule(
            "button_companions",
            "required",
            "Button получает наблюдаемый ExtendedTooltip.",
            "corpus_invariant",
            ["ExtendedTooltip"],
        ),
        FormRule(
            "separate_id_spaces",
            "required",
            "Элементы, реквизиты и команды используют раздельные пространства ID.",
            "corpus_invariant",
        ),
        FormRule(
            "other_elements_unsupported",
            "boundary",
            "Остальные kind требуют отдельного доказательного fixture.",
            "contract_decision",
        ),
    ),
    "attributes": (
        FormRule(
            "string_attribute",
            "supported",
            "Первая вертикаль поддерживает строковый реквизит с положительной длиной.",
            "observed_pattern",
            {"kind": "string", "length": "positive_integer"},
        ),
        FormRule(
            "main_attribute_optional",
            "required",
            "Главных реквизитов может быть ноль или один, но не два.",
            "corpus_invariant",
        ),
        FormRule(
            "simple_data_path",
            "required",
            "DataPath input_field должен точно совпасть с именем реквизита формы.",
            "contract_decision",
        ),
        FormRule(
            "special_data_path_not_checked",
            "boundary",
            "Items.*, индексированные, служебные и непрозрачные пути не поддержаны.",
            "corpus_invariant",
        ),
        FormRule(
            "other_attribute_types_unsupported",
            "boundary",
            "Составные, ссылочные и иные типы требуют отдельных fixtures.",
            "contract_decision",
        ),
    ),
    "commands_events": (
        FormRule(
            "custom_command_reference",
            "required",
            "Кнопка ссылается на существующую команду через Form.Command.<name>.",
            "corpus_invariant",
            "Form.Command.<name>",
        ),
        FormRule(
            "command_action_stub",
            "supported",
            "Обычный Action команды получает клиентскую процедуру.",
            "observed_pattern",
            {"directive": "&НаКлиенте", "parameters": ["Команда"]},
        ),
        FormRule(
            "on_create_at_server_stub",
            "supported",
            "OnCreateAtServer получает доказанный серверный каркас.",
            "observed_pattern",
            {
                "directive": "&НаСервере",
                "parameters": ["Отказ", "СтандартнаяОбработка"],
            },
        ),
        FormRule(
            "unknown_event_not_checked",
            "boundary",
            "Иное событие не получает угаданную сигнатуру и остаётся not_checked.",
            "contract_decision",
        ),
        FormRule(
            "missing_action_not_structural_error",
            "boundary",
            "Отсутствующий Action в произвольной форме не является общей XML-ошибкой.",
            "corpus_invariant",
        ),
    ),
    "diagnostics": (
        FormRule(
            "separate_coverage_levels",
            "required",
            "Каждый уровень проверки возвращается отдельно.",
            "contract_decision",
            [
                "xml_parse",
                "structural",
                "configuration_links",
                "bsl_static",
                "platform_import",
                "runtime_visual",
            ],
        ),
        FormRule(
            "no_blanket_valid",
            "required",
            "Общее valid не подменяет сведения о непроверенных уровнях.",
            "contract_decision",
        ),
        FormRule(
            "not_checked_has_reason",
            "required",
            "Каждый not_checked сопровождается диагностикой с причиной.",
            "contract_decision",
        ),
        FormRule(
            "static_does_not_prove_native",
            "boundary",
            "Статический GREEN не доказывает импорт или внешний вид в 1С.",
            "contract_decision",
        ),
    ),
}


def _minimal_example() -> dict[str, object]:
    return {
        "schema_version": 1,
        "form_name": "ФормаПараметров",
        "format_version": "2.16",
        "title": {"ru": "Параметры"},
        "attributes": [
            {
                "name": "ПервоеЗначение",
                "type": {"kind": "string", "length": 100},
            },
            {
                "name": "ВтороеЗначение",
                "type": {"kind": "string", "length": 100},
            },
        ],
        "elements": [
            {
                "kind": "usual_group",
                "name": "ГруппаПараметров",
                "title": {"ru": "Параметры"},
                "children": [
                    {
                        "kind": "input_field",
                        "name": "ПервоеЗначение",
                        "data_path": "ПервоеЗначение",
                    },
                    {
                        "kind": "input_field",
                        "name": "ВтороеЗначение",
                        "data_path": "ВтороеЗначение",
                    },
                    {
                        "kind": "button",
                        "name": "Выполнить",
                        "command": "Выполнить",
                        "default": True,
                    },
                ],
            }
        ],
        "commands": [
            {
                "name": "Выполнить",
                "title": {"ru": "Выполнить"},
                "action": "Выполнить",
            }
        ],
        "events": [
            {
                "event": "OnCreateAtServer",
                "handler": "ПриСозданииНаСервере",
            }
        ],
    }


def get_managed_form_rules(topic: RuleTopic = "overview") -> dict[str, object]:
    """Вернуть один bounded-раздел правил без чтения файлов или Registry."""

    if topic not in RULE_TOPICS:
        raise FormsRuleQueryError(
            "Неизвестная тема. Доступны: " + ", ".join(RULE_TOPICS) + "."
        )

    payload: dict[str, object] = {
        "schema_version": 1,
        "topic": topic,
        "target": {
            "format": "configurator_xml",
            "compiler_versions": ["2.16"],
            "other_versions": "inventory_only",
        },
        "evidence": {
            "kind": "anonymized_corpus",
            "scope": "multiple_configurations",
            "platform_requirement": "not_proven",
        },
        "coverage": {
            "rules": "checked",
            "platform_import": "not_checked",
            "runtime_visual": "not_checked",
        },
        "rules": [rule.to_dict() for rule in _RULES[topic]],
    }
    if topic == "specification":
        payload["example"] = _minimal_example()
    elif topic == "elements":
        payload["supported"] = {
            "root": ["usual_group"],
            "group_children": ["input_field", "button"],
        }
    elif topic == "commands_events":
        payload["supported"] = {
            "form_events": ["OnCreateAtServer"],
            "command_reference": "Form.Command.<name>",
        }
    return payload


__all__ = [
    "EvidenceLevel",
    "FormRule",
    "FormsRuleQueryError",
    "RULE_TOPICS",
    "RuleStatus",
    "RuleTopic",
    "get_managed_form_rules",
]
