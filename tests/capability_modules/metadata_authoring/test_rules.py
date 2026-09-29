from typing import get_args

import pytest

from metadata_core.checker import (
    check_metadata_artifacts,
)
from metadata_core.compiler import (
    compile_metadata_object,
)
from metadata_core.rules import (
    RULE_TOPICS,
    RuleTopic,
    get_metadata_authoring_rules,
)
from pathlib import Path


def test_rules_публикуют_границу_и_пять_поддержанных_видов():
    assert RULE_TOPICS == (
        "overview",
        "catalog",
        "document",
        "data_processor",
        "report",
        "information_register",
        "forms",
        "artifacts",
        "diagnostics",
    )
    assert get_args(RuleTopic) == RULE_TOPICS
    overview = get_metadata_authoring_rules("overview")

    assert overview["status"] == "supported"
    assert overview["available_topics"] == list(RULE_TOPICS)
    assert "topic=artifacts" in overview["compiler_specification"][
        "form_xml_preparation"
    ]
    assert overview["supported_metadata_kinds"] == [
        "Справочник",
        "Документ",
        "РегистрСведений",
        "Обработка",
        "Отчет",
    ]
    assert overview["writes_files"] is False
    assert overview["imports_configuration"] is False
    assert overview["external_reference_resolution"] == "not_checked"
    assert overview["caller_responsibility"] == (
        "существование объектов и реквизитов из catalog_ref, document_ref, Объект.* и "
        "Запись.* обеспечивает caller; compiler кодирует переданное намерение, "
        "но не подтверждает цель по Registry или конфигурации"
    )
    assert overview["compiler_specification"]["required"] == [
        "schema_version",
        "object_ref",
        "format_version",
        "identity",
        "synonym",
        "attributes",
        "forms",
    ]
    assert overview["compiler_specification"]["catalog_additional_required"] == [
        "code_length",
        "description_length",
    ]
    assert set(overview["compiler_specification"]["field_type_kinds"]) == {
        "string",
        "boolean",
        "number",
        "date",
        "catalog_ref",
        "document_ref",
    }
    assert overview["compiler_specification"]["field_types"]["number"] == {
        "required": ["kind", "digits", "fraction_digits"],
        "digits": "1..32",
        "fraction_digits": "0..digits",
        "allowed_sign": "Any",
    }
    assert overview["compiler_specification"]["form_roles"] == {
        "catalog": ["object", "list", "choice"],
        "document": ["object", "list", "choice"],
        "information_register": ["record", "list", "record_set"],
        "data_processor": ["object"],
        "report": ["object"],
        "legacy_default": {
            "catalog": "object",
            "document": "object",
            "information_register": "record",
            "data_processor": "object",
            "report": "object",
        },
        "one_physical_form_one_role": True,
        "shared_list_choice_form": "not_supported",
    }
    assert "ровно одна default=true" in overview["compiler_specification"]["form_defaults"]
    assert "Cross-item правило проверяет compiler" in overview[
        "compiler_specification"
    ]["form_defaults"]
    assert "check_managed_form" in overview["forms_workflow"]
    assert "Если forms[] непуст" in overview["forms_workflow"]
    assert "get_managed_form_rules(topic=overview)" in overview[
        "forms_workflow"
    ]
    assert "provenance" in overview["forms_workflow"]
    assert "byte-for-byte программно" in overview["forms_workflow"]
    assert "не сокращайте" in overview["forms_workflow"]
    assert "не используйте minimal_shape_reference" in overview["forms_workflow"]
    assert "десятки KB является нормальным" in overview["forms_workflow"]


def test_forms_topic_публикует_обязательную_цепочку_и_границу():
    rules = get_metadata_authoring_rules("forms")

    assert rules["when_required"] == (
        "обязательно перед compile_metadata_object, если forms[] непуст"
    )
    assert rules["role_mapping"] == {
        "Справочник": ["object", "list", "choice"],
        "Документ": ["object", "list", "choice"],
        "РегистрСведений": ["record", "list", "record_set"],
        "Обработка": ["object"],
        "Отчет": ["object"],
    }
    assert "ровно одна" in rules["default_semantics"]["data_processor_override"]
    assert "default=true" in rules["default_semantics"]["data_processor_override"]
    assert rules["default_semantics"][
        "exactly_one_true_per_represented_default_capable_role"
    ] is True
    assert rules["default_semantics"]["additional_same_role_forms"] == "default=false"
    assert rules["default_semantics"]["absent_role_default_property"] == "empty_allowed"
    assert rules["default_semantics"]["enforcement"] == "compiler cross-item validation"
    assert rules["required_call_order"] == [
        "get_managed_form_rules(topic=overview)",
        "запросить перечисленные в overview предметные темы Forms",
        "compile_managed_form для каждой формы и её role",
        "для Обработка.* реализовать тела обработчиков в Module.bsl, не меняя Form.xml",
        "check_managed_form на неизменённом Form.xml и итоговом Module.bsl",
        "программно передать эти точные проверенные строки плюс ту же role в forms[]",
        "compile_metadata_object",
        "check_metadata_artifacts",
    ]
    assert rules["exact_handoff"] == {
        "source": "Form.xml из compile_managed_form; Module.bsl из compiler либо с заполненными агентом телами обработчиков Обработка.*",
        "forms_check_input": "неизменённый Form.xml и итоговый Module.bsl",
        "metadata_forms_input": "те же точные проверенные строки byte-for-byte",
        "forbidden_transformations": [
            "retype",
            "summarize",
            "truncate",
            "reconstruct",
            "minimal_shape_reference",
        ],
        "transport": "programmatic",
        "tens_of_kilobytes": "normal",
    }
    assert rules["metadata_checker_boundary"] == {
        "provenance": "not_checked",
        "semantic_role": "closed_xml_profiles_only",
        "owner_relative_bundle": "checked",
        "document_object_owner": "checked",
    }
    assert "не заменяет Forms compile/check" in rules["manual_xml"]


def test_skill_ставит_forms_workflow_до_metadata_compile():
    skill = Path(__file__).resolve().parents[3] / "skills/1c-metadata-creator/SKILL.md"
    description = skill.read_text(encoding="utf-8")
    assert "1c-form-creator" in description
    assert "Form.xml" in description
    assert "metadata_creator.py build" in description


def test_rules_фиксируют_полные_generated_types():
    catalog = get_metadata_authoring_rules("catalog")
    document = get_metadata_authoring_rules("document")
    data_processor = get_metadata_authoring_rules("data_processor")
    register = get_metadata_authoring_rules("information_register")

    assert catalog["generated_types"] == [
        "Object",
        "Ref",
        "Selection",
        "List",
        "Manager",
    ]
    assert catalog["generated_type_name_pattern"] == "Catalog<Category>.<Имя>"
    assert catalog["descriptor_element"] == "md:Catalog"
    assert catalog["form_declaration"] == "ChildObjects/Form"
    assert catalog["form_declaration_value"] == "<Форма>"
    assert catalog["default_form_value"] == "Catalog.<Имя>.Form.<Форма>"
    assert catalog["default_form_properties"] == {
        "object": "DefaultObjectForm",
        "list": "DefaultListForm",
        "choice": "DefaultChoiceForm",
    }
    assert catalog["standard_form_fields"] == {
        "Объект.Description": "обязательно при description_length > 0",
        "Объект.Code": "обязательно при code_length > 0",
        "Объект.<ИмяРеквизита>": "обязательно для каждого пользовательского реквизита",
        "Список.Description": "обязательно для default list/choice при description_length > 0",
        "Список.Code": "обязательно для default list/choice при code_length > 0",
    }
    assert "commands пустым" in catalog["standard_command_bar"]
    assert data_processor["generated_types"] == ["Object", "Manager"]
    assert data_processor["descriptor_path"] == "DataProcessors/<Имя>.xml"
    assert data_processor["default_form_property"] == "DefaultForm"
    assert data_processor["forms_count"] == 1
    assert data_processor["default_required"] is True
    assert data_processor["compiler_specification_example"]["object_ref"] == (
        "Обработка.ИмпортПример"
    )
    assert data_processor["compiler_specification_example"]["attributes"] == []
    assert data_processor["compiler_specification_example"]["forms"][0][
        "default"
    ] is True
    assert "проверенный итоговый Module.bsl" in data_processor[
        "compiler_specification_example_note"
    ]
    assert data_processor["forms_profile"]["main_attribute"] == {
        "name": "Объект",
        "type": {
            "kind": "metadata_object",
            "object": "Обработка.<Имя>",
        },
        "main": True,
        "saved_data": "не требуется",
    }
    assert ".epf" in data_processor["scope"]
    assert register["generated_types"] == [
        "Record",
        "Manager",
        "Selection",
        "List",
        "RecordSet",
        "RecordKey",
        "RecordManager",
    ]
    assert register["periodicity_property"] == "InformationRegisterPeriodicity"
    assert register["compiler_periodicity"] == "nonperiodical"
    assert register["generated_periodicity"] == "Nonperiodical"
    assert register["descriptor_element"] == "md:InformationRegister"
    assert register["form_declaration_value"] == "<Форма>"
    assert register["default_form_value"] == (
        "InformationRegister.<Имя>.Form.<Форма>"
    )
    assert register["default_form_properties"] == {
        "record": "DefaultRecordForm",
        "list": "DefaultListForm",
        "record_set": None,
    }
    assert register["record_set_default_property"] is None
    assert "default=false" in register["record_set_default_rule"]
    assert "role=record с default=true" in register["record_set_default_rule"]
    assert "DefaultRecordForm" in register["record_set_default_rule"]
    assert register["catalog_reference_type"] == "cfg:CatalogRef.<ИмяСправочника>"
    assert "commands пустым" in register["standard_command_bar"]
    assert register["generated_type_name_pattern"] == (
        "InformationRegister<Category>.<Имя>"
    )
    assert document["generated_types"] == [
        "Object", "Ref", "Selection", "List", "Manager"
    ]
    assert document["descriptor_element"] == "md:Document"
    assert document["posting"] == "Deny"
    assert document["real_time_posting"] == "Deny"
    assert document["default_form_properties"] == {
        "object": "DefaultObjectForm",
        "list": "DefaultListForm",
        "choice": "DefaultChoiceForm",
    }
    assert document["document_reference_type"] == "cfg:DocumentRef.<ИмяДокумента>"
    assert document["forms_profile"]["context"] == {
        "owner": "Документ.<Имя>",
        "role": "object",
    }
    assert document["forms_profile"]["main_attribute"] == {
        "name": "Объект",
        "type": {
            "kind": "metadata_object",
            "object": "Документ.<Имя>",
        },
        "main": True,
    }
    assert "Объект.Date" in document["forms_profile"]["standard_field_rule"]
    assert document["forms_profile"]["commands"] == []
    assert document["forms_profile"]["events"] == []
    assert document["forms_workflow"] == [
        "compile_managed_form",
        "check_managed_form",
        "передать content Form.xml и Module.bsl в compile_metadata_object.forms[]",
        "check_metadata_artifacts",
    ]
    assert "не собирайте Form.xml вручную" in document["forms_handoff"]
    assert "semantic role" in document["metadata_checker_boundary"]


@pytest.mark.parametrize("topic", ["catalog", "document", "information_register"])
def test_тематический_пример_проходит_compile_и_check(topic):
    topic_rules = get_metadata_authoring_rules(topic)
    specification = topic_rules["compiler_specification_example"]

    assert "shape placeholder" in topic_rules["compiler_specification_example_note"]
    assert "compile_managed_form" in topic_rules["compiler_specification_example_note"]
    assert "check_managed_form" in topic_rules["compiler_specification_example_note"]

    compiled = compile_metadata_object(specification)
    checked = check_metadata_artifacts(
        compiled["object_ref"], compiled["format_version"], compiled["artifacts"]
    )

    assert compiled["status"] == "compiled"
    assert checked["status"] == "passed"


def test_artifact_rules_достаточны_для_сборки_bundle_без_чтения_кода():
    rules = get_metadata_authoring_rules("artifacts")

    assert rules["namespaces"] == {
        "md": "http://v8.1c.ru/8.3/MDClasses",
        "xr": "http://v8.1c.ru/8.3/xcf/readable",
        "v8": "http://v8.1c.ru/8.1/data/core",
        "xs": "http://www.w3.org/2001/XMLSchema",
        "logform": "http://v8.1c.ru/8.3/xcf/logform",
    }
    assert rules["generated_type"] == {
        "element": "xr:GeneratedType",
        "attributes": ["name", "category"],
        "children": ["xr:TypeId", "xr:ValueId"],
        "uuid_unique_across_bundle": True,
    }
    assert rules["compiler_to_checker"] == {
        "tool": "check_metadata_artifacts",
        "arguments": {
            "object_ref": "compile result.object_ref",
            "format_version": "compile result.format_version",
            "artifacts": "compile result.artifacts",
        },
        "legacy_artifacts_mapping_supported": True,
    }
    assert rules["required_base"] == ["<owner>.xml"]
    assert "configuration_registration" not in rules
    assert rules["form_descriptor"]["form_type"] == "Managed"
    assert rules["form_descriptor"]["name_value"] == (
        "точное короткое значение ChildObjects/Form"
    )
    assert rules["default_form_references"] == {
        "empty_allowed_only_for_absent_role": True,
        "must_be_owner_relative": True,
        "must_reference_declared_form": True,
        "semantic_role_from_form_xml": "closed_xml_profiles_only",
    }
    assert rules["form_xml"] == {
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
    }
    assert rules["qname_prefix_scope"] == "same_xml_document"
