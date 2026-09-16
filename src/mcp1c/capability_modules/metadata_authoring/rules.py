"""Компактный контракт создания файлов метаданных без внешних эффектов."""

from __future__ import annotations

import copy
from typing import Literal, TypeAlias


RuleTopic: TypeAlias = Literal[
    "overview",
    "catalog",
    "information_register",
    "artifacts",
    "diagnostics",
]

RULE_TOPICS: tuple[RuleTopic, ...] = (
    "overview",
    "catalog",
    "information_register",
    "artifacts",
    "diagnostics",
)

_CATALOG_TYPES = ["Object", "Ref", "Selection", "List", "Manager"]
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
        "supported_metadata_kinds": ["Справочник", "РегистрСведений"],
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
                "identity",
                "synonym",
                "attributes",
                "forms",
            ],
            "schema_version": 1,
            "identity": "UUID",
            "field": ["name", "synonym", "type"],
            "field_type_kinds": [
                "string",
                "boolean",
                "number",
                "date",
                "catalog_ref",
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
            },
            "form": [
                "name",
                "synonym",
                "default",
                "form_xml",
                "module_bsl",
            ],
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
        },
        "configuration_boundary": (
            "компилятор возвращает инструкцию регистрации, но не создаёт и не "
            "перезаписывает Configuration.xml; checker может применить её к "
            "переданной копии configuration_xml только в памяти"
        ),
        "recommended_preparation": (
            "до compile получите фактическую структуру целевой конфигурации "
            "core-инструментами MCP и сформируйте specification по найденным данным"
        ),
        "external_reference_resolution": "not_checked",
        "caller_responsibility": (
            "существование объектов и реквизитов из catalog_ref, Объект.* и "
            "Запись.* обеспечивает caller; compiler кодирует переданное намерение, "
            "но не подтверждает цель по Registry или конфигурации"
        ),
        "writes_files": False,
        "imports_configuration": False,
        "native_import_proven": False,
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
        "configuration_child": "Catalog",
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
        "configuration_child": "InformationRegister",
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
    },
    "artifacts": {
        "paths_are_owner_relative": True,
        "namespaces": {
            "md": "http://v8.1c.ru/8.3/MDClasses",
            "xr": "http://v8.1c.ru/8.3/xcf/readable",
            "v8": "http://v8.1c.ru/8.1/data/core",
            "xs": "http://www.w3.org/2001/XMLSchema",
            "logform": "http://v8.1c.ru/8.3/xcf/logform",
        },
        "descriptor_root": "md:MetaDataObject",
        "configuration_registration": (
            "md:Configuration/md:ChildObjects/<Catalog|InformationRegister>"
        ),
        "compiler_to_checker": {
            "tool": "check_metadata_artifacts",
            "arguments": {
                "object_ref": "compile result.object_ref",
                "artifacts": "compile result.artifacts",
                "configuration_xml": (
                    "текст существующего Configuration.xml целевой конфигурации"
                ),
                "configuration_registration": (
                    "compile result.configuration_registration"
                ),
            },
            "registration_is_in_memory_only": True,
            "configuration_xml_is_not_modified": True,
            "configuration_source": (
                "Configuration.xml передаётся либо в artifacts, либо отдельно; два источника отклоняются"
            ),
            "configuration_pair_required_together": [
                "configuration_xml",
                "configuration_registration",
            ],
            "legacy_artifacts_mapping_supported": True,
        },
        "generated_type": {
            "element": "xr:GeneratedType",
            "attributes": ["name", "category"],
            "children": ["xr:TypeId", "xr:ValueId"],
            "uuid_unique_across_bundle": True,
        },
        "qname_prefix_scope": "same_xml_document",
        "required_base": ["Configuration.xml", "<owner>.xml"],
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
            "not_descriptor_root": "md:MetaDataObject",
            "minimal_static_example": (
                '<Form xmlns="http://v8.1c.ru/8.3/xcf/logform">'
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
            "registration",
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
