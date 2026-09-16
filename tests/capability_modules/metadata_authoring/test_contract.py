from __future__ import annotations

from mcp1c.capability_modules.metadata_authoring.checker import (
    check_metadata_artifacts,
)


MD = "http://v8.1c.ru/8.3/MDClasses"
XR = "http://v8.1c.ru/8.3/xcf/readable"
V8 = "http://v8.1c.ru/8.1/data/core"
XS = "http://www.w3.org/2001/XMLSchema"


def _generated(
    categories: tuple[str, ...], prefix: str, object_name: str
) -> str:
    return "\n".join(
        (
            f'<xr:GeneratedType name="{prefix}{category}.{object_name}" category="{category}">'
            f"<xr:TypeId>00000000-0000-0000-0000-{index:012d}</xr:TypeId>"
            f"<xr:ValueId>10000000-0000-0000-0000-{index:012d}</xr:ValueId>"
            "</xr:GeneratedType>"
        )
        for index, category in enumerate(categories, 1)
    )


def _catalog_artifacts(*, include_xs: bool = True) -> dict[str, str]:
    xs = f' xmlns:xs="{XS}"' if include_xs else ""
    descriptor = f'''<MetaDataObject xmlns="{MD}" xmlns:xr="{XR}" xmlns:v8="{V8}"{xs} version="2.20">
<Catalog uuid="20000000-0000-0000-0000-000000000001">
<InternalInfo>{_generated(("Object", "Ref", "Selection", "List", "Manager"), "Catalog", "ТестовыйСправочник")}</InternalInfo>
<Properties><Name>ТестовыйСправочник</Name><CodeLength>9</CodeLength><DescriptionLength>150</DescriptionLength><DefaultObjectForm>Catalog.ТестовыйСправочник.Form.ФормаЭлемента</DefaultObjectForm></Properties>
<ChildObjects><Form>ФормаЭлемента</Form><Attribute uuid="20000000-0000-0000-0000-000000000002"><Properties><Name>Артикул</Name><Type><v8:Type>xs:string</v8:Type></Type></Properties></Attribute></ChildObjects>
</Catalog></MetaDataObject>'''
    return {
        "Catalogs/ТестовыйСправочник.xml": descriptor,
        "Catalogs/ТестовыйСправочник/Forms/ФормаЭлемента.xml": f'''<MetaDataObject xmlns="{MD}" xmlns:v8="{V8}" version="2.20"><Form uuid="20000000-0000-0000-0000-000000000003"><Properties><Name>ФормаЭлемента</Name><FormType>Managed</FormType></Properties></Form></MetaDataObject>''',
        "Catalogs/ТестовыйСправочник/Forms/ФормаЭлемента/Ext/Form.xml": '<Form xmlns="http://v8.1c.ru/8.3/xcf/logform" version="2.20"><ChildItems><InputField><DataPath>Объект.Наименование</DataPath></InputField><InputField><DataPath>Объект.Код</DataPath></InputField></ChildItems></Form>',
        "Catalogs/ТестовыйСправочник/Forms/ФормаЭлемента/Ext/Form/Module.bsl": "",
    }


def _information_register_artifacts() -> dict[str, str]:
    categories = (
        "Record",
        "Manager",
        "Selection",
        "List",
        "RecordSet",
        "RecordKey",
        "RecordManager",
    )
    descriptor = f'''<MetaDataObject xmlns="{MD}" xmlns:xr="{XR}" xmlns:v8="{V8}" xmlns:cfg="{MD}" version="2.20">
<InformationRegister uuid="30000000-0000-0000-0000-000000000001">
<InternalInfo>{_generated(categories, "InformationRegister", "ТестовыйРегистр")}</InternalInfo>
<Properties><Name>ТестовыйРегистр</Name><InformationRegisterPeriodicity>Nonperiodical</InformationRegisterPeriodicity><DefaultRecordForm>InformationRegister.ТестовыйРегистр.Form.ФормаЗаписи</DefaultRecordForm></Properties>
<ChildObjects><Form>ФормаЗаписи</Form><Dimension uuid="30000000-0000-0000-0000-000000000002"><Properties><Name>Справочник</Name><Type><v8:Type>cfg:CatalogRef.ТестовыйСправочник</v8:Type></Type></Properties></Dimension></ChildObjects>
</InformationRegister></MetaDataObject>'''
    return {
        "InformationRegisters/ТестовыйРегистр.xml": descriptor,
        "InformationRegisters/ТестовыйРегистр/Forms/ФормаЗаписи.xml": f'''<MetaDataObject xmlns="{MD}" version="2.20"><Form uuid="30000000-0000-0000-0000-000000000003"><Properties><Name>ФормаЗаписи</Name><FormType>Managed</FormType></Properties></Form></MetaDataObject>''',
        "InformationRegisters/ТестовыйРегистр/Forms/ФормаЗаписи/Ext/Form.xml": '<Form xmlns="http://v8.1c.ru/8.3/xcf/logform" version="2.20"/>',
        "InformationRegisters/ТестовыйРегистр/Forms/ФормаЗаписи/Ext/Form/Module.bsl": "",
    }


def _codes(result: dict[str, object]) -> set[str]:
    return {item["code"] for item in result["diagnostics"]}


def test_catalog_bundle_проходит_полную_проверку():
    result = check_metadata_artifacts(
        "Справочник.ТестовыйСправочник", "2.20", _catalog_artifacts()
    )

    assert result["status"] == "passed"
    assert set(result["coverage"].values()) == {"passed"}


def test_catalog_bundle_без_обязательного_стандартного_поля_отклоняется():
    artifacts = _catalog_artifacts()
    path = "Catalogs/ТестовыйСправочник/Forms/ФормаЭлемента/Ext/Form.xml"
    artifacts[path] = artifacts[path].replace(
        "<InputField><DataPath>Объект.Наименование</DataPath></InputField>",
        "",
    )

    result = check_metadata_artifacts(
        "Справочник.ТестовыйСправочник", "2.20", artifacts
    )

    assert result["status"] == "failed"
    assert "required_standard_field_missing" in _codes(result)


def test_catalog_bundle_с_чужой_версией_формы_отклоняется():
    artifacts = _catalog_artifacts()
    path = "Catalogs/ТестовыйСправочник/Forms/ФормаЭлемента/Ext/Form.xml"
    artifacts[path] = artifacts[path].replace('version="2.20"', 'version="2.16"')

    result = check_metadata_artifacts(
        "Справочник.ТестовыйСправочник", "2.20", artifacts
    )

    assert result["status"] == "failed"
    assert ("format_version_mismatch", path) in {
        (item["code"], item["path"]) for item in result["diagnostics"]
    }


def test_catalog_bundle_без_версии_формы_отклоняется():
    artifacts = _catalog_artifacts()
    path = "Catalogs/ТестовыйСправочник/Forms/ФормаЭлемента/Ext/Form.xml"
    artifacts[path] = artifacts[path].replace(' version="2.20"', "")

    result = check_metadata_artifacts(
        "Справочник.ТестовыйСправочник", "2.20", artifacts
    )

    assert result["status"] == "failed"
    assert ("missing_format_version", path) in {
        (item["code"], item["path"]) for item in result["diagnostics"]
    }


def test_catalog_bundle_без_версии_owner_descriptor_отклоняется():
    artifacts = _catalog_artifacts()
    path = "Catalogs/ТестовыйСправочник.xml"
    artifacts[path] = artifacts[path].replace(
        ' version="2.20"', ""
    )

    result = check_metadata_artifacts(
        "Справочник.ТестовыйСправочник", "2.20", artifacts
    )

    assert result["status"] == "failed"
    assert ("missing_format_version", path) in {
        (item["code"], item["path"]) for item in result["diagnostics"]
    }


def test_catalog_bundle_с_чужой_версией_owner_descriptor_отклоняется():
    artifacts = _catalog_artifacts()
    path = "Catalogs/ТестовыйСправочник.xml"
    artifacts[path] = artifacts[path].replace('version="2.20"', 'version="2.16"')

    result = check_metadata_artifacts(
        "Справочник.ТестовыйСправочник", "2.20", artifacts
    )

    assert ("format_version_mismatch", path) in {
        (item["code"], item["path"]) for item in result["diagnostics"]
    }


def test_catalog_bundle_с_чужой_версией_descriptor_формы_отклоняется():
    artifacts = _catalog_artifacts()
    path = "Catalogs/ТестовыйСправочник/Forms/ФормаЭлемента.xml"
    artifacts[path] = artifacts[path].replace('version="2.20"', 'version="2.16"')

    result = check_metadata_artifacts(
        "Справочник.ТестовыйСправочник", "2.20", artifacts
    )

    assert ("format_version_mismatch", path) in {
        (item["code"], item["path"]) for item in result["diagnostics"]
    }


def test_checker_требует_корректную_format_version():
    result = check_metadata_artifacts(
        "Справочник.ТестовыйСправочник", "2", _catalog_artifacts()
    )

    assert result["status"] == "failed"
    assert ("invalid_format_version", "$format_version") in {
        (item["code"], item["path"]) for item in result["diagnostics"]
    }


def test_catalog_bundle_сообщает_о_каждой_форме_чужой_версии():
    artifacts = _catalog_artifacts()
    descriptor_path = "Catalogs/ТестовыйСправочник.xml"
    artifacts[descriptor_path] = artifacts[descriptor_path].replace(
        "<Form>ФормаЭлемента</Form>",
        "<Form>ФормаЭлемента</Form><Form>ДополнительнаяФорма</Form>",
    )
    first_path = "Catalogs/ТестовыйСправочник/Forms/ФормаЭлемента/Ext/Form.xml"
    artifacts[first_path] = artifacts[first_path].replace(
        'version="2.20"', 'version="2.16"'
    )
    artifacts["Catalogs/ТестовыйСправочник/Forms/ДополнительнаяФорма.xml"] = f'''<MetaDataObject xmlns="{MD}" version="2.16"><Form uuid="20000000-0000-0000-0000-000000000004"><Properties><Name>ДополнительнаяФорма</Name><FormType>Managed</FormType></Properties></Form></MetaDataObject>'''
    second_path = "Catalogs/ТестовыйСправочник/Forms/ДополнительнаяФорма/Ext/Form.xml"
    artifacts[second_path] = (
        '<Form xmlns="http://v8.1c.ru/8.3/xcf/logform" version="2.16"/>'
    )
    artifacts[
        "Catalogs/ТестовыйСправочник/Forms/ДополнительнаяФорма/Ext/Form/Module.bsl"
    ] = ""

    result = check_metadata_artifacts(
        "Справочник.ТестовыйСправочник", "2.20", artifacts
    )

    mismatches = [
        item
        for item in result["diagnostics"]
        if item["code"] == "format_version_mismatch"
    ]
    assert [item["path"] for item in mismatches] == [
        first_path,
        second_path,
        "Catalogs/ТестовыйСправочник/Forms/ДополнительнаяФорма.xml",
    ]


def test_information_register_bundle_проходит_полную_проверку():
    result = check_metadata_artifacts(
        "РегистрСведений.ТестовыйРегистр",
        "2.20",
        _information_register_artifacts(),
    )

    assert result["status"] == "passed"
    assert set(result["coverage"].values()) == {"passed"}


def test_catalog_без_двух_generated_types_отклоняется():
    artifacts = _catalog_artifacts()
    artifacts["Catalogs/ТестовыйСправочник.xml"] = artifacts[
        "Catalogs/ТестовыйСправочник.xml"
    ].replace(
        _generated(
            ("Object", "Ref", "Selection", "List", "Manager"),
            "Catalog",
            "ТестовыйСправочник",
        ),
        _generated(
            ("Object", "Ref", "Selection"),
            "Catalog",
            "ТестовыйСправочник",
        ),
    )

    result = check_metadata_artifacts("Справочник.ТестовыйСправочник", "2.20", artifacts)

    assert result["status"] == "failed"
    assert "missing_generated_type" in _codes(result)


def test_qname_xs_без_namespace_отклоняется():
    result = check_metadata_artifacts(
        "Справочник.ТестовыйСправочник",
        "2.20",
        _catalog_artifacts(include_xs=False),
    )

    assert result["status"] == "failed"
    assert "undeclared_qname_prefix" in _codes(result)


def test_generated_type_с_неверным_name_отклоняется():
    artifacts = _catalog_artifacts()
    path = "Catalogs/ТестовыйСправочник.xml"
    artifacts[path] = artifacts[path].replace(
        "CatalogManager.ТестовыйСправочник", "WrongManager.ТестовыйСправочник"
    )

    result = check_metadata_artifacts("Справочник.ТестовыйСправочник", "2.20", artifacts)

    assert result["status"] == "failed"
    assert "generated_type_name_mismatch" in _codes(result)


def test_internal_form_xml_с_неверным_namespace_отклоняется():
    artifacts = _catalog_artifacts()
    path = "Catalogs/ТестовыйСправочник/Forms/ФормаЭлемента/Ext/Form.xml"
    artifacts[path] = '<Form xmlns="urn:wrong"/>'

    result = check_metadata_artifacts("Справочник.ТестовыйСправочник", "2.20", artifacts)

    assert result["status"] == "failed"
    assert "invalid_form_xml_root" in _codes(result)


def test_физическое_имя_catalogs_нельзя_использовать_как_cfg_тип():
    artifacts = _information_register_artifacts()
    path = "InformationRegisters/ТестовыйРегистр.xml"
    artifacts[path] = artifacts[path].replace(
        "cfg:CatalogRef.ТестовыйСправочник",
        "cfg:Catalogs.ТестовыйСправочник",
    )

    result = check_metadata_artifacts(
        "РегистрСведений.ТестовыйРегистр", "2.20", artifacts
    )

    assert result["status"] == "failed"
    assert "unsupported_metadata_type" in _codes(result)


def test_не_owner_relative_путь_descriptor_формы_отклоняется():
    artifacts = _catalog_artifacts()
    form = artifacts.pop(
        "Catalogs/ТестовыйСправочник/Forms/ФормаЭлемента.xml"
    )
    artifacts["Catalogs/Forms/ФормаЭлемента.xml"] = form

    result = check_metadata_artifacts("Справочник.ТестовыйСправочник", "2.20", artifacts)

    assert result["status"] == "failed"
    assert "non_owner_relative_artifact" in _codes(result)


def test_configuration_xml_отклоняется_как_внешний_артефакт():
    artifacts = _catalog_artifacts()
    artifacts["Configuration.xml"] = f'<MetaDataObject xmlns="{MD}"><Configuration><ChildObjects/></Configuration></MetaDataObject>'

    result = check_metadata_artifacts("Справочник.ТестовыйСправочник", "2.20", artifacts)

    assert result["status"] == "failed"
    assert "external_configuration_not_supported" in _codes(result)
