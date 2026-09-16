from mcp1c.capability_modules.metadata_authoring.rules import (
    RULE_TOPICS,
    get_metadata_authoring_rules,
)


def test_rules_публикуют_границу_и_два_поддержанных_вида():
    assert RULE_TOPICS == (
        "overview",
        "catalog",
        "information_register",
        "artifacts",
        "diagnostics",
    )
    overview = get_metadata_authoring_rules("overview")

    assert overview["status"] == "supported"
    assert overview["supported_metadata_kinds"] == [
        "Справочник",
        "РегистрСведений",
    ]
    assert overview["writes_files"] is False
    assert overview["imports_configuration"] is False
    assert overview["external_reference_resolution"] == "not_checked"
    assert overview["caller_responsibility"] == (
        "существование объектов и реквизитов из catalog_ref, Объект.* и "
        "Запись.* обеспечивает caller; compiler кодирует переданное намерение, "
        "но не подтверждает цель по Registry или конфигурации"
    )
    assert overview["compiler_specification"]["required"] == [
        "schema_version",
        "object_ref",
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
    }
    assert overview["compiler_specification"]["field_types"]["number"] == {
        "required": ["kind", "digits", "fraction_digits"],
        "digits": "1..32",
        "fraction_digits": "0..digits",
        "allowed_sign": "Any",
    }


def test_rules_фиксируют_полные_generated_types():
    catalog = get_metadata_authoring_rules("catalog")
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
    assert catalog["standard_form_fields"] == {
        "Объект.Наименование": "рекомендуется при description_length > 0",
        "Объект.Код": "рекомендуется при code_length > 0",
    }
    assert "commands пустым" in catalog["standard_command_bar"]
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
    assert register["catalog_reference_type"] == "cfg:CatalogRef.<ИмяСправочника>"
    assert "commands пустым" in register["standard_command_bar"]
    assert register["generated_type_name_pattern"] == (
        "InformationRegister<Category>.<Имя>"
    )


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
    assert rules["configuration_registration"] == (
        "md:Configuration/md:ChildObjects/<Catalog|InformationRegister>"
    )
    assert rules["compiler_to_checker"] == {
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
    }
    assert rules["form_descriptor"]["form_type"] == "Managed"
    assert rules["form_descriptor"]["name_value"] == (
        "точное короткое значение ChildObjects/Form"
    )
    assert rules["form_xml"] == {
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
    }
    assert rules["qname_prefix_scope"] == "same_xml_document"
