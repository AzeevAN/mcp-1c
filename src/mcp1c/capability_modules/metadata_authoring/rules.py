"""Компактный контракт создания файлов метаданных без внешних эффектов."""

from __future__ import annotations

import copy
from typing import Literal, TypeAlias


RuleTopic: TypeAlias = Literal[
    "overview",
    "catalog",
    "document",
    "data_processor",
    "information_register",
    "forms",
    "artifacts",
    "diagnostics",
]

RULE_TOPICS: tuple[RuleTopic, ...] = (
    "overview",
    "catalog",
    "document",
    "data_processor",
    "information_register",
    "forms",
    "artifacts",
    "diagnostics",
)

_CATALOG_TYPES = ["Object", "Ref", "Selection", "List", "Manager"]
_DOCUMENT_TYPES = ["Object", "Ref", "Selection", "List", "Manager"]
_REGISTER_TYPES = [
    "Record",
    "Manager",
    "Selection",
    "List",
    "RecordSet",
    "RecordKey",
    "RecordManager",
]
_DATA_PROCESSOR_TYPES = ["Object", "Manager"]

_RULES: dict[RuleTopic, dict[str, object]] = {
    "overview": {
        "status": "supported",
        "available_topics": list(RULE_TOPICS),
        "supported_metadata_kinds": [
            "Справочник",
            "Документ",
            "РегистрСведений",
            "Обработка",
        ],
        "recommended_call_order": [
            "get_metadata_authoring_rules",
            "compile_managed_form (для каждой формы)",
            "check_managed_form (для каждой формы)",
            "compile_metadata_object",
            "check_metadata_artifacts",
        ],
        "compiler_schema_version": 1,
        "compiler_is_pure": True,
        "compiler_identity": (
            "identity становится UUID объекта; остальные UUID детерминированно "
            "выводятся из identity"
        ),
        "compiler_specification": {
            "required": [
                "schema_version",
                "object_ref",
                "format_version",
                "identity",
                "synonym",
                "attributes",
                "forms",
            ],
            "schema_version": 1,
            "format_version": {
                "required": True,
                "pattern": "digits.digits[.digits...]",
                "source": "caller читает format_version целевой Configuration.xml самостоятельно",
            },
            "identity": "UUID",
            "field": ["name", "synonym", "type"],
            "field_type_kinds": [
                "string",
                "boolean",
                "number",
                "date",
                "catalog_ref",
                "document_ref",
            ],
            "field_types": {
                "string": {
                    "required": ["kind", "length"],
                    "length": "1..1024",
                    "allowed_length": "Variable",
                },
                "boolean": {"required": ["kind"]},
                "number": {
                    "required": ["kind", "digits", "fraction_digits"],
                    "digits": "1..32",
                    "fraction_digits": "0..digits",
                    "allowed_sign": "Any",
                },
                "date": {
                    "required": ["kind", "fractions"],
                    "fractions": ["date", "time", "date_time"],
                },
                "catalog_ref": {
                    "required": ["kind", "object"],
                    "object": "Справочник.<Имя>",
                },
                "document_ref": {
                    "required": ["kind", "object"],
                    "object": "Документ.<Имя>",
                },
            },
            "form": [
                "name",
                "synonym",
                "role (optional)",
                "default",
                "form_xml",
                "module_bsl",
            ],
            "form_roles": {
                "catalog": ["object", "list", "choice"],
                "document": ["object", "list", "choice"],
                "information_register": ["record", "list", "record_set"],
                "data_processor": ["object"],
                "legacy_default": {
                    "catalog": "object",
                    "document": "object",
                    "information_register": "record",
                    "data_processor": "object",
                },
                "one_physical_form_one_role": True,
                "shared_list_choice_form": "not_supported",
            },
            "form_defaults": (
                "поле default остаётся обязательным boolean, но значение true "
                "необязательно для каждой представленной роли; допустимо не "
                "назначать ни одной формы по умолчанию, но не более одной "
                "default=true на роль"
            ),
            "form_xml_preparation": (
                "если forms[] непуст, до metadata compile обязательно запросите "
                "topic=forms, затем get_managed_form_rules(topic=overview) и "
                "перечисленные там предметные темы, после чего выполните "
                "Forms compile/check workflow; "
                "topic=artifacts содержит только shape reference корня, namespace "
                "и version, а не готовый артефакт формы"
            ),
            "catalog_additional_required": [
                "code_length",
                "description_length",
            ],
            "catalog_standard_fields": {
                "code_length": "0..50; Код существует только при значении > 0",
                "description_length": (
                    "0..150; Наименование существует только при значении > 0"
                ),
                "default_object_form": (
                    "Основная форма обязана выводить "
                    "Объект.Наименование при description_length > 0 и Объект.Код "
                    "при code_length > 0. Это DataPath главного реквизита Объект, "
                    "а не отдельные реквизиты формы. Отсутствие обязательного "
                    "DataPath отклоняет specification."
                ),
            },
            "information_register_additional_required": [
                "periodicity",
                "dimensions",
                "resources",
            ],
            "document_additional_required": [
                "number_length",
                "number_allowed_length",
                "number_periodicity",
                "check_unique",
                "autonumbering",
                "posting",
                "real_time_posting",
            ],
        },
        "configuration_boundary": (
            "Configuration.xml и регистрация объекта не входят в контракт: caller "
            "сам читает локальную конфигурацию, передаёт только format_version и "
            "применяет регистрацию после проверки owner-relative артефактов"
        ),
        "recommended_preparation": (
            "до compile получите фактическую структуру целевой конфигурации "
            "core-инструментами MCP и сформируйте specification по найденным данным"
        ),
        "external_reference_resolution": "not_checked",
        "caller_responsibility": (
            "существование объектов и реквизитов из catalog_ref, document_ref, Объект.* и "
            "Запись.* обеспечивает caller; compiler кодирует переданное намерение, "
            "но не подтверждает цель по Registry или конфигурации"
        ),
        "writes_files": False,
        "imports_configuration": False,
        "native_import_proven": False,
        "forms_workflow": (
            "Если forms[] непуст, обязательно вызовите "
            "get_managed_form_rules(topic=overview), запросите перечисленные там "
            "предметные темы, затем для каждой роли выполните compile_managed_form -> "
            "check_managed_form на тех же возвращённых строках; только после "
            "успешной проверки передайте точные строки Form.xml/Module.bsl "
            "byte-for-byte программно и ту же role в forms[] -> "
            "compile_metadata_object -> check_metadata_artifacts. Ручной XML не "
            "считается доказанной формой: Metadata checker не подтверждает "
            "provenance и semantic role Form.xml. Не перепечатывайте, не "
            "пересказывайте, не сокращайте, не обрезайте и не реконструируйте "
            "строки; не используйте minimal_shape_reference. Metadata compile "
            "request размером в десятки KB является нормальным."
        ),
    },
    "document": {
        "object_ref": "Документ.<Имя>",
        "descriptor_path": "Documents/<Имя>.xml",
        "descriptor_element": "md:Document",
        "required_sections": ["InternalInfo", "Properties", "ChildObjects"],
        "name_property": "Properties/Name",
        "form_declaration": "ChildObjects/Form",
        "form_declaration_value": "<Форма>",
        "default_form_value": "Document.<Имя>.Form.<Форма>",
        "default_form_properties": {
            "object": "DefaultObjectForm",
            "list": "DefaultListForm",
            "choice": "DefaultChoiceForm",
        },
        "attribute_declaration": "ChildObjects/Attribute[@uuid]",
        "generated_types": _DOCUMENT_TYPES,
        "generated_type_name_pattern": "Document<Category>.<Имя>",
        "default_form_property": "DefaultObjectForm",
        "document_reference_type": "cfg:DocumentRef.<ИмяДокумента>",
        "number": {
            "type": "String",
            "length": "1..50",
            "allowed_length": ["Variable", "Fixed"],
            "periodicity": ["Nonperiodical", "Year"],
        },
        "posting": "Deny",
        "real_time_posting": "Deny",
        "limitations": [
            "непроводимый документ",
            "без табличных частей и движений",
            "без команд проведения, событий и прикладного BSL",
            "одна физическая форма не совмещает роли list и choice",
        ],
        "forms_handoff": (
            "не собирайте Form.xml вручную: сначала вызовите compile_managed_form "
            "для Документ.<Имя> + role=object, list или choice, проверьте результат через "
            "check_managed_form и передайте content артефактов Form.xml/Module.bsl "
            "в forms[]; Metadata Authoring только упаковывает проверенный "
            "owner-relative артефакт"
        ),
        "forms_workflow": [
            "compile_managed_form",
            "check_managed_form",
            "передать content Form.xml и Module.bsl в compile_metadata_object.forms[]",
            "check_metadata_artifacts",
        ],
        "metadata_checker_boundary": (
            "check_metadata_artifacts проверяет descriptor, owner-relative Default*Form "
            "и комплект артефактов; чужой cfg:DocumentObject отклоняется и "
            "DefaultObjectForm обязан содержать DocumentObject текущего владельца, "
            "но checker не выводит semantic role из Form.xml; проверка роли "
            "остаётся результатом check_managed_form"
        ),
        "forms_profile": {
            "context": {
                "owner": "Документ.<Имя>",
                "role": "object",
            },
            "main_attribute": {
                "name": "Объект",
                "type": {
                    "kind": "metadata_object",
                    "object": "Документ.<Имя>",
                },
                "main": True,
            },
            "standard_field_rule": (
                "стандартные реквизиты документа не добавляются отдельными "
                "attributes формы: поле даты использует data_path `Объект.Дата`"
            ),
            "commands": [],
            "events": [],
        },
        "compiler_specification_example": {
            "schema_version": 1,
            "object_ref": "Документ.ЗаявкаПример",
            "format_version": "2.20",
            "identity": "60000000-0000-0000-0000-000000000001",
            "synonym": "Заявка (пример)",
            "number_length": 11,
            "number_allowed_length": "Variable",
            "number_periodicity": "Nonperiodical",
            "check_unique": True,
            "autonumbering": True,
            "posting": "Deny",
            "real_time_posting": "Deny",
            "attributes": [
                {
                    "name": "Основание",
                    "synonym": "Основание",
                    "type": {
                        "kind": "document_ref",
                        "object": "Документ.ДокументОснование",
                    },
                }
            ],
            "forms": [],
        },
        "compiler_specification_example_note": (
            "Если forms[] будет непуст, raw form_xml в примере допустим только как "
            "shape placeholder; в реальной цепочке замените его content, полученным "
            "после compile_managed_form и check_managed_form."
        ),
    },
    "data_processor": {
        "object_ref": "Обработка.<Имя>",
        "scope": "только встроенный объект метаданных; внешняя .epf не поддержана",
        "descriptor_path": "DataProcessors/<Имя>.xml",
        "descriptor_element": "md:DataProcessor",
        "required_sections": ["InternalInfo", "Properties", "ChildObjects"],
        "generated_types": _DATA_PROCESSOR_TYPES,
        "generated_type_name_pattern": "DataProcessor<Category>.<Имя>",
        "form_roles": ["object"],
        "default_form_property": "DefaultForm",
        "default_form_value": "DataProcessor.<Имя>.Form.<Форма>",
        "forms_required": True,
        "forms_count": 1,
        "default_required": True,
        "forms_profile": {
            "context": {"owner": "Обработка.<Имя>", "role": "object"},
            "main_attribute": {
                "name": "Объект",
                "type": {
                    "kind": "metadata_object",
                    "object": "Обработка.<Имя>",
                },
                "main": True,
                "saved_data": "не требуется",
            },
        },
        "compiler_specification_example": {
            "schema_version": 1,
            "object_ref": "Обработка.ИмпортПример",
            "format_version": "2.20",
            "identity": "80000000-0000-0000-0000-000000000001",
            "synonym": "Импорт (пример)",
            "attributes": [],
            "forms": [
                {
                    "name": "Форма",
                    "synonym": "Форма",
                    "role": "object",
                    "default": True,
                    "form_xml": "<точный content Forms compiler>",
                    "module_bsl": "<точный content Forms compiler>",
                }
            ],
        },
        "compiler_specification_example_note": (
            "Сначала соберите role=object форму через Forms rules → compile → check, "
            "затем программно передайте точные Form.xml и Module.bsl byte-for-byte."
        ),
        "limitations": [
            "без внешней .epf",
            "без команд, событий, макетов и прикладного BSL объекта метаданных",
            "без дополнительных и вспомогательных форм",
        ],
    },
    "catalog": {
        "object_ref": "Справочник.<Имя>",
        "descriptor_path": "Catalogs/<Имя>.xml",
        "descriptor_element": "md:Catalog",
        "required_sections": ["InternalInfo", "Properties", "ChildObjects"],
        "name_property": "Properties/Name",
        "form_declaration": "ChildObjects/Form",
        "form_declaration_value": "<Форма>",
        "default_form_value": "Catalog.<Имя>.Form.<Форма>",
        "default_form_properties": {
            "object": "DefaultObjectForm",
            "list": "DefaultListForm",
            "choice": "DefaultChoiceForm",
        },
        "attribute_declaration": "ChildObjects/Attribute[@uuid]",
        "generated_types": _CATALOG_TYPES,
        "generated_type_name_pattern": "Catalog<Category>.<Имя>",
        "default_form_property": "DefaultObjectForm",
        "standard_form_fields": {
            "Объект.Наименование": "обязательно при description_length > 0",
            "Объект.Код": "обязательно при code_length > 0",
        },
        "standard_command_bar": (
            "Для role=object не объявляйте пользовательские команды Записать/ЗаписатьИЗакрыть: "
            "оставьте commands пустым и используйте корневую AutoCommandBar, "
            "которую платформа заполняет по главному реквизиту Объект."
        ),
        "compiler_specification_example": {
            "schema_version": 1,
            "object_ref": "Справочник.ПроектыПример",
            "format_version": "2.20",
            "identity": "40000000-0000-0000-0000-000000000001",
            "synonym": "Проекты (пример)",
            "code_length": 9,
            "description_length": 100,
            "attributes": [
                {
                    "name": "Активен",
                    "synonym": "Активен",
                    "type": {"kind": "boolean"},
                }
            ],
            "forms": [
                {
                    "name": "ФормаЭлемента",
                    "synonym": "Форма элемента",
                    "role": "object",
                    "default": True,
                    "form_xml": (
                        '<Form xmlns="http://v8.1c.ru/8.3/xcf/logform" '
                        'version="2.20"><ChildItems>'
                        '<InputField name="Наименование" id="1">'
                        '<DataPath>Объект.Наименование</DataPath></InputField>'
                        '<InputField name="Код" id="2">'
                        '<DataPath>Объект.Код</DataPath></InputField>'
                        '</ChildItems></Form>'
                    ),
                    "module_bsl": "",
                }
            ],
        },
        "compiler_specification_example_note": (
            "Встроенный raw form_xml — только shape placeholder payload и не является "
            "готовым доказанным артефактом. В реальной цепочке замените его content "
            "результатом compile_managed_form, прошедшим check_managed_form."
        ),
    },
    "information_register": {
        "object_ref": "РегистрСведений.<Имя>",
        "descriptor_path": "InformationRegisters/<Имя>.xml",
        "descriptor_element": "md:InformationRegister",
        "required_sections": ["InternalInfo", "Properties", "ChildObjects"],
        "name_property": "Properties/Name",
        "form_declaration": "ChildObjects/Form",
        "form_declaration_value": "<Форма>",
        "default_form_value": "InformationRegister.<Имя>.Form.<Форма>",
        "default_form_properties": {
            "record": "DefaultRecordForm",
            "list": "DefaultListForm",
            "record_set": None,
        },
        "field_declarations": [
            "ChildObjects/Dimension[@uuid]",
            "ChildObjects/Resource[@uuid]",
            "ChildObjects/Attribute[@uuid]",
        ],
        "catalog_reference_type": "cfg:CatalogRef.<ИмяСправочника>",
        "generated_types": _REGISTER_TYPES,
        "generated_type_name_pattern": "InformationRegister<Category>.<Имя>",
        "periodicity_property": "InformationRegisterPeriodicity",
        "compiler_periodicity": "nonperiodical",
        "generated_periodicity": "Nonperiodical",
        "unsupported_periodicity_alias": "Periodicity",
        "default_form_property": "DefaultRecordForm",
        "record_set_default_property": None,
        "record_set_default_rule": (
            "Обычная форма создания и редактирования одной записи задаётся "
            "role=record с default=true и становится DefaultRecordForm. "
            "role=record_set допускается только с default=false: "
            "она не подменяет форму записи, а DefaultRecordSetForm не "
            "подтверждён реальными descriptor"
        ),
        "standard_command_bar": (
            "Не объявляйте пользовательские команды Записать/ЗаписатьИЗакрыть: "
            "оставьте commands пустым и используйте корневую AutoCommandBar, "
            "которую платформа заполняет по главному реквизиту Запись."
        ),
        "compiler_specification_example": {
            "schema_version": 1,
            "object_ref": "РегистрСведений.ОценкиПример",
            "format_version": "2.20",
            "identity": "50000000-0000-0000-0000-000000000001",
            "synonym": "Оценки (пример)",
            "periodicity": "nonperiodical",
            "dimensions": [
                {
                    "name": "Контрагент",
                    "synonym": "Контрагент",
                    "main_filter": True,
                    "type": {
                        "kind": "catalog_ref",
                        "object": "Справочник.Контрагенты",
                    },
                }
            ],
            "resources": [
                {
                    "name": "Оценка",
                    "synonym": "Оценка",
                    "type": {
                        "kind": "number",
                        "digits": 10,
                        "fraction_digits": 2,
                    },
                }
            ],
            "attributes": [],
            "forms": [
                {
                    "name": "ФормаЗаписи",
                    "synonym": "Форма записи",
                    "role": "record",
                    "default": True,
                    "form_xml": (
                        '<Form xmlns="http://v8.1c.ru/8.3/xcf/logform" '
                        'version="2.20"><ChildItems>'
                        '<InputField name="Контрагент" id="1">'
                        '<DataPath>Запись.Контрагент</DataPath></InputField>'
                        '</ChildItems></Form>'
                    ),
                    "module_bsl": "",
                }
            ],
        },
        "compiler_specification_example_note": (
            "Встроенный raw form_xml — только shape placeholder payload и не является "
            "готовым доказанным артефактом. В реальной цепочке замените его content "
            "результатом compile_managed_form, прошедшим check_managed_form."
        ),
    },
    "forms": {
        "status": "supported_workflow",
        "when_required": "обязательно перед compile_metadata_object, если forms[] непуст",
        "role_mapping": {
            "Справочник": ["object", "list", "choice"],
            "Документ": ["object", "list", "choice"],
            "РегистрСведений": ["record", "list", "record_set"],
            "Обработка": ["object"],
        },
        "legacy_role_when_omitted": {
            "Справочник": "object",
            "Документ": "object",
            "РегистрСведений": "record",
            "Обработка": "object",
        },
        "default_semantics": {
            "field_required": True,
            "true_optional_per_role": True,
            "maximum_true_per_role": 1,
            "zero_defaults_allowed": True,
            "data_processor_override": (
                "ровно одна role=object форма с default=true; она становится DefaultForm"
            ),
            "shared_physical_list_choice_form": "not_supported",
        },
        "required_call_order": [
            "get_managed_form_rules(topic=overview)",
            "запросить перечисленные в overview предметные темы Forms",
            "compile_managed_form для каждой формы и её role",
            "check_managed_form на тех же точных возвращённых строках",
            "программно передать точные Form.xml и Module.bsl byte-for-byte плюс ту же role в forms[]",
            "compile_metadata_object",
            "check_metadata_artifacts",
        ],
        "exact_handoff": {
            "source": "строки Form.xml и Module.bsl из compile_managed_form",
            "forms_check_input": "те же точные возвращённые строки",
            "metadata_forms_input": "те же точные строки byte-for-byte",
            "forbidden_transformations": [
                "retype",
                "summarize",
                "truncate",
                "reconstruct",
                "minimal_shape_reference",
            ],
            "transport": "programmatic",
            "tens_of_kilobytes": "normal",
        },
        "manual_xml": (
            "raw Form.xml может служить только shape reference; ручной XML не "
            "считается доказанной формой и не заменяет Forms compile/check"
        ),
        "metadata_checker_boundary": {
            "provenance": "not_checked",
            "semantic_role": "not_checked",
            "owner_relative_bundle": "checked",
            "document_object_owner": "checked",
        },
    },
    "artifacts": {
        "paths_are_owner_relative": True,
        "path_contract": {
            "expected_bundle_only": True,
            "forbidden": ["absolute", "backslash", "empty_segment", ".", ".."],
            "unsafe_diagnostic": "unsafe_artifact_path",
            "extra_file_diagnostic": "unexpected_artifact",
        },
        "namespaces": {
            "md": "http://v8.1c.ru/8.3/MDClasses",
            "xr": "http://v8.1c.ru/8.3/xcf/readable",
            "v8": "http://v8.1c.ru/8.1/data/core",
            "xs": "http://www.w3.org/2001/XMLSchema",
            "logform": "http://v8.1c.ru/8.3/xcf/logform",
        },
        "descriptor_root": "md:MetaDataObject",
        "compiler_to_checker": {
            "tool": "check_metadata_artifacts",
            "arguments": {
                "object_ref": "compile result.object_ref",
                "format_version": "compile result.format_version",
                "artifacts": "compile result.artifacts",
            },
            "legacy_artifacts_mapping_supported": True,
        },
        "generated_type": {
            "element": "xr:GeneratedType",
            "attributes": ["name", "category"],
            "children": ["xr:TypeId", "xr:ValueId"],
            "uuid_unique_across_bundle": True,
        },
        "qname_prefix_scope": "same_xml_document",
        "required_base": ["<owner>.xml"],
        "required_for_each_declared_form": [
            "<owner>/Forms/<Форма>.xml",
            "<owner>/Forms/<Форма>/Ext/Form.xml",
            "<owner>/Forms/<Форма>/Ext/Form/Module.bsl",
        ],
        "form_descriptor": {
            "root": "md:MetaDataObject/md:Form",
            "required": ["uuid", "Properties/Name", "Properties/FormType"],
            "form_type": "Managed",
            "name_value": "точное короткое значение ChildObjects/Form",
        },
        "default_form_references": {
            "empty_allowed": True,
            "must_be_owner_relative": True,
            "must_reference_declared_form": True,
            "semantic_role_from_form_xml": "not_checked",
        },
        "form_xml": {
            "path": "<owner>/Forms/<Форма>/Ext/Form.xml",
            "root": (
                'Form xmlns="http://v8.1c.ru/8.3/xcf/logform"'
            ),
            "format_version": {
                "required": True,
                "source": "specification.format_version",
                "constraint": "must_equal_supplied_format_version",
                "mismatch_status": "failed",
            },
            "not_descriptor_root": "md:MetaDataObject",
            "shape_reference_only": True,
            "shape_reference_warning": (
                "Не копируйте этот raw XML как готовый forms[] artifact: он "
                "показывает только форму XML и не заменяет compile_managed_form "
                "плюс check_managed_form."
            ),
            "minimal_shape_reference": (
                '<Form xmlns="http://v8.1c.ru/8.3/xcf/logform" version="2.20">'
                '<ChildItems><InputField name="Поле" id="1">'
                '<DataPath>Объект.Реквизит</DataPath>'
                '</InputField></ChildItems></Form>'
            ),
            "data_path_patterns": [
                "Объект.<Реквизит>",
                "Запись.<Реквизит>",
            ],
            "native_import_proven": False,
        },
        "max_artifacts": 32,
        "max_artifact_bytes": 2 * 1024 * 1024,
        "max_total_bytes": 8 * 1024 * 1024,
    },
    "diagnostics": {
        "coverage": [
            "xml",
            "descriptor",
            "generated_types",
            "forms",
            "namespaces",
        ],
        "result_statuses": ["passed", "failed"],
        "read_only": True,
        "native_import_proven": False,
    },
}


def get_metadata_authoring_rules(topic: RuleTopic = "overview") -> dict[str, object]:
    """Вернуть один раздел закрытого контракта capability."""

    if topic not in RULE_TOPICS:
        raise ValueError(
            f"Неизвестная тема `{topic}`. Допустимы: {', '.join(RULE_TOPICS)}."
        )
    return copy.deepcopy(_RULES[topic])


__all__ = ["RULE_TOPICS", "RuleTopic", "get_metadata_authoring_rules"]
