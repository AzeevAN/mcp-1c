"""Типы и каноническое представление расширенной структуры выгрузки."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from types import MappingProxyType
from typing import Mapping

from .base_layer import _canonical, _order
from .intake_v2_extensions import ExtensionStructure as NativeExtensionStructure
from .model import Configuration, Field


class ConversionError(RuntimeError):
    """Известный структурный контракт source B нарушен."""


class RelationState(str, Enum):
    RESOLVED = "resolved"
    UNRESOLVED = "unresolved"


class AutoRecordPolicy(str, Enum):
    ALLOW = "Allow"
    DENY = "Deny"


class BindingState(str, Enum):
    """Состояние ссылки metadata на процедуру или функцию модуля."""

    UNAVAILABLE = "unavailable"
    RESOLVED = "resolved"
    UNRESOLVED = "unresolved"
    MODULE_MISSING = "module_missing"
    PROCEDURE_MISSING = "procedure_missing"


@dataclass(frozen=True, slots=True)
class TypeDescription:
    """Тип из файловой выгрузки вместе с квалификаторами."""

    types: tuple[str, ...] = ()
    string_length: int | None = None
    string_allowed_length: str = ""
    digits: int | None = None
    fraction_digits: int | None = None
    number_allowed_sign: str = ""
    date_parts: str = ""

    def to_field(self, name: str, **head: str) -> Field:
        return Field(
            name=name,
            synonym=head.get("synonym", ""),
            comment=head.get("comment", ""),
            indexing=head.get("indexing", ""),
            types=list(self.types),
            string_length=self.string_length,
            string_allowed_length=self.string_allowed_length,
            digits=self.digits,
            fraction_digits=self.fraction_digits,
            date_parts=self.date_parts,
        )


@dataclass(frozen=True, slots=True)
class MetadataRelation:
    kind: str
    target: str
    state: RelationState
    properties: tuple[tuple[str, str], ...] = ()


@dataclass(frozen=True, slots=True)
class CommonAttributePayload:
    value_type: TypeDescription
    password_mode: bool | None = None
    format: str = ""
    edit_format: str = ""
    tooltip: str = ""
    mark_negatives: bool | None = None
    mask: str = ""
    multi_line: bool | None = None
    extended_edit: bool | None = None
    min_value: str = ""
    max_value: str = ""
    fill_from_filling_value: bool | None = None
    fill_value: str = ""
    fill_checking: str = ""
    choice_folders_and_items: str = ""
    choice_form: str = ""
    quick_choice: str = ""
    create_on_input: str = ""
    data_history: str = ""
    indexing: str = ""
    full_text_search: str = ""
    data_separation: str = ""
    data_separation_value: str = ""
    data_separation_use: str = ""
    conditional_separation: str = ""
    separated_data_use: str = ""
    auto_use: str = ""
    authentication_separation: str = ""
    users_separation: str = ""
    configuration_extensions_separation: str = ""
    link_by_type: str = ""


@dataclass(frozen=True, slots=True)
class SessionParameterPayload:
    value_type: TypeDescription


@dataclass(frozen=True, slots=True)
class JournalStandardAttribute:
    name: str
    synonym: str = ""
    comment: str = ""
    password_mode: bool | None = None
    format: str = ""
    edit_format: str = ""
    tooltip: str = ""
    mark_negatives: bool | None = None
    mask: str = ""
    multi_line: bool | None = None
    extended_edit: bool | None = None
    min_value: str = ""
    max_value: str = ""
    fill_from_filling_value: bool | None = None
    fill_value: str = ""
    fill_checking: str = ""
    choice_form: str = ""
    choice_history_on_input: str = ""
    quick_choice: str = ""
    create_on_input: str = ""
    data_history: str = ""
    full_text_search: str = ""
    link_by_type: str = ""
    type_reduction_mode: str = ""


@dataclass(frozen=True, slots=True)
class JournalColumnReference:
    raw: str
    target: str


@dataclass(frozen=True, slots=True)
class JournalColumn:
    name: str
    synonym: str = ""
    comment: str = ""
    indexing: str = ""
    references: tuple[JournalColumnReference, ...] = ()


@dataclass(frozen=True, slots=True)
class JournalCommand:
    name: str
    synonym: str = ""
    comment: str = ""
    parameter_type: TypeDescription = TypeDescription()
    group: str = ""
    modifies_data: bool | None = None
    parameter_use_mode: str = ""
    representation: str = ""
    shortcut: str = ""
    tooltip: str = ""
    picture: tuple[str, ...] = ()
    server_unavailable_behavior: str = ""


@dataclass(frozen=True, slots=True)
class DocumentJournalPayload:
    list_presentation: str = ""
    extended_list_presentation: str = ""
    explanation: str = ""
    default_form: str = ""
    auxiliary_form: str = ""
    use_standard_commands: bool | None = None
    include_help_in_contents: bool | None = None
    registered_documents: tuple[str, ...] = ()
    standard_attributes: tuple[JournalStandardAttribute, ...] = ()
    columns: tuple[JournalColumn, ...] = ()
    commands: tuple[JournalCommand, ...] = ()
    forms: tuple[str, ...] = ()
    templates: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class FilterCriterionContentItem:
    """Одна исходная цель ``Content`` критерия отбора."""

    raw: str
    target: str


@dataclass(frozen=True, slots=True)
class FilterCriterionPayload:
    """Доказанный descriptor-контракт критерия отбора Source B."""

    uuid: str = ""
    value_type: TypeDescription = TypeDescription()
    content: tuple[FilterCriterionContentItem, ...] = ()
    use_standard_commands: bool | None = None
    default_form: str = ""
    auxiliary_form: str = ""
    list_presentation: str = ""
    extended_list_presentation: str = ""
    explanation: str = ""
    forms: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class CommonCommandPayload:
    """Доказанные свойства общей команды из descriptor Source B."""

    uuid: str = ""
    group: str = ""
    representation: str = ""
    tooltip: str = ""
    picture: tuple[str, ...] = ()
    shortcut: str = ""
    include_help_in_contents: bool | None = None
    parameter_type: TypeDescription | None = None
    parameter_use_mode: str = ""
    modifies_data: bool | None = None
    server_unavailable_behavior: str = ""


@dataclass(frozen=True, slots=True)
class SubsystemContentItem:
    raw: str
    target: str


@dataclass(frozen=True, slots=True)
class InterfaceVisibility:
    target: str
    common: bool | None = None
    roles: tuple[tuple[str, bool], ...] = ()


@dataclass(frozen=True, slots=True)
class InterfacePlacement:
    target: str
    group: str


@dataclass(frozen=True, slots=True)
class SubsystemCommandInterface:
    """Статическая настройка интерфейса из Source B, не текущий UI сеанса."""

    command_visibility: tuple[InterfaceVisibility, ...] = ()
    command_placement: tuple[InterfacePlacement, ...] = ()
    command_order: tuple[InterfacePlacement, ...] = ()
    group_order: tuple[str, ...] = ()
    subsystem_order: tuple[str, ...] = ()
    subsystem_visibility: tuple[InterfaceVisibility, ...] = ()


@dataclass(frozen=True, slots=True)
class SubsystemPayload:
    uuid: str = ""
    path: tuple[str, ...] = ()
    parent: str = ""
    include_help_in_contents: bool | None = None
    include_in_command_interface: bool | None = None
    use_one_command: bool | None = None
    explanation: str = ""
    picture: tuple[str, ...] = ()
    content: tuple[SubsystemContentItem, ...] = ()
    command_interface: SubsystemCommandInterface = SubsystemCommandInterface()


@dataclass(frozen=True, slots=True)
class ExchangePlanContentItem:
    raw: str
    target: str
    auto_record: AutoRecordPolicy


@dataclass(frozen=True, slots=True)
class ExchangePlanPayload:
    """Свойства source B сверх schema-v1-проекции плана обмена."""

    based_on: tuple[str, ...] = ()
    characteristics: str = ""
    choice_data_get_mode_on_input_by_string: str = ""
    choice_history_on_input: str = ""
    choice_mode: str = ""
    code_allowed_length: str = ""
    code_length: int | None = None
    create_on_input: str = ""
    data_history: str = ""
    data_lock_control_mode: str = ""
    data_lock_fields: tuple[str, ...] = ()
    default_presentation: str = ""
    description_length: int | None = None
    edit_type: str = ""
    execute_after_write_data_history_version_processing: bool | None = None
    explanation: str = ""
    extended_list_presentation: str = ""
    extended_object_presentation: str = ""
    full_text_search: str = ""
    full_text_search_on_input_by_string: str = ""
    include_configuration_extensions: bool | None = None
    include_help_in_contents: bool | None = None
    input_by_string: tuple[str, ...] = ()
    list_presentation: str = ""
    object_presentation: str = ""
    quick_choice: bool | None = None
    search_string_mode_on_input_by_string: str = ""
    update_data_history_immediately_after_write: bool | None = None
    use_standard_commands: bool | None = None
    default_choice_form: str = ""
    auxiliary_choice_form: str = ""
    default_list_form: str = ""
    auxiliary_list_form: str = ""
    default_object_form: str = ""
    auxiliary_object_form: str = ""
    standard_attributes: tuple[JournalStandardAttribute, ...] = ()
    commands: tuple[JournalCommand, ...] = ()
    forms: tuple[str, ...] = ()
    templates: tuple[str, ...] = ()
    content: tuple[ExchangePlanContentItem, ...] = ()


@dataclass(frozen=True, slots=True)
class CodeBinding:
    raw: str
    state: BindingState
    module_address: str = ""
    procedure_address: str = ""


@dataclass(frozen=True, slots=True)
class HTTPServiceMethod:
    uuid: str
    name: str
    synonym: str
    comment: str
    http_method: str
    handler: str
    binding: CodeBinding


@dataclass(frozen=True, slots=True)
class HTTPServiceURLTemplate:
    uuid: str
    name: str
    synonym: str
    comment: str
    template: str
    methods: tuple[HTTPServiceMethod, ...] = ()


@dataclass(frozen=True, slots=True)
class HTTPServicePayload:
    uuid: str
    root_url: str
    reuse_sessions: str | None
    session_max_age: int | None
    url_templates: tuple[HTTPServiceURLTemplate, ...] = ()


@dataclass(frozen=True, slots=True)
class XDTOPackagePayload:
    uuid: str
    namespace: str
    target_namespace: str
    element_form_qualified: bool | None
    attribute_form_qualified: bool | None
    imports: tuple[str, ...] = ()
    object_types: tuple[str, ...] = ()
    value_types: tuple[str, ...] = ()
    properties: tuple[str, ...] = ()
    content_sha256: str = ""


@dataclass(frozen=True, slots=True)
class XDTOMemberPayload:
    package_address: str
    namespace: str
    member_kind: str
    content_sha256: str


@dataclass(frozen=True, slots=True)
class EventSubscriptionPayload:
    binding: CodeBinding


@dataclass(frozen=True, slots=True)
class ScheduledJobPayload:
    description: str
    restart_count_on_failure: int | None
    restart_interval_on_failure: int | None
    binding: CodeBinding


class FormStructureState(str, Enum):
    READY = "ready"
    PARTIAL = "partial"
    DEFERRED = "deferred"
    UNREADABLE = "unreadable"
    DESCRIPTOR_ONLY = "descriptor_only"


class FormModuleState(str, Enum):
    READY = "ready"
    EMPTY = "empty"
    MISSING = "module_missing"
    UNREADABLE = "unreadable"


@dataclass(frozen=True, slots=True)
class FormEventBinding:
    element: str | None
    event: str
    handler: str
    binding: CodeBinding


@dataclass(frozen=True, slots=True)
class CommonFormPayload:
    uuid: str = ""
    form_type: str = ""
    explanation: str = ""
    extended_presentation: str = ""
    include_help_in_contents: bool | None = None
    use_purposes: tuple[str, ...] = ()
    use_standard_commands: bool | None = None
    structure_state: FormStructureState = FormStructureState.DESCRIPTOR_ONLY
    module_state: FormModuleState = FormModuleState.MISSING
    container_marker: int | None = None
    attributes: tuple[str, ...] = ()
    elements: tuple[str, ...] = ()
    events: tuple[FormEventBinding, ...] = ()


class BotModuleState(str, Enum):
    PRESENT = "present"
    MISSING = "module_missing"


@dataclass(frozen=True, slots=True)
class BotPayload:
    uuid: str = ""
    picture: tuple[str, ...] = ()
    predefined: bool | None = None
    module_state: BotModuleState = BotModuleState.MISSING


@dataclass(frozen=True, slots=True)
class ExtendedObject:
    full_name: str
    kind: str
    name: str
    synonym: str = ""
    comment: str = ""
    code_address: str = ""
    base_object: bool = False
    payload: object | None = None
    modules: tuple[str, ...] = ()
    forms: tuple[str, ...] = ()
    relations: tuple[MetadataRelation, ...] = ()


@dataclass(frozen=True, slots=True)
class ExtendedStructure:
    objects: Mapping[str, ExtendedObject]
    extension_structure: NativeExtensionStructure | None = None

    def __post_init__(self) -> None:
        ordered = dict(sorted(self.objects.items(), key=lambda item: _order(item[0])))
        if any(key != value.full_name for key, value in ordered.items()):
            raise ConversionError("ключ extended object не совпадает с full_name")
        object.__setattr__(self, "objects", MappingProxyType(ordered))
        if self.extension_structure is not None and not isinstance(
            self.extension_structure, NativeExtensionStructure
        ):
            raise ConversionError("extension_structure имеет неверный тип")

    def __len__(self) -> int:
        return len(self.objects)

    def get(self, full_name: str) -> ExtendedObject | None:
        return self.objects.get(full_name)

    def to_layer_data(self) -> dict[str, object]:
        return extended_layer_data(self)


@dataclass(frozen=True, slots=True)
class ConversionDiagnostic:
    code: str
    signature: str
    count: int
    examples: tuple[str, ...]
    severity: str = "info"


@dataclass(frozen=True, slots=True)
class StructureConversion:
    base: Configuration
    extended: ExtendedStructure
    base_content_sha256: str
    extended_content_sha256: str
    diagnostics: tuple[ConversionDiagnostic, ...] = ()


def _extended_payload_data(value: object | None) -> object:
    """Отделить metadata-декларации от производных code/forms-состояний."""
    if isinstance(value, XDTOPackagePayload):
        return {
            "uuid": value.uuid,
            "namespace": value.namespace,
            "target_namespace": value.target_namespace,
            "element_form_qualified": value.element_form_qualified,
            "attribute_form_qualified": value.attribute_form_qualified,
            "imports": list(value.imports),
            "object_types": list(value.object_types),
            "value_types": list(value.value_types),
            "properties": list(value.properties),
            "content_sha256": value.content_sha256,
        }
    if isinstance(value, XDTOMemberPayload):
        return {
            "package_address": value.package_address,
            "namespace": value.namespace,
            "member_kind": value.member_kind,
            "content_sha256": value.content_sha256,
        }
    if isinstance(value, HTTPServicePayload):
        return {
            "uuid": value.uuid,
            "root_url": value.root_url,
            "reuse_sessions": value.reuse_sessions,
            "session_max_age": value.session_max_age,
            "url_templates": [
                {
                    "uuid": template.uuid,
                    "name": template.name,
                    "synonym": template.synonym,
                    "comment": template.comment,
                    "template": template.template,
                    "methods": [
                        {
                            "uuid": method.uuid,
                            "name": method.name,
                            "synonym": method.synonym,
                            "comment": method.comment,
                            "http_method": method.http_method,
                            "handler": method.handler,
                        }
                        for method in template.methods
                    ],
                }
                for template in value.url_templates
            ],
        }
    if isinstance(value, EventSubscriptionPayload):
        return {"binding": {"raw": value.binding.raw}}
    if isinstance(value, ScheduledJobPayload):
        return {
            "description": value.description,
            "restart_count_on_failure": value.restart_count_on_failure,
            "restart_interval_on_failure": value.restart_interval_on_failure,
            "binding": {"raw": value.binding.raw},
        }
    if isinstance(value, CommonFormPayload):
        return {
            "uuid": value.uuid,
            "form_type": value.form_type,
            "explanation": value.explanation,
            "extended_presentation": value.extended_presentation,
            "include_help_in_contents": value.include_help_in_contents,
            "use_purposes": list(value.use_purposes),
            "use_standard_commands": value.use_standard_commands,
        }
    if isinstance(value, BotPayload):
        return {
            "uuid": value.uuid,
            "picture": list(value.picture),
            "predefined": value.predefined,
        }
    return _canonical(value)


def extended_layer_data(extended: ExtendedStructure) -> dict[str, object]:
    """Вернуть канонические metadata-данные без тел кода и разбора форм.

    Читает слой `intake_v2_runtime._compose_extended_structure` (точный набор
    полей объекта); round-trip — `tests/test_intake_v2_layer_roundtrip.py`.
    """
    if not isinstance(extended, ExtendedStructure):
        raise TypeError("extended должен быть ExtendedStructure")
    return {
        "extension_structure": (
            extended.extension_structure.to_layer_dict()
            if extended.extension_structure is not None
            else None
        ),
        "objects": [
            {
                "full_name": value.full_name,
                "kind": value.kind,
                "name": value.name,
                "synonym": value.synonym,
                "comment": value.comment,
                "code_address": value.code_address,
                "base_object": value.base_object,
                "payload": _extended_payload_data(value.payload),
                # Наличие формы у объекта — структурная связь metadata;
                # содержимое формы и состояние её модуля живут в своих слоях.
                "forms": list(value.forms),
                "relations": _canonical(value.relations),
            }
            for value in extended.objects.values()
        ]
    }
