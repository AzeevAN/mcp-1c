"""RED-контракты converter структуры CONFIG-INTAKE-V2, этап 5."""

from __future__ import annotations

import hashlib
import importlib
import io
from pathlib import Path

import pytest

from mcp1c.intake_v2 import CandidateTransport
from mcp1c.intake_v2_collector import DEFAULT_KIND_SPECS, collect_source_b
from mcp1c.intake_v2_probe import probe_export


SUBJECT = "mcp1c.intake_v2_converter"
NS = "http://v8.1c.ru/8.3/MDClasses"
NS_V8 = "http://v8.1c.ru/8.1/data/core"
NS_XS = "http://www.w3.org/2001/XMLSchema"
NS_CFG = "http://v8.1c.ru/8.1/data/enterprise/current-config"


def _symbol(name: str):
    try:
        module = importlib.import_module(SUBJECT)
    except ModuleNotFoundError as error:
        if error.name != SUBJECT:
            raise
        pytest.fail(
            f"RED: отсутствует модуль {SUBJECT} для контракта {name}"
        )
    if not hasattr(module, name):
        pytest.fail(f"RED: в {SUBJECT} отсутствует контракт {name}")
    return getattr(module, name)


def _localized(value: str, *, language: str = "ru") -> str:
    return (
        f"<v8:item><v8:lang>{language}</v8:lang>"
        f"<v8:content>{value}</v8:content></v8:item>"
    )


def _type(
    *values: str,
    string_length: int | None = None,
    allowed_length: str = "Variable",
    digits: int | None = None,
    fraction_digits: int | None = None,
    allowed_sign: str = "Any",
    date_parts: str = "DateTime",
) -> str:
    body = "".join(f"<v8:Type>{value}</v8:Type>" for value in values)
    if string_length is not None:
        body += (
            "<v8:StringQualifiers>"
            f"<v8:Length>{string_length}</v8:Length>"
            f"<v8:AllowedLength>{allowed_length}</v8:AllowedLength>"
            "</v8:StringQualifiers>"
        )
    if digits is not None:
        body += (
            "<v8:NumberQualifiers>"
            f"<v8:Digits>{digits}</v8:Digits>"
            f"<v8:FractionDigits>{fraction_digits or 0}</v8:FractionDigits>"
            f"<v8:AllowedSign>{allowed_sign}</v8:AllowedSign>"
            "</v8:NumberQualifiers>"
        )
    if "xs:dateTime" in values:
        body += (
            "<v8:DateQualifiers>"
            f"<v8:DateFractions>{date_parts}</v8:DateFractions>"
            "</v8:DateQualifiers>"
        )
    return f"<Type>{body}</Type>"


def _document(
    kind: str,
    properties: str,
    children: str = "",
) -> bytes:
    return (
        f'<MetaDataObject xmlns="{NS}" xmlns:v8="{NS_V8}" '
        f'xmlns:xs="{NS_XS}" xmlns:cfg="{NS_CFG}">'
        f"<{kind}><Properties>{properties}</Properties>"
        f"<ChildObjects>{children}</ChildObjects></{kind}>"
        "</MetaDataObject>"
    ).encode()


def _configuration(*, unknown: bool = False) -> bytes:
    extra = "<FutureFlag>Enabled</FutureFlag>" if unknown else ""
    properties = (
        "<Name>DemoConfiguration</Name>"
        "<Synonym>"
        f"{_localized('Демонстрационная конфигурация')}"
        "</Synonym>"
        "<Comment>Синтетическая конфигурация</Comment>"
        "<Vendor>Example</Vendor><Version>1.2.3</Version>"
        "<CompatibilityMode>Version8_3_24</CompatibilityMode>"
        f"{extra}"
    )
    children = (
        "<Catalog>Items</Catalog>"
        "<CommonAttribute>Tenant</CommonAttribute>"
        "<SessionParameter>Tenant</SessionParameter>"
    )
    return _document("Configuration", properties, children)


def _field(name: str, type_xml: str, *, synonym: str = "") -> str:
    synonym_xml = f"<Synonym>{_localized(synonym)}</Synonym>" if synonym else ""
    return (
        "<Attribute><Properties>"
        f"<Name>{name}</Name>{synonym_xml}<Indexing>Index</Indexing>{type_xml}"
        "</Properties></Attribute>"
    )


def _catalog(*, unknown: bool = False) -> bytes:
    extra = "<FutureProperty>value</FutureProperty>" if unknown else ""
    properties = (
        "<Name>Items</Name>"
        f"<Synonym>{_localized('Объекты')}</Synonym>"
        "<Comment>Синтетический справочник</Comment>"
        "<Hierarchical>true</Hierarchical>"
        "<HierarchyType>HierarchyFoldersAndItems</HierarchyType>"
        "<CodeLength>9</CodeLength><DescriptionLength>120</DescriptionLength>"
        f"<CodeType>String</CodeType>{extra}"
    )
    tabular = (
        "<TabularSection><Properties><Name>Lines</Name>"
        f"<Synonym>{_localized('Строки')}</Synonym></Properties><ChildObjects>"
        f"{_field('Amount', _type('xs:decimal', digits=15, fraction_digits=2))}"
        "</ChildObjects></TabularSection>"
    )
    children = (
        f"{_field('Title', _type('xs:string', string_length=80), synonym='Заголовок')}"
        f"{_field('Owner', _type('cfg:CatalogRef.Items'))}{tabular}"
    )
    return _document("Catalog", properties, children)


def _common_attribute(*, unresolved: int = 0) -> bytes:
    content = [
        "<Item><Metadata>Catalog.Items</Metadata><Use>Use</Use>"
        "<ConditionalSeparation>Constant.Enabled</ConditionalSeparation></Item>"
    ]
    content.extend(
        "<Item><Metadata>Catalog.Missing{index}</Metadata><Use>DontUse</Use>"
        "<ConditionalSeparation>Constant.Enabled</ConditionalSeparation></Item>".format(
            index=index
        )
        for index in range(unresolved)
    )
    properties = (
        "<Name>Tenant</Name>"
        f"<Synonym>{_localized('Область данных')}</Synonym>"
        "<Comment>Синтетический общий реквизит</Comment>"
        f"{_type('xs:string', string_length=40, allowed_length='Fixed')}"
        "<PasswordMode>false</PasswordMode><Format>DF=yyyy-MM-dd</Format>"
        "<EditFormat>DF=dd.MM.yyyy</EditFormat><ToolTip>Подсказка</ToolTip>"
        "<MarkNegatives>false</MarkNegatives><Mask>AA-999</Mask>"
        "<MultiLine>false</MultiLine><ExtendedEdit>true</ExtendedEdit>"
        "<FillFromFillingValue>true</FillFromFillingValue>"
        "<FillChecking>ShowError</FillChecking>"
        "<ChoiceFoldersAndItems>Items</ChoiceFoldersAndItems>"
        "<QuickChoice>Auto</QuickChoice><CreateOnInput>Use</CreateOnInput>"
        "<DataHistory>Use</DataHistory><Indexing>IndexWithAdditionalOrder</Indexing>"
        "<FullTextSearch>Use</FullTextSearch>"
        "<DataSeparation>Separate</DataSeparation>"
        "<DataSeparationValue>Independently</DataSeparationValue>"
        "<DataSeparationUse>SessionParameter.Tenant</DataSeparationUse>"
        "<ConditionalSeparation>Constant.Enabled</ConditionalSeparation>"
        "<SeparatedDataUse>Independently</SeparatedDataUse>"
        "<AutoUse>Use</AutoUse>"
        "<AuthenticationSeparation>DontUse</AuthenticationSeparation>"
        "<UsersSeparation>Separate</UsersSeparation>"
        "<ConfigurationExtensionsSeparation>DontUse</ConfigurationExtensionsSeparation>"
        "<Content>" + "".join(content) + "</Content>"
    )
    return _document("CommonAttribute", properties)


def _session_parameter() -> bytes:
    properties = (
        "<Name>Tenant</Name>"
        f"<Synonym>{_localized('Текущая область')}</Synonym>"
        "<Comment>Синтетический параметр</Comment>"
        f"{_type('xs:string', 'cfg:CatalogRef.Items', string_length=40)}"
    )
    return _document("SessionParameter", properties)


class MemoryTree:
    transport = CandidateTransport.INCOMING
    origin_name = "synthetic.zip"

    def __init__(self, payloads: dict[str, bytes]):
        self.payloads = dict(payloads)
        self._fingerprint = self._digest()

    def _digest(self) -> str:
        digest = hashlib.sha256()
        for path in sorted(self.payloads):
            digest.update(path.encode())
            digest.update(self.payloads[path])
        return digest.hexdigest()

    def paths(self) -> tuple[str, ...]:
        return tuple(sorted(self.payloads))

    def open(self, path: str):
        return io.BytesIO(self.payloads[path])

    def size(self, path: str) -> int:
        return len(self.payloads[path])

    def fingerprint(self) -> str:
        return self._fingerprint

    def source_sha256(self) -> str:
        return self._digest()

    def verify_stable(self, expected: str) -> bool:
        return expected == self._fingerprint == self._digest()


def _collection(
    tmp_path: Path,
    *,
    unknown: bool = False,
    unresolved: int = 0,
    malformed: str = "",
):
    payloads = {
        "Configuration.xml": _configuration(unknown=unknown),
        "Catalogs/Items.xml": _catalog(unknown=unknown),
        "Catalogs/Items/Ext/ObjectModule.bsl": b"procedure Demo() endprocedure",
        "Catalogs/Items/Forms/Card.xml": b"<form-descriptor/>",
        "Catalogs/Items/Forms/Card/Ext/Form/Form.xml": b"<form/>",
        "CommonAttributes/Tenant.xml": _common_attribute(unresolved=unresolved),
        "SessionParameters/Tenant.xml": _session_parameter(),
    }
    if malformed:
        payloads[malformed] = b"<MetaDataObject><broken>"
    tree = MemoryTree(payloads)
    return collect_source_b(tree, probe_export(tree), tmp_path / "collection")


def test_metadata_kind_spec_выбирает_структурный_adapter():
    specs = {item.source_name: item for item in DEFAULT_KIND_SPECS}

    assert specs["Catalogs"].base_adapter == "schema_v1"
    assert specs["Catalogs"].extended_adapter == ""
    assert specs["CommonAttributes"].base_adapter == ""
    assert specs["CommonAttributes"].extended_adapter == "common_attribute"
    assert specs["SessionParameters"].extended_adapter == "session_parameter"
    assert specs["ExchangePlans"].base_adapter == "schema_v1"
    assert specs["ExchangePlans"].extended_adapter == "exchange_plan"
    assert specs["CommonCommands"].base_adapter == ""
    assert specs["CommonCommands"].extended_adapter == ""


def test_converter_строит_base_без_дублирования_в_extended(tmp_path):
    convert_collection = _symbol("convert_collection")

    result = convert_collection(_collection(tmp_path))

    assert result.base.name == "DemoConfiguration"
    assert result.base.synonym == "Демонстрационная конфигурация"
    assert result.base.version == "1.2.3"
    assert result.base.vendor == "Example"
    catalog = result.base.get("Справочник.Items")
    assert catalog is not None
    assert catalog.synonym == "Объекты"
    assert catalog.props == {
        "hierarchical": True,
        "hierarchy_type": "ИерархияГруппИЭлементов",
        "code_length": 9,
        "description_length": 120,
        "code_type": "Строка",
    }
    assert [(field.name, field.types) for field in catalog.attributes] == [
        ("Title", ["Строка"]),
        ("Owner", ["Справочник.Items"]),
    ]
    assert catalog.attributes[0].string_length == 80
    assert catalog.attributes[0].indexing == "Индексировать"
    assert catalog.tabular_parts[0].attributes[0].types == ["Число"]
    assert catalog.tabular_parts[0].attributes[0].digits == 15
    assert catalog.tabular_parts[0].attributes[0].fraction_digits == 2

    overlay = result.extended.get("Справочник.Items")
    assert overlay is not None
    assert overlay.base_object is True
    assert overlay.payload is None
    assert overlay.modules == ("Справочник.Items.МодульОбъекта",)
    assert overlay.forms == ("Справочник.Items.Форма.Card",)


def test_common_objects_имеют_типизированный_payload(tmp_path):
    CommonAttributePayload = _symbol("CommonAttributePayload")
    SessionParameterPayload = _symbol("SessionParameterPayload")
    convert_collection = _symbol("convert_collection")

    result = convert_collection(_collection(tmp_path))

    common = result.extended.get("ОбщийРеквизит.Tenant")
    session = result.extended.get("ПараметрСеанса.Tenant")
    assert common is not None and isinstance(common.payload, CommonAttributePayload)
    assert session is not None and isinstance(session.payload, SessionParameterPayload)
    assert common.base_object is False
    assert common.code_address == "ОбщиеРеквизиты.Tenant"
    assert common.payload.value_type.types == ("Строка",)
    assert common.payload.value_type.string_length == 40
    assert common.payload.value_type.string_allowed_length == "Fixed"
    assert common.payload.indexing == "IndexWithAdditionalOrder"
    assert common.payload.full_text_search == "Use"
    assert common.payload.data_separation == "Separate"
    assert common.payload.auto_use == "Use"
    assert common.modules == () and common.forms == ()

    assert session.code_address == "ПараметрыСеанса.Tenant"
    assert session.payload.value_type.types == (
        "Строка",
        "Справочник.Items",
    )
    assert session.modules == () and session.forms == ()


def test_common_attribute_хранит_edges_а_не_копирует_поле(tmp_path):
    convert_collection = _symbol("convert_collection")

    result = convert_collection(_collection(tmp_path))
    common = result.extended.get("ОбщийРеквизит.Tenant")
    catalog = result.base.get("Справочник.Items")
    assert common is not None and catalog is not None

    assert [field.name for field in catalog.attributes] == ["Title", "Owner"]
    assert [
        (relation.kind, relation.target, relation.state.value, relation.properties)
        for relation in common.relations
    ] == [
        (
            "applies_to",
            "Справочник.Items",
            "resolved",
            (("conditional_separation", "Константа.Enabled"), ("use", "Use")),
        ),
        (
            "conditional_separation",
            "Константа.Enabled",
            "unresolved",
            (),
        ),
        (
            "data_separation_use",
            "ПараметрСеанса.Tenant",
            "resolved",
            (),
        ),
    ]


def test_unresolved_цели_агрегируются_и_ограничиваются(tmp_path):
    convert_collection = _symbol("convert_collection")

    result = convert_collection(_collection(tmp_path, unresolved=6))
    diagnostic = next(
        item for item in result.diagnostics if item.code == "unresolved_relation"
    )

    assert diagnostic.count == 7
    assert len(diagnostic.examples) == 3
    assert all("Missing" in example or "Enabled" in example for example in diagnostic.examples)


def test_unknown_property_даёт_info_но_не_меняет_semantic_hash(tmp_path):
    convert_collection = _symbol("convert_collection")

    plain = convert_collection(_collection(tmp_path / "plain"))
    future = convert_collection(_collection(tmp_path / "future", unknown=True))

    assert future.base_content_sha256 == plain.base_content_sha256
    assert future.extended_content_sha256 == plain.extended_content_sha256
    assert any(item.code == "unknown_property" for item in future.diagnostics)
    assert all(item.severity == "info" for item in future.diagnostics)


@pytest.mark.parametrize(
    "path",
    [
        "Catalogs/Items.xml",
        "CommonAttributes/Tenant.xml",
        "SessionParameters/Tenant.xml",
    ],
)
def test_malformed_descriptor_останавливает_converter(tmp_path, path):
    ConversionError = _symbol("ConversionError")
    convert_collection = _symbol("convert_collection")

    with pytest.raises(ConversionError, match="XML"):
        convert_collection(_collection(tmp_path, malformed=path))


def test_hashes_детерминированы_после_restart_collection(tmp_path):
    convert_collection = _symbol("convert_collection")
    load_collection = importlib.import_module(
        "mcp1c.intake_v2_collector"
    ).load_collection
    collection = _collection(tmp_path)

    first = convert_collection(collection)
    second = convert_collection(load_collection(collection.root))

    assert first.base_content_sha256 == second.base_content_sha256
    assert first.extended_content_sha256 == second.extended_content_sha256


def test_converter_не_следует_по_symlink_внутри_collection(tmp_path):
    ConversionError = _symbol("ConversionError")
    convert_collection = _symbol("convert_collection")
    collection = _collection(tmp_path)
    metadata = collection.root / "metadata"
    moved = collection.root / "metadata-real"
    metadata.rename(moved)
    metadata.symlink_to(moved, target_is_directory=True)

    with pytest.raises(ConversionError, match="символ|обычн"):
        convert_collection(collection)
