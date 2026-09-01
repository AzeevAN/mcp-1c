"""RED-контракты converter структуры CONFIG-INTAKE-V2, этап 5."""

from __future__ import annotations

import hashlib
import importlib
import io
from pathlib import Path

import pytest

from module_samples import v8_container_bytes
from mcp1c.intake_v2 import CandidateTransport
from mcp1c.intake_v2_collector import DEFAULT_KIND_SPECS, collect_source_b
from mcp1c.intake_v2_probe import probe_export


SUBJECT = "mcp1c.intake_v2_converter"
NS = "http://v8.1c.ru/8.3/MDClasses"
NS_V8 = "http://v8.1c.ru/8.1/data/core"
NS_XS = "http://www.w3.org/2001/XMLSchema"
NS_CFG = "http://v8.1c.ru/8.1/data/enterprise/current-config"
NS_EXTERNAL_PROPERTIES = "http://v8.1c.ru/8.3/xcf/extrnprops"


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


def _configuration(
    *,
    unknown: bool = False,
    journal: bool = False,
    bindings: bool = False,
    common_forms: bool = False,
    bots: bool = False,
) -> bytes:
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
    if journal:
        children += (
            "<Document>Invoice</Document>"
            "<DocumentJournal>Ledger</DocumentJournal>"
        )
    if bindings:
        children += (
            "<ExchangePlan>Nodes</ExchangePlan>"
            "<CommonModule>Handlers</CommonModule>"
            "<EventSubscription>Resolved</EventSubscription>"
            "<EventSubscription>ModuleMissing</EventSubscription>"
            "<EventSubscription>ProcedureMissing</EventSubscription>"
            "<EventSubscription>Unresolved</EventSubscription>"
            "<ScheduledJob>Refresh</ScheduledJob>"
        )
    if common_forms:
        children += (
            "<CommonForm>Workspace</CommonForm>"
            "<CommonForm>Container</CommonForm>"
            "<CommonForm>Unreadable</CommonForm>"
            "<CommonForm>DescriptorOnly</CommonForm>"
            "<CommonForm>Flat</CommonForm>"
        )
    if bots:
        children += "<Bot>Assistant</Bot>"
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


def _base_document() -> bytes:
    properties = (
        "<Name>Invoice</Name>"
        f"<Synonym>{_localized('Документ')}</Synonym>"
        "<Posting>Allow</Posting><NumberLength>11</NumberLength>"
    )
    children = _field(
        "Amount",
        _type("xs:decimal", digits=15, fraction_digits=2),
        synonym="Сумма",
    )
    return _document("Document", properties, children)


def _document_journal(*, malformed_reference: bool = False) -> bytes:
    bad_reference = (
        "Document.Invoice.Broken.Amount"
        if malformed_reference
        else "Document.Invoice.Attribute.Missing"
    )
    properties = (
        "<Name>Ledger</Name>"
        f"<Synonym>{_localized('Журнал')}</Synonym>"
        "<Comment>Синтетический журнал</Comment>"
        f"<ListPresentation>{_localized('Список журнала')}</ListPresentation>"
        f"<ExtendedListPresentation>{_localized('Полный список')}</ExtendedListPresentation>"
        "<DefaultForm>DocumentJournal.Ledger.Form.Card</DefaultForm>"
        "<RegisteredDocuments><v8:Item>Document.Invoice</v8:Item>"
        "<v8:Item>Document.Missing</v8:Item></RegisteredDocuments>"
        "<StandardAttributes><StandardAttribute name=\"Date\">"
        f"<Synonym>{_localized('Дата')}</Synonym>"
        "<Comment>Дата документа</Comment><PasswordMode>false</PasswordMode>"
        "<Format/><EditFormat/><ToolTip/><MarkNegatives>false</MarkNegatives>"
        "<Mask/><MultiLine>false</MultiLine><ExtendedEdit>false</ExtendedEdit>"
        "<FillFromFillingValue>false</FillFromFillingValue>"
        "<FillChecking>ShowError</FillChecking><QuickChoice>Auto</QuickChoice>"
        "<CreateOnInput>Use</CreateOnInput><DataHistory>Use</DataHistory>"
        "<FullTextSearch>Use</FullTextSearch>"
        "<TypeReductionMode>Transform</TypeReductionMode>"
        "</StandardAttribute></StandardAttributes>"
    )
    columns = (
        "<Column><Properties><Name>Amount</Name>"
        f"<Synonym>{_localized('Сумма')}</Synonym>"
        "<Indexing>IndexWithAdditionalOrder</Indexing>"
        "<References><v8:Item>Document.Invoice.Attribute.Amount</v8:Item>"
        "</References></Properties></Column>"
        "<Column><Properties><Name>Empty</Name><References/>"
        "</Properties></Column>"
        "<Column><Properties><Name>Missing</Name>"
        f"<References><v8:Item>{bad_reference}</v8:Item></References>"
        "</Properties></Column>"
    )
    command = (
        "<Command><Properties><Name>Refresh</Name>"
        f"<Synonym>{_localized('Обновить')}</Synonym>"
        f"<CommandParameterType><v8:Type>cfg:DocumentRef.Invoice</v8:Type>"
        "</CommandParameterType><Group>CommandGroup.NavigationPanel</Group>"
        "<ModifiesData>false</ModifiesData><Representation>Auto</Representation>"
        "</Properties></Command>"
    )
    return _document(
        "DocumentJournal",
        properties,
        columns + command + "<Form>Card</Form><Template>Print</Template>",
    )


def _exchange_plan() -> bytes:
    properties = (
        "<Name>Nodes</Name>"
        f"<Synonym>{_localized('Узлы')}</Synonym>"
        "<Comment>Синтетический план обмена</Comment>"
        "<DistributedInfoBase>true</DistributedInfoBase>"
        "<CodeLength>9</CodeLength><CodeAllowedLength>Variable</CodeAllowedLength>"
        "<DescriptionLength>120</DescriptionLength>"
        f"<ListPresentation>{_localized('Список узлов')}</ListPresentation>"
        "<DefaultObjectForm>ExchangePlan.Nodes.Form.Card</DefaultObjectForm>"
        "<UseStandardCommands>true</UseStandardCommands>"
        "<IncludeConfigurationExtensions>false</IncludeConfigurationExtensions>"
        "<IncludeHelpInContents>true</IncludeHelpInContents>"
        "<InputByString><Field>ExchangePlan.Nodes.StandardAttribute.Code</Field>"
        "</InputByString>"
    )
    tabular = (
        "<TabularSection><Properties><Name>Lines</Name>"
        f"<Synonym>{_localized('Строки')}</Synonym></Properties><ChildObjects>"
        f"{_field('Amount', _type('xs:decimal', digits=15, fraction_digits=2))}"
        "</ChildObjects></TabularSection>"
    )
    command = (
        "<Command><Properties><Name>Refresh</Name>"
        f"<Synonym>{_localized('Обновить')}</Synonym>"
        "<ModifiesData>false</ModifiesData><Representation>Auto</Representation>"
        "</Properties></Command>"
    )
    children = (
        f"{_field('Address', _type('xs:string', string_length=80))}"
        f"{tabular}{command}<Form>Card</Form><Template>Message</Template>"
    )
    return _document("ExchangePlan", properties, children)


def _exchange_plan_content(auto_record: str = "Allow") -> bytes:
    return (
        f'<ExchangePlanContent xmlns="{NS_EXTERNAL_PROPERTIES}" xmlns:v8="{NS_V8}">'
        "<Item><Metadata>Catalog.Items</Metadata>"
        f"<AutoRecord>{auto_record}</AutoRecord></Item>"
        "<Item><Metadata>Document.Missing</Metadata>"
        "<AutoRecord>Deny</AutoRecord></Item>"
        "</ExchangePlanContent>"
    ).encode()


def _common_module() -> bytes:
    properties = (
        "<Name>Handlers</Name>"
        f"<Synonym>{_localized('Обработчики')}</Synonym>"
        "<Global>false</Global><Server>true</Server>"
        "<ClientManagedApplication>false</ClientManagedApplication>"
        "<ServerCall>true</ServerCall><Privileged>false</Privileged>"
        "<ExternalConnection>true</ExternalConnection>"
        "<ReturnValuesReuse>DontUse</ReturnValuesReuse>"
    )
    return _document("CommonModule", properties)


def _event_subscription(name: str, handler: str) -> bytes:
    properties = (
        f"<Name>{name}</Name><Comment>Синтетическая подписка</Comment>"
        "<Source><v8:Type>cfg:CatalogObject.Items</v8:Type></Source>"
        "<Event>BeforeWrite</Event>"
        f"<Handler>{handler}</Handler>"
    )
    return _document("EventSubscription", properties)


def _scheduled_job() -> bytes:
    properties = (
        "<Name>Refresh</Name><Comment>Синтетическое задание</Comment>"
        "<MethodName>CommonModule.Handlers.RunJob</MethodName>"
        "<Description>Обновляет синтетический индекс</Description>"
        "<Use>true</Use><Predefined>false</Predefined><Key>refresh</Key>"
        "<RestartCountOnFailure>3</RestartCountOnFailure>"
        "<RestartIntervalOnFailure>60</RestartIntervalOnFailure>"
    )
    return _document("ScheduledJob", properties)


def _common_form(name: str) -> bytes:
    properties = (
        f"<Name>{name}</Name>"
        f"<Synonym>{_localized('Общая форма')}</Synonym>"
        "<Comment>Синтетическая общая форма</Comment>"
        f"<Explanation>{_localized('Пояснение формы')}</Explanation>"
        f"<ExtendedPresentation>{_localized('Расширенное представление')}</ExtendedPresentation>"
        "<FormType>Managed</FormType><IncludeHelpInContents>true</IncludeHelpInContents>"
        "<UsePurposes><v8:Value>PlatformApplication</v8:Value>"
        "<v8:Value>MobilePlatformClient</v8:Value></UsePurposes>"
        "<UseStandardCommands>false</UseStandardCommands>"
    )
    root = _document("CommonForm", properties).decode()
    return root.replace("<CommonForm>", f'<CommonForm uuid="uuid-{name}">').encode()


def _common_form_xml() -> bytes:
    return (
        '<Form xmlns="http://v8.1c.ru/8.3/xcf/logform">'
        '<Attributes><Attribute name="Filter"><Columns>'
        '<Column name="NestedColumn"/></Columns></Attribute></Attributes>'
        '<Events><Event name="OnOpen">OnOpen</Event></Events>'
        '<ChildItems><InputField name="FilterField">'
        '<Events><Event name="OnChange">OnChange</Event></Events>'
        '<ExtendedTooltip name="GeneratedTooltip"/>'
        '</InputField><UsualGroup name="Pages"><Button name="Refresh">'
        '<Events><Event name="Click">Refresh</Event></Events>'
        '</Button></UsualGroup></ChildItems></Form>'
    ).encode()


def _bot(*, predefined: str = "true", unknown: bool = False) -> bytes:
    extra = "<FutureBotProperty>value</FutureBotProperty>" if unknown else ""
    properties = (
        "<Name>Assistant</Name>"
        f"<Synonym>{_localized('Бот-помощник')}</Synonym>"
        "<Comment>Синтетический бот</Comment>"
        "<Picture><v8:Ref>CommonPicture.Bot</v8:Ref></Picture>"
        f"<Predefined>{predefined}</Predefined>{extra}"
    )
    root = _document("Bot", properties, "<Form>Invented</Form>").decode()
    return root.replace("<Bot>", '<Bot uuid="uuid-Assistant">').encode()


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
    journal: bool = False,
    malformed_journal_reference: bool = False,
    bindings: bool = False,
    plan_auto_record: str = "Allow",
    plan_content: bool = True,
    common_forms: bool = False,
    common_form_external_module: bytes | None = None,
    bots: bool = False,
    bot_module: bool = True,
    bot_module_name: str = "Assistant",
    bot_unknown: bool = False,
    bot_predefined: str = "true",
    schedule: bytes = b"<Schedule><Period>60</Period></Schedule>",
):
    payloads = {
        "Configuration.xml": _configuration(
            unknown=unknown,
            journal=journal,
            bindings=bindings,
            common_forms=common_forms,
            bots=bots,
        ),
        "Catalogs/Items.xml": _catalog(unknown=unknown),
        "Catalogs/Items/Ext/ObjectModule.bsl": b"procedure Demo() endprocedure",
        "Catalogs/Items/Forms/Card.xml": b"<form-descriptor/>",
        "Catalogs/Items/Forms/Card/Ext/Form.xml": b"<form/>",
        "CommonAttributes/Tenant.xml": _common_attribute(unresolved=unresolved),
        "SessionParameters/Tenant.xml": _session_parameter(),
    }
    if journal:
        payloads.update(
            {
                "Documents/Invoice.xml": _base_document(),
                "DocumentJournals/Ledger.xml": _document_journal(
                    malformed_reference=malformed_journal_reference
                ),
                "DocumentJournals/Ledger/Ext/ManagerModule.bsl": b"procedure Manager() endprocedure",
                "DocumentJournals/Ledger/Commands/Refresh/Ext/CommandModule.bsl": (
                    b"procedure Run() endprocedure"
                ),
                "DocumentJournals/Ledger/Forms/Card.xml": b"<form-descriptor/>",
                "DocumentJournals/Ledger/Forms/Card/Ext/Form.xml": b"<form/>",
                "DocumentJournals/Ledger/Forms/Card/Ext/Form/Module.bsl": b"procedure Form() endprocedure",
                "DocumentJournals/Ledger/Templates/Print.xml": b"<template-descriptor/>",
                "DocumentJournals/Ledger/Templates/Print/Ext/Template.xml": b"<template/>",
            }
        )
    if bindings:
        payloads.update(
            {
                "ExchangePlans/Nodes.xml": _exchange_plan(),
                "ExchangePlans/Nodes/Ext/ObjectModule.bsl": (
                    b"procedure ObjectMethod() endprocedure"
                ),
                "ExchangePlans/Nodes/Ext/ManagerModule.bsl": (
                    b"procedure ManagerMethod() endprocedure"
                ),
                "ExchangePlans/Nodes/Commands/Refresh/Ext/CommandModule.bsl": (
                    b"procedure Run() endprocedure"
                ),
                "ExchangePlans/Nodes/Forms/Card.xml": b"<form-descriptor/>",
                "ExchangePlans/Nodes/Forms/Card/Ext/Form.xml": b"<form/>",
                "ExchangePlans/Nodes/Forms/Card/Ext/Form/Module.bsl": (
                    b"procedure FormMethod() endprocedure"
                ),
                "ExchangePlans/Nodes/Templates/Message.xml": b"<template/>",
                "CommonModules/Handlers.xml": _common_module(),
                "CommonModules/Handlers/Ext/Module.bsl": (
                    "Процедура OnWrite() Экспорт\nКонецПроцедуры\n"
                    "Процедура RunJob() Экспорт\nКонецПроцедуры\n"
                ).encode(),
                "EventSubscriptions/Resolved.xml": _event_subscription(
                    "Resolved", "CommonModule.Handlers.OnWrite"
                ),
                "EventSubscriptions/ModuleMissing.xml": _event_subscription(
                    "ModuleMissing", "CommonModule.Absent.OnWrite"
                ),
                "EventSubscriptions/ProcedureMissing.xml": _event_subscription(
                    "ProcedureMissing", "CommonModule.Handlers.Absent"
                ),
                "EventSubscriptions/Unresolved.xml": _event_subscription(
                    "Unresolved", ""
                ),
                "ScheduledJobs/Refresh.xml": _scheduled_job(),
                "ScheduledJobs/Refresh/Ext/Schedule.xml": schedule,
            }
        )
        if plan_content:
            payloads[
                "ExchangePlans/Nodes/Ext/Content.xml"
            ] = _exchange_plan_content(plan_auto_record)
    if common_forms:
        payloads.update(
            {
                "CommonForms/Workspace.xml": _common_form("Workspace"),
                "CommonForms/Workspace/Ext/Form.xml": _common_form_xml(),
                "CommonForms/Workspace/Ext/Form/Module.bsl": (
                    "Процедура OnOpen()\nКонецПроцедуры\n"
                    "Процедура OnChange()\nКонецПроцедуры\n"
                    "Процедура Refresh()\nКонецПроцедуры\n"
                ).encode(),
                "CommonForms/Container.xml": _common_form("Container"),
                "CommonForms/Container/Ext/Form.bin": v8_container_bytes(
                    [
                        ("form", b"{19}"),
                        (
                            "module",
                            b"procedure ContainerHandler() endprocedure",
                        ),
                    ]
                ),
                "CommonForms/Unreadable.xml": _common_form("Unreadable"),
                "CommonForms/Unreadable/Ext/Form.bin": b"broken",
                "CommonForms/DescriptorOnly.xml": _common_form("DescriptorOnly"),
                "CommonForm.Flat.Form": v8_container_bytes(
                    [
                        ("form", b"{19}"),
                        ("module", b"procedure FlatHandler() endprocedure"),
                    ]
                ),
            }
        )
        if common_form_external_module is not None:
            payloads[
                "CommonForms/Container/Ext/Form/Module.bsl"
            ] = common_form_external_module
    if bots:
        payloads["Bots/Assistant.xml"] = _bot(
            predefined=bot_predefined,
            unknown=bot_unknown,
        )
        if bot_module:
            payloads[f"Bots/{bot_module_name}/Ext/Module.bsl"] = (
                b"procedure Reply() endprocedure"
            )
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
    assert specs["EventSubscriptions"].extended_adapter == "event_subscription"
    assert specs["ScheduledJobs"].extended_adapter == "scheduled_job"
    assert specs["CommonForms"].extended_adapter == "common_form"
    assert specs["Bots"].extended_adapter == "bot"
    assert specs["Bots"].layouts == frozenset({"tree"})
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


def test_document_journal_хранит_свойства_и_пустую_графу(tmp_path):
    DocumentJournalPayload = _symbol("DocumentJournalPayload")
    convert_collection = _symbol("convert_collection")

    result = convert_collection(_collection(tmp_path, journal=True))
    journal = result.extended.get("ЖурналДокументов.Ledger")

    assert journal is not None and isinstance(journal.payload, DocumentJournalPayload)
    assert journal.payload.list_presentation == "Список журнала"
    assert journal.payload.extended_list_presentation == "Полный список"
    assert journal.payload.default_form == "ЖурналДокументов.Ledger.Форма.Card"
    assert journal.payload.registered_documents == (
        "Документ.Invoice",
        "Документ.Missing",
    )
    assert journal.payload.standard_attributes[0].name == "Date"
    assert journal.payload.standard_attributes[0].type_reduction_mode == "Transform"
    assert journal.payload.standard_attributes[0].password_mode is False
    assert journal.payload.standard_attributes[0].full_text_search == "Use"
    assert [column.name for column in journal.payload.columns] == [
        "Amount",
        "Empty",
        "Missing",
    ]
    assert journal.payload.columns[1].references == ()
    assert (
        journal.payload.columns[0].indexing
        == "ИндексироватьСДопУпорядочиванием"
    )
    assert result.base.get("ЖурналДокументов.Ledger") is None


def test_document_journal_разрешает_типы_граф_без_копирования(tmp_path):
    convert_collection = _symbol("convert_collection")
    resolve_journal_column_types = _symbol("resolve_journal_column_types")

    result = convert_collection(_collection(tmp_path, journal=True))
    journal = result.extended.get("ЖурналДокументов.Ledger")
    assert journal is not None
    amount, empty, missing = journal.payload.columns

    assert amount.references[0].raw == "Document.Invoice.Attribute.Amount"
    assert amount.references[0].target == "Документ.Invoice.Amount"
    assert resolve_journal_column_types(result.base, amount) == ("Число",)
    assert resolve_journal_column_types(result.base, empty) == ()
    assert resolve_journal_column_types(result.base, missing) == ()
    assert [field.name for field in result.base.get("Документ.Invoice").attributes] == [
        "Amount"
    ]


def test_document_journal_связывает_состав_формы_и_весь_код(tmp_path):
    convert_collection = _symbol("convert_collection")

    result = convert_collection(_collection(tmp_path, journal=True))
    journal = result.extended.get("ЖурналДокументов.Ledger")
    assert journal is not None

    assert journal.payload.forms == ("Card",)
    assert journal.payload.templates == ("Print",)
    assert [command.name for command in journal.payload.commands] == ["Refresh"]
    assert journal.payload.commands[0].parameter_type.types == (
        "Документ.Invoice",
    )
    assert journal.payload.commands[0].modifies_data is False
    assert journal.modules == (
        "ЖурналДокументов.Ledger.Команда.Refresh",
        "ЖурналДокументов.Ledger.МодульМенеджера",
        "ЖурналДокументов.Ledger.Форма.Card",
    )
    assert journal.forms == ("ЖурналДокументов.Ledger.Форма.Card",)
    assert not any(
        item.code == "unhandled_metadata_member"
        and item.signature == "DocumentJournals"
        for item in result.diagnostics
    )


def test_document_journal_диагностирует_отсутствующие_цели(tmp_path):
    convert_collection = _symbol("convert_collection")

    result = convert_collection(_collection(tmp_path, journal=True))
    journal = result.extended.get("ЖурналДокументов.Ledger")
    assert journal is not None
    unresolved = [
        relation for relation in journal.relations if relation.state.value == "unresolved"
    ]

    assert {(item.kind, item.target) for item in unresolved} == {
        ("column_field", "Документ.Invoice.Missing"),
        ("registers_document", "Документ.Missing"),
    }
    assert {
        (item.kind, item.target)
        for item in journal.relations
        if item.state.value == "resolved"
    } == {
        ("column_field", "Документ.Invoice.Amount"),
        ("registers_document", "Документ.Invoice"),
    }
    diagnostic = next(
        item for item in result.diagnostics if item.code == "unresolved_relation"
    )
    # Ещё один unresolved даёт общий реквизит базовой синтетической фикстуры.
    assert diagnostic.count == 3


def test_document_journal_отвергает_неточную_field_ref(tmp_path):
    ConversionError = _symbol("ConversionError")
    convert_collection = _symbol("convert_collection")

    with pytest.raises(ConversionError, match="field ref"):
        convert_collection(
            _collection(
                tmp_path,
                journal=True,
                malformed_journal_reference=True,
            )
        )


def test_exchange_plan_сохраняет_base_content_и_весь_фактический_контент(tmp_path):
    ExchangePlanPayload = _symbol("ExchangePlanPayload")
    convert_collection = _symbol("convert_collection")

    result = convert_collection(_collection(tmp_path, bindings=True))
    base = result.base.get("ПланОбмена.Nodes")
    plan = result.extended.get("ПланОбмена.Nodes")

    assert base is not None and plan is not None
    assert base.props["distributed_infobase"] is True
    assert [field.name for field in base.attributes] == ["Address"]
    assert [part.name for part in base.tabular_parts] == ["Lines"]
    assert plan.base_object is True
    assert isinstance(plan.payload, ExchangePlanPayload)
    assert plan.payload.code_length == 9
    assert plan.payload.description_length == 120
    assert plan.payload.default_object_form == "ПланОбмена.Nodes.Форма.Card"
    assert plan.payload.input_by_string == (
        "ExchangePlan.Nodes.StandardAttribute.Code",
    )
    assert [item.name for item in plan.payload.commands] == ["Refresh"]
    assert plan.payload.forms == ("Card",)
    assert plan.payload.templates == ("Message",)
    assert [
        (item.raw, item.target, item.auto_record.value)
        for item in plan.payload.content
    ] == [
        ("Catalog.Items", "Справочник.Items", "Allow"),
        ("Document.Missing", "Документ.Missing", "Deny"),
    ]
    assert plan.modules == (
        "ПланОбмена.Nodes.Команда.Refresh",
        "ПланОбмена.Nodes.МодульМенеджера",
        "ПланОбмена.Nodes.МодульОбъекта",
        "ПланОбмена.Nodes.Форма.Card",
    )
    assert plan.forms == ("ПланОбмена.Nodes.Форма.Card",)
    assert {
        (relation.target, relation.state.value) for relation in plan.relations
    } >= {
        ("Справочник.Items", "resolved"),
        ("Документ.Missing", "unresolved"),
    }


def test_exchange_plan_отвергает_неизвестную_политику_autorecord(tmp_path):
    ConversionError = _symbol("ConversionError")
    convert_collection = _symbol("convert_collection")

    with pytest.raises(ConversionError, match="AutoRecord"):
        convert_collection(
            _collection(tmp_path, bindings=True, plan_auto_record="Maybe")
        )


def test_exchange_plan_требует_content_xml(tmp_path):
    ConversionError = _symbol("ConversionError")
    convert_collection = _symbol("convert_collection")

    with pytest.raises(ConversionError, match="Content.xml"):
        convert_collection(
            _collection(tmp_path, bindings=True, plan_content=False)
        )


def test_subscription_resolver_различает_четыре_состояния(tmp_path):
    BindingState = _symbol("BindingState")
    EventSubscriptionPayload = _symbol("EventSubscriptionPayload")
    convert_collection = _symbol("convert_collection")

    result = convert_collection(_collection(tmp_path, bindings=True))
    expected = {
        "Resolved": BindingState.RESOLVED,
        "ModuleMissing": BindingState.MODULE_MISSING,
        "ProcedureMissing": BindingState.PROCEDURE_MISSING,
        "Unresolved": BindingState.UNRESOLVED,
    }
    for name, state in expected.items():
        subscription = result.extended.get(f"ПодпискаНаСобытие.{name}")
        assert subscription is not None
        assert isinstance(subscription.payload, EventSubscriptionPayload)
        assert subscription.payload.binding.state is state
        assert subscription.modules == () and subscription.forms == ()

    resolved = result.extended.get("ПодпискаНаСобытие.Resolved")
    assert resolved.payload.binding.raw == "CommonModule.Handlers.OnWrite"
    assert resolved.payload.binding.module_address == "ОбщийМодуль.Handlers"
    assert (
        resolved.payload.binding.procedure_address
        == "ОбщийМодуль.Handlers::OnWrite"
    )
    base = result.base.get("ПодпискаНаСобытие.Resolved")
    assert base.props["source"] == ["Справочник.Items"]
    assert base.props["event"] == "BeforeWrite"
    assert base.props["handler"] == "CommonModule.Handlers.OnWrite"
    assert [(item.kind, item.target, item.state.value) for item in resolved.relations] == [
        ("event_source", "Справочник.Items", "resolved")
    ]


def test_scheduled_job_сохраняет_restart_и_разрешённый_метод(tmp_path):
    ScheduledJobPayload = _symbol("ScheduledJobPayload")
    convert_collection = _symbol("convert_collection")

    result = convert_collection(_collection(tmp_path, bindings=True))
    job = result.extended.get("РегламентноеЗадание.Refresh")

    assert job is not None and isinstance(job.payload, ScheduledJobPayload)
    assert job.base_object is True
    assert job.payload.description == "Обновляет синтетический индекс"
    assert job.payload.restart_count_on_failure == 3
    assert job.payload.restart_interval_on_failure == 60
    assert job.payload.binding.raw == "CommonModule.Handlers.RunJob"
    assert job.payload.binding.state.value == "resolved"
    assert job.payload.binding.procedure_address == "ОбщийМодуль.Handlers::RunJob"
    assert job.modules == () and job.forms == ()


def test_schedule_xml_меняет_raw_sha_но_не_semantic_hashes(tmp_path):
    convert_collection = _symbol("convert_collection")
    first_collection = _collection(
        tmp_path / "first",
        bindings=True,
        schedule=b"<Schedule><Period>60</Period></Schedule>",
    )
    second_collection = _collection(
        tmp_path / "second",
        bindings=True,
        schedule=b"<Schedule><Period>120</Period></Schedule>",
    )

    first = convert_collection(first_collection)
    second = convert_collection(second_collection)

    assert first_collection.probe.raw_sha256 != second_collection.probe.raw_sha256
    assert first.base_content_sha256 == second.base_content_sha256
    assert first.extended_content_sha256 == second.extended_content_sha256
    assert not any(
        "Schedule.xml" in example
        for item in (*first_collection.diagnostics, *first.diagnostics)
        for example in item.examples
    )


def test_common_form_сохраняет_descriptor_структуру_модуль_и_handlers(tmp_path):
    FormStructureState = _symbol("FormStructureState")
    FormModuleState = _symbol("FormModuleState")
    CommonFormPayload = _symbol("CommonFormPayload")
    convert_collection = _symbol("convert_collection")

    result = convert_collection(_collection(tmp_path, common_forms=True))
    form = result.extended.get("ОбщаяФорма.Workspace")

    assert form is not None and isinstance(form.payload, CommonFormPayload)
    assert form.payload.uuid == "uuid-Workspace"
    assert form.payload.form_type == "Managed"
    assert form.payload.explanation == "Пояснение формы"
    assert form.payload.extended_presentation == "Расширенное представление"
    assert form.payload.include_help_in_contents is True
    assert form.payload.use_purposes == (
        "PlatformApplication",
        "MobilePlatformClient",
    )
    assert form.payload.use_standard_commands is False
    assert form.payload.structure_state is FormStructureState.READY
    assert form.payload.module_state is FormModuleState.READY
    assert form.payload.attributes == ("Filter",)
    assert form.payload.elements == ("FilterField", "Pages", "Refresh")
    assert [
        (
            item.element,
            item.event,
            item.handler,
            item.binding.state.value,
            item.binding.procedure_address,
        )
        for item in form.payload.events
    ] == [
        (None, "OnOpen", "OnOpen", "resolved", "ОбщаяФорма.Workspace::OnOpen"),
        (
            "FilterField",
            "OnChange",
            "OnChange",
            "resolved",
            "ОбщаяФорма.Workspace::OnChange",
        ),
        (
            "Refresh",
            "Click",
            "Refresh",
            "resolved",
            "ОбщаяФорма.Workspace::Refresh",
        ),
    ]
    assert form.modules == ("ОбщаяФорма.Workspace",)
    assert form.forms == ("ОбщаяФорма.Workspace",)


def test_common_form_явно_различает_partial_unreadable_descriptor_only(tmp_path):
    FormStructureState = _symbol("FormStructureState")
    FormModuleState = _symbol("FormModuleState")
    convert_collection = _symbol("convert_collection")

    result = convert_collection(_collection(tmp_path, common_forms=True))
    container = result.extended.get("ОбщаяФорма.Container")
    unreadable = result.extended.get("ОбщаяФорма.Unreadable")
    descriptor_only = result.extended.get("ОбщаяФорма.DescriptorOnly")
    flat = result.extended.get("ОбщаяФорма.Flat")

    assert container.payload.structure_state is FormStructureState.PARTIAL
    assert container.payload.module_state is FormModuleState.READY
    assert container.payload.container_marker == 19
    assert container.payload.attributes == ()
    assert container.payload.elements == ()
    assert container.payload.events == ()

    assert unreadable.payload.structure_state is FormStructureState.UNREADABLE
    assert unreadable.payload.module_state is FormModuleState.UNREADABLE
    assert descriptor_only.payload.structure_state is FormStructureState.DESCRIPTOR_ONLY
    assert descriptor_only.payload.module_state is FormModuleState.MISSING
    assert flat.payload.structure_state is FormStructureState.PARTIAL
    assert flat.payload.module_state is FormModuleState.READY
    assert flat.payload.container_marker == 19
    assert {
        item.signature
        for item in result.diagnostics
        if item.code == "form_coverage"
    } == {"descriptor_only", "module_missing", "partial", "unreadable"}


def test_common_form_не_скрывает_конфликт_модулей(tmp_path):
    FormModuleState = _symbol("FormModuleState")
    convert_collection = _symbol("convert_collection")

    result = convert_collection(
        _collection(
            tmp_path,
            common_forms=True,
            common_form_external_module=(
                b"procedure DifferentHandler() endprocedure"
            ),
        )
    )

    form = result.extended.get("ОбщаяФорма.Container")
    assert form is not None
    assert form.payload.module_state is FormModuleState.UNREADABLE
    assert any(
        item.code == "ambiguous_code_module"
        and item.signature == "CommonForm"
        for item in result.diagnostics
    )


def test_common_form_локализует_битый_form_xml(tmp_path):
    FormStructureState = _symbol("FormStructureState")
    FormModuleState = _symbol("FormModuleState")
    convert_collection = _symbol("convert_collection")

    result = convert_collection(
        _collection(
            tmp_path,
            common_forms=True,
            malformed="CommonForms/Workspace/Ext/Form.xml",
        )
    )

    form = result.extended.get("ОбщаяФорма.Workspace")
    assert form is not None
    assert form.payload.structure_state is FormStructureState.UNREADABLE
    assert form.payload.module_state is FormModuleState.READY


def test_bot_сохраняет_descriptor_и_фактический_модуль(tmp_path):
    BotPayload = _symbol("BotPayload")
    BotModuleState = _symbol("BotModuleState")
    convert_collection = _symbol("convert_collection")

    result = convert_collection(_collection(tmp_path, bots=True))
    bot = result.extended.get("Бот.Assistant")

    assert bot is not None and isinstance(bot.payload, BotPayload)
    assert bot.payload.uuid == "uuid-Assistant"
    assert bot.payload.picture == ("CommonPicture.Bot",)
    assert bot.payload.predefined is True
    assert bot.payload.module_state is BotModuleState.PRESENT
    assert bot.code_address == "Бот.Assistant"
    assert bot.modules == ("Бот.Assistant",)
    assert bot.forms == ()
    assert bot.relations == ()
    assert any(
        item.code == "unknown_child" and item.signature == "Bot"
        for item in result.diagnostics
    )


def test_bot_явно_показывает_отсутствие_модуля_и_unknown(tmp_path):
    BotModuleState = _symbol("BotModuleState")
    convert_collection = _symbol("convert_collection")

    result = convert_collection(
        _collection(tmp_path, bots=True, bot_module=False, bot_unknown=True)
    )
    bot = result.extended.get("Бот.Assistant")

    assert bot is not None
    assert bot.payload.module_state is BotModuleState.MISSING
    assert {
        (item.code, item.signature)
        for item in result.diagnostics
    } >= {
        ("bot_coverage", "module_missing"),
        ("unknown_property", "Bot"),
    }


def test_bot_не_угадывает_неверный_predefined(tmp_path):
    convert_collection = _symbol("convert_collection")
    ConversionError = _symbol("ConversionError")

    with pytest.raises(ConversionError, match="Predefined"):
        convert_collection(
            _collection(tmp_path, bots=True, bot_predefined="sometimes")
        )


def test_bot_не_скрывает_расхождение_регистра_descriptor_и_модуля(tmp_path):
    convert_collection = _symbol("convert_collection")
    ConversionError = _symbol("ConversionError")

    with pytest.raises(ConversionError, match="адрес модуля бота"):
        convert_collection(
            _collection(tmp_path, bots=True, bot_module_name="assistant")
        )
