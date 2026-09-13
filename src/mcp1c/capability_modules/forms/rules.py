"""Компактные правила первой вертикали, загружаемые только по запросу.

Наблюдения обезличены: реальный корпус доказывает форму правила, но не входит
ни в пакет, ни в ответ инструмента. Уровень ``corpus_invariant`` означает
инвариант доступного корпуса, а не обещание нативного импорта платформой.
"""

from __future__ import annotations

import copy
from dataclasses import dataclass
from typing import Literal, TypeAlias

from .metadata_types import (
    DYNAMIC_LIST_KINDS,
    METADATA_OBJECT_KINDS,
    METADATA_REFERENCE_KINDS,
)
from .terminology import MAX_TERM_QUERY_LENGTH, all_form_terms, search_form_terms
from .version_catalog import platform_profiles_payload


RuleTopic: TypeAlias = Literal[
    "overview",
    "terminology",
    "specification",
    "elements",
    "attributes",
    "layout",
    "commands_events",
    "diagnostics",
]
RuleStatus: TypeAlias = Literal["required", "supported", "boundary"]
EvidenceLevel: TypeAlias = Literal[
    "corpus_invariant",
    "observed_pattern",
    "contract_decision",
    "platform_documentation",
]

RULE_TOPICS: tuple[RuleTopic, ...] = (
    "overview",
    "terminology",
    "specification",
    "elements",
    "attributes",
    "layout",
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
            (
                "Версия формата обязательна и должна совпадать с version корня "
                "Configuration.xml целевой выгрузки. Форматы 2.16 и 2.20 "
                "подтверждены корпусом; другая числовая версия сохраняется буквально "
                "с предупреждением до нативного импорта."
            ),
            "contract_decision",
            {
                "confirmed": ["2.16", "2.20"],
                "other": "compiled_with_warning",
                "selection": "match_target_configuration_root_version",
            },
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
    "terminology": (
        FormRule(
            "canonical_keys_only",
            "required",
            (
                "Русские и английские термины используются только для поиска; "
                "в specification всегда передаётся найденный canonical-ключ."
            ),
            "contract_decision",
        ),
        FormRule(
            "terminology_required_for_new_contracts",
            "required",
            (
                "Каждый новый элемент или свойство Forms добавляется вместе "
                "с русским и английским поисковым соответствием."
            ),
            "contract_decision",
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
            "Имена используют буквы, цифры и подчёркивание, не начинаются с цифры и не совпадают с русскими или английскими зарезервированными словами BSL.",
            "contract_decision",
        ),
    ),
    "elements": (
        FormRule(
            "recursive_element_tree",
            "supported",
            "Элементы образуют явное рекурсивное дерево; compiler сохраняет заданный агентом порядок.",
            "observed_pattern",
        ),
        FormRule(
            "supported_basic_elements",
            "supported",
            (
                "Базовый authoring-слой поддерживает группы, поля ввода, "
                "флажки, статические и связанные с данными надписи, кнопки, "
                "страницы и таблицы."
            ),
            "observed_pattern",
            [
                "usual_group",
                "input_field",
                "check_box_field",
                "label_decoration",
                "label_field",
                "radio_button_field",
                "button",
                "command_bar",
                "pages",
                "table",
            ],
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
            "check_box_requires_boolean",
            "required",
            "CheckBoxField ссылается только на boolean-реквизит и получает наблюдаемые служебные элементы.",
            "observed_pattern",
            ["ContextMenu", "ExtendedTooltip"],
        ),
        FormRule(
            "label_decoration_companions",
            "required",
            "LabelDecoration получает наблюдаемые служебные дочерние элементы.",
            "corpus_invariant",
            ["ContextMenu", "ExtendedTooltip"],
        ),
        FormRule(
            "label_field_companions",
            "required",
            "LabelField получает наблюдаемые служебные дочерние элементы.",
            "corpus_invariant",
            ["ContextMenu", "ExtendedTooltip"],
        ),
        FormRule(
            "radio_button_field_structure",
            "required",
            (
                "RadioButtonField требует DataPath, минимум два ChoiceList "
                "и служебные элементы."
            ),
            "corpus_invariant",
            [
                "DataPath",
                "RadioButtonType",
                "ChoiceList",
                "ContextMenu",
                "ExtendedTooltip",
            ],
        ),
        FormRule(
            "command_bar_structure",
            "required",
            (
                "CommandBar без CommandSource содержит одну или более кнопок, "
                "подменю либо групп кнопок; панель всегда получает наблюдаемую "
                "расширенную подсказку."
            ),
            "observed_pattern",
            ["ExtendedTooltip", "ChildItems<Button|Popup|ButtonGroup>"],
        ),
        FormRule(
            "popup_structure",
            "required",
            (
                "Popup имеет локализованный заголовок и расширенную подсказку; "
                "без CommandSource нужны кнопки либо группы кнопок."
            ),
            "observed_pattern",
            ["Title", "ExtendedTooltip", "ChildItems<Button|ButtonGroup>"],
        ),
        FormRule(
            "button_group_structure",
            "required",
            (
                "ButtonGroup получает расширенную подсказку; без "
                "CommandSource нужна одна или более прямых кнопок."
            ),
            "observed_pattern",
            ["ExtendedTooltip", "ChildItems<Button>"],
        ),
        FormRule(
            "command_source_structure",
            "supported",
            (
                "CommandSource автоматически добавляет команды формы, "
                "глобальные команды панели формы либо команды существующего "
                "реквизита или элемента; явные кнопки можно добавлять после них."
            ),
            "platform_documentation",
            {
                "containers": ["command_bar", "popup", "button_group"],
                "kinds": ["form", "form_global_commands", "item"],
                "item_target": "existing_attribute_or_element",
                "children": "optional_with_command_source",
            },
        ),
        FormRule(
            "separate_id_spaces",
            "required",
            "Элементы, реквизиты и команды используют раздельные пространства ID.",
            "corpus_invariant",
        ),
        FormRule(
            "table_companions",
            "required",
            "Table получает наблюдаемый набор служебных дочерних элементов.",
            "corpus_invariant",
            [
                "ContextMenu",
                "AutoCommandBar",
                "ExtendedTooltip",
                "SearchStringAddition",
                "ViewStatusAddition",
                "SearchControlAddition",
            ],
        ),
        FormRule(
            "auto_command_bar_structure",
            "supported",
            (
                "Table всегда получает AutoCommandBar; по умолчанию платформа "
                "заполняет его сама, а явный слой может отключить Autofill и "
                "добавить кнопки, подменю или группы кнопок."
            ),
            "observed_pattern",
            {
                "container": "table",
                "autofill": "default_true_explicit_false",
                "children": ["button", "popup", "button_group"],
                "own_events": False,
            },
        ),
        FormRule(
            "table_context_menu_structure",
            "supported",
            (
                "Table всегда получает ContextMenu; по умолчанию платформа "
                "заполняет его сама, а явный слой может отключить Autofill и "
                "добавить кнопки, подменю или группы кнопок."
            ),
            "observed_pattern",
            {
                "container": "table",
                "autofill": "default_true_explicit_false",
                "children": ["button", "popup", "button_group"],
            },
        ),
        FormRule(
            "specialized_elements_boundary",
            "boundary",
            "Специализированные поля документов, диаграмм и схем пока читаются только как inventory.",
            "contract_decision",
        ),
    ),
    "attributes": (
        FormRule(
            "basic_attribute_types",
            "supported",
            "Базовый слой поддерживает скалярные, ссылочные и составные типы, таблицу значений и одиночный объект метаданных.",
            "observed_pattern",
            [
                "string",
                "boolean",
                "number",
                "date",
                "value_table",
                "metadata_object",
                "metadata_reference",
                "composite",
                "dynamic_list",
            ],
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
            "Обычное поле ссылается на реквизит; объектный реквизит допускает путь Объект.Поле; колонка таблицы значений — РеквизитТаблицы.Колонка.",
            "contract_decision",
        ),
        FormRule(
            "special_data_path_not_checked",
            "boundary",
            "Items.*, индексированные, служебные и непрозрачные пути не поддержаны.",
            "corpus_invariant",
        ),
        FormRule(
            "registry_types_boundary",
            "boundary",
            "При доступном Registry-контексте проверяются объект, вложенное поле, ссылочные варианты, основная таблица DynamicList и доказуемые ограничения типа; без контекста генерация продолжается с предупреждением.",
            "contract_decision",
        ),
        FormRule(
            "registry_reference_formats",
            "required",
            (
                "Object и main_table используют каноническую ссылку Registry "
                "ВидМетаданных.Имя; допустимые виды различаются по типу."
            ),
            "contract_decision",
            {
                "metadata_object": {
                    "format": "ВидМетаданных.Имя",
                    "kinds": list(METADATA_OBJECT_KINDS),
                },
                "metadata_reference": {
                    "format": "ВидМетаданных.Имя",
                    "kinds": list(METADATA_REFERENCE_KINDS),
                },
                "dynamic_list": {
                    "format": "ВидМетаданных.Имя",
                    "kinds": list(DYNAMIC_LIST_KINDS),
                },
            },
        ),
    ),
    "layout": (
        FormRule(
            "agent_owns_semantic_layout",
            "required",
            "Агент выбирает страницы, группы, порядок и свойства из требований пользователя.",
            "contract_decision",
        ),
        FormRule(
            "compiler_preserves_layout",
            "required",
            "Compiler не переставляет элементы и не угадывает дизайн.",
            "contract_decision",
        ),
        FormRule(
            "managed_layout_not_coordinates",
            "supported",
            "Компоновка задаётся деревом, порядком и свойствами, а не пиксельными координатами.",
            "corpus_invariant",
        ),
        FormRule(
            "supported_layout_properties",
            "supported",
            "Агент явно задаёт ориентацию и представление группы, видимость её заголовка и растяжение крупных областей.",
            "observed_pattern",
            {
                "group_orientation": [
                    "vertical",
                    "horizontal",
                    "always_horizontal",
                ],
                "group_representation": [
                    "none",
                    "normal_separation",
                    "strong_separation",
                ],
                "boolean": [
                    "show_title",
                    "horizontal_stretch",
                    "vertical_stretch",
                ],
            },
        ),
        FormRule(
            "group_related_controls",
            "supported",
            "Связанные входы и действия размещайте в одной группе; вторичные параметры выносите на страницу настроек.",
            "observed_pattern",
        ),
        FormRule(
            "pages_for_parallel_contexts",
            "supported",
            "Pages используйте для параллельных областей, между которыми пользователь переключается.",
            "observed_pattern",
        ),
        FormRule(
            "table_columns_match_type",
            "required",
            "Каждая визуальная колонка Table должна ссылаться на объявленную колонку value_table.",
            "contract_decision",
        ),
        FormRule(
            "visual_acceptance_required",
            "boundary",
            "Правила компоновки не заменяют открытие формы и пользовательскую визуальную приёмку.",
            "contract_decision",
        ),
    ),
    "commands_events": (
        FormRule(
            "custom_command_reference",
            "required",
            (
                "В JSON-свойстве button.command передавайте только имя "
                "существующей команды без префикса; compiler сам создаёт "
                "XML-ссылку Form.Command.<name>."
            ),
            "corpus_invariant",
            {
                "specification": "<name>",
                "generated_xml": "Form.Command.<name>",
            },
        ),
        FormRule(
            "command_action_stub",
            "supported",
            "Обычный Action команды получает клиентскую процедуру.",
            "observed_pattern",
            {"directive": "&НаКлиенте", "parameters": ["Команда"]},
        ),
        FormRule(
            "standard_command_catalog",
            "supported",
            (
                "Стандартная команда не создаёт пользовательский Action и "
                "разрешается только для доказанного вида владельца."
            ),
            "observed_pattern",
            {
                "form": ["Help", "Close", "CustomizeForm"],
                "object_form": ["Write", "WriteAndClose"],
                "document_form": ["Post", "PostAndClose", "UndoPosting"],
                "table": ["Add", "Delete", "MoveUp", "MoveDown"],
            },
        ),
        FormRule(
            "no_synchronous_file_exists_on_client",
            "required",
            (
                "Не вызывайте Файл.Существует() в клиентском контексте "
                "управляемого приложения: это запрещённый синхронный метод. "
                "Обрабатывайте ошибку чтения либо используйте асинхронный API "
                "платформы."
            ),
            "contract_decision",
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
            "owner_aware_event_catalog",
            "supported",
            "Событие связывается с формой либо с точным именем элемента.",
            "observed_pattern",
            {
                "form": [
                    "OnCreateAtServer",
                    "OnOpen",
                    "NotificationProcessing",
                    "ExternalEvent",
                    "FillCheckProcessingAtServer",
                    "BeforeClose",
                    "OnClose",
                    "ChoiceProcessing",
                ],
                "object_form": [
                    "OnReadAtServer",
                    "BeforeWrite",
                    "BeforeWriteAtServer",
                    "OnWriteAtServer",
                    "AfterWriteAtServer",
                    "AfterWrite",
                ],
                "input_field": [
                    "OnChange",
                    "StartChoice",
                    "Clearing",
                    "ChoiceProcessing",
                    "AutoComplete",
                    "TextEditEnd",
                    "Opening",
                ],
                "check_box_field": ["OnChange"],
                "label_decoration": ["Click", "URLProcessing"],
                "label_field": ["OnChange", "Click", "URLProcessing"],
                "radio_button_field": ["OnChange"],
                "pages": ["OnCurrentPageChange"],
                "table": [
                    "Selection",
                    "OnActivateRow",
                    "ChoiceProcessing",
                    "OnStartEdit",
                    "BeforeAddRow",
                    "BeforeRowChange",
                    "BeforeDeleteRow",
                    "AfterDeleteRow",
                    "OnChange",
                    "OnEditEnd",
                ],
            },
        ),
        FormRule(
            "object_form_lifecycle_catalog",
            "supported",
            (
                "События чтения и записи разрешены только при главном "
                "реквизите metadata_object и получают документированные "
                "контексты и параметры."
            ),
            "platform_documentation",
            {
                "OnReadAtServer": {
                    "directive": "&НаСервере",
                    "parameters": ["ТекущийОбъект"],
                },
                "BeforeWrite": {
                    "directive": "&НаКлиенте",
                    "parameters": ["Отказ", "ПараметрыЗаписи"],
                },
                "BeforeWriteAtServer": {
                    "directive": "&НаСервере",
                    "parameters": [
                        "Отказ",
                        "ТекущийОбъект",
                        "ПараметрыЗаписи",
                    ],
                },
                "OnWriteAtServer": {
                    "directive": "&НаСервере",
                    "parameters": [
                        "Отказ",
                        "ТекущийОбъект",
                        "ПараметрыЗаписи",
                    ],
                },
                "AfterWriteAtServer": {
                    "directive": "&НаСервере",
                    "parameters": ["ТекущийОбъект", "ПараметрыЗаписи"],
                },
                "AfterWrite": {
                    "directive": "&НаКлиенте",
                    "parameters": ["ПараметрыЗаписи"],
                },
            },
        ),
        FormRule(
            "document_posting_events_not_form_events",
            "boundary",
            (
                "События BeforePosting, BeforeUndoPosting, Posting и "
                "UndoPosting не принадлежат форме документа и не должны "
                "добавляться в её список Events."
            ),
            "platform_documentation",
        ),
        FormRule(
            "managed_form_event_profile",
            "boundary",
            (
                "Каталог выбирает известные сигнатуры по версии платформы. "
                "Непроверенная совместимость версии Form.xml не блокирует "
                "генерацию, но возвращается предупреждением."
            ),
            "platform_documentation",
        ),
        FormRule(
            "platform_version_matrix",
            "supported",
            (
                "platform_version выбирает профиль событий и Form.xml вместе "
                "с уровнем доказательности; неизвестная версия разрешается с "
                "предупреждением."
            ),
            "contract_decision",
            platform_profiles_payload(),
        ),
        FormRule(
            "async_client_contract",
            "required",
            (
                "Ждать допустим только внутри Асинх-процедуры, а Асинх-процедура "
                "должна выполняться на клиенте. Callback API Начать... остаётся "
                "допустимым без Асинх."
            ),
            "platform_documentation",
        ),
        FormRule(
            "file_transfer_via_temporary_storage",
            "required",
            (
                "Локальный файл выбирается на клиенте; сервер получает адрес "
                "временного хранилища, а не локальный путь клиента."
            ),
            "platform_documentation",
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
        FormRule(
            "new_metadata_object_registry_warning",
            "boundary",
            (
                "Для новой обработки или другого ещё не загруженного объекта "
                "Registry закономерно возвращает metadata_object_not_found и "
                "configuration_links=warning; это не блокирует генерацию, а "
                "снимается после загрузки нового объекта и обновления snapshot."
            ),
            "contract_decision",
            {
                "code": "metadata_object_not_found",
                "configuration_links": "warning",
            },
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
                        "name": "Проверить",
                        "command": "Проверить",
                        "default": True,
                    },
                ],
            }
        ],
        "commands": [
            {
                "name": "Проверить",
                "title": {"ru": "Проверить"},
                "action": "Проверить",
            }
        ],
        "events": [
            {
                "event": "OnCreateAtServer",
                "handler": "ПриСозданииНаСервере",
            }
        ],
    }


def get_managed_form_rules(
    topic: RuleTopic = "overview",
    query: str | None = None,
) -> dict[str, object]:
    """Вернуть один bounded-раздел правил без чтения файлов или Registry."""

    if topic not in RULE_TOPICS:
        raise FormsRuleQueryError(
            "Неизвестная тема. Доступны: " + ", ".join(RULE_TOPICS) + "."
        )
    if query is not None:
        if topic != "terminology":
            raise FormsRuleQueryError(
                "Параметр query доступен только для topic=terminology."
            )
        if not query.strip():
            raise FormsRuleQueryError("Поисковый запрос не должен быть пустым.")
        if len(query) > MAX_TERM_QUERY_LENGTH:
            raise FormsRuleQueryError(
                f"Поисковый запрос длиннее {MAX_TERM_QUERY_LENGTH} символов."
            )

    payload: dict[str, object] = {
        "schema_version": 1,
        "topic": topic,
        "target": {
            "format": "configurator_xml",
            "confirmed_versions": ["2.16", "2.20"],
            "other_numeric_versions": "compiled_with_warning",
            "version_source": "Configuration.xml:/MetaDataObject/@version",
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
    if topic == "terminology":
        payload["languages"] = ["ru", "en", "canonical"]
        payload["match_policy"] = "exact_alias_or_all_query_tokens"
        if query is None:
            payload["entries"] = all_form_terms()
        else:
            payload["query"] = query
            payload["matches"] = search_form_terms(query)
    elif topic == "specification":
        payload["example"] = _minimal_example()
    elif topic == "elements":
        payload["supported"] = {
            "root": [
                "usual_group",
                "input_field",
                "check_box_field",
                "label_decoration",
                "label_field",
                "radio_button_field",
                "button",
                "command_bar",
                "pages",
                "table",
            ],
            "recursive_children": [
                "usual_group",
                "input_field",
                "check_box_field",
                "label_decoration",
                "label_field",
                "radio_button_field",
                "button",
                "command_bar",
                "pages",
                "table",
            ],
            "page_representations": ["tabs_on_top"],
            "command_bar_children": ["button", "popup", "button_group"],
            "popup_children": ["button", "button_group"],
        }
    elif topic == "attributes":
        payload["supported"] = {
            "scalar": ["string", "boolean", "number", "date"],
            "reference": ["metadata_reference"],
            "composite": ["scalar", "metadata_reference"],
            "collection": ["value_table"],
            "query": ["dynamic_list"],
            "string_length_zero": "unlimited",
        }
    elif topic == "layout":
        payload["example"] = {
            "intent": "Импорт файла",
            "tree": [
                "usual_group: источник и действия",
                "pages: данные, предпросмотр, настройки",
                "table: колонки value_table",
            ],
        }
    elif topic == "commands_events":
        payload["supported"] = {
            "platform_version": (
                "необязательная версия вида 8.3.23 или 8.3.23.1997; "
                "неизвестная версия использует ближайший профиль с предупреждением"
            ),
            "platform_profiles": platform_profiles_payload(),
            "event_owner": "поле owner отсутствует для формы либо содержит имя элемента",
            "form_events": [
                "OnCreateAtServer",
                "OnOpen",
                "NotificationProcessing",
                "ExternalEvent",
                "FillCheckProcessingAtServer",
                "BeforeClose",
                "OnClose",
                "ChoiceProcessing",
            ],
            "object_form_events": [
                "OnReadAtServer",
                "BeforeWrite",
                "BeforeWriteAtServer",
                "OnWriteAtServer",
                "AfterWriteAtServer",
                "AfterWrite",
            ],
            "element_events": {
                "input_field": [
                    "OnChange",
                    "StartChoice",
                    "Clearing",
                    "ChoiceProcessing",
                    "AutoComplete",
                    "TextEditEnd",
                    "Opening",
                ],
                "check_box_field": ["OnChange"],
                "label_decoration": ["Click", "URLProcessing"],
                "label_field": ["OnChange", "Click", "URLProcessing"],
                "radio_button_field": ["OnChange"],
                "pages": ["OnCurrentPageChange"],
                "table": [
                    "Selection",
                    "OnActivateRow",
                    "ChoiceProcessing",
                    "OnStartEdit",
                    "BeforeAddRow",
                    "BeforeRowChange",
                    "BeforeDeleteRow",
                    "AfterDeleteRow",
                    "OnChange",
                    "OnEditEnd",
                ],
            },
            "command_reference": {
                "specification": "<name>",
                "generated_xml": "Form.Command.<name>",
            },
            "standard_commands": {
                "form": ["Help", "Close", "CustomizeForm"],
                "object_form": ["Write", "WriteAndClose"],
                "document_form": ["Post", "PostAndClose", "UndoPosting"],
                "table": ["Add", "Delete", "MoveUp", "MoveDown"],
            },
            "async": "Асинх и Ждать только в клиентском контексте платформы 8.3.18+",
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
