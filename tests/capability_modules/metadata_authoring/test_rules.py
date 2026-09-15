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
    assert register["descriptor_element"] == "md:InformationRegister"
    assert register["form_declaration_value"] == "<Форма>"
    assert register["default_form_value"] == (
        "InformationRegister.<Имя>.Form.<Форма>"
    )
    assert register["catalog_reference_type"] == "cfg:CatalogRef.<ИмяСправочника>"
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
    assert rules["form_descriptor"]["form_type"] == "Managed"
    assert rules["form_descriptor"]["name_value"] == (
        "точное короткое значение ChildObjects/Form"
    )
    assert rules["qname_prefix_scope"] == "same_xml_document"
