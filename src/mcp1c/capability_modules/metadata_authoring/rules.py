"""Компактный контракт создания файлов метаданных без внешних эффектов."""

from __future__ import annotations

import copy
from typing import Literal, TypeAlias


RuleTopic: TypeAlias = Literal[
    "overview",
    "catalog",
    "document",
    "information_register",
    "artifacts",
    "diagnostics",
]

RULE_TOPICS: tuple[RuleTopic, ...] = (
    "overview",
    "catalog",
    "document",
    "information_register",
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

_RULES: dict[RuleTopic, dict[str, object]] = {
    "overview": {
        "status": "supported",
        "available_topics": list(RULE_TOPICS),
        "supported_metadata_kinds": ["Справочник", "Документ", "РегистрСведений"],
        "recommended_call_order": [
            "get_metadata_authoring_rules",
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
                "default",
                "form_xml",
                "module_bsl",
            ],
            "form_xml_preparation": (
                "до первой compile с forms запросите topic=artifacts: там "
                "опубликованы точный корень, namespace, version и минимальный "
                "статический пример Form.xml"
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
            "без форм списка и выбора",
        ],
        "forms_handoff": (
            "не собирайте Form.xml вручную: сначала вызовите compile_managed_form "
            "для Документ.<Имя> + role=object, проверьте результат через "
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
        "attribute_declaration": "ChildObjects/Attribute[@uuid]",
        "generated_types": _CATALOG_TYPES,
        "generated_type_name_pattern": "Catalog<Category>.<Имя>",
        "default_form_property": "DefaultObjectForm",
        "standard_form_fields": {
            "Объект.Наименование": "обязательно при description_length > 0",
            "Объект.Код": "обязательно при code_length > 0",
        },
        "standard_command_bar": (
            "Не объявляйте пользовательские команды Записать/ЗаписатьИЗакрыть: "
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
            "minimal_static_example": (
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
