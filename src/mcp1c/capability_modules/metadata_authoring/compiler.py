"""Детерминированная сборка owner-relative артефактов метаданных 1С."""

from __future__ import annotations

from dataclasses import dataclass
from html import escape
from io import StringIO
import re
import uuid
import xml.etree.ElementTree as ET

from .checker import MAX_ARTIFACTS, MAX_ARTIFACT_BYTES, MAX_TOTAL_BYTES


MD = "http://v8.1c.ru/8.3/MDClasses"
APP = "http://v8.1c.ru/8.2/managed-application/core"
CFG = "http://v8.1c.ru/8.1/data/enterprise/current-config"
V8 = "http://v8.1c.ru/8.1/data/core"
XR = "http://v8.1c.ru/8.3/xcf/readable"
XS = "http://www.w3.org/2001/XMLSchema"
XSI = "http://www.w3.org/2001/XMLSchema-instance"
LOGFORM = "http://v8.1c.ru/8.3/xcf/logform"
_NAME = re.compile(r"^[A-Za-zА-Яа-яЁё_][A-Za-zА-Яа-яЁё0-9_]*$")
_OBJECT_REF = re.compile(r"^(Справочник|РегистрСведений)\.([A-Za-zА-Яа-яЁё_][A-Za-zА-Яа-яЁё0-9_]*)$")
_FORMAT_VERSION = re.compile(r"^\d+\.\d+(?:\.\d+)*$")
_FORBIDDEN_XML = re.compile(r"<!\s*(?:DOCTYPE|ENTITY)\b", re.I)


def _root_attributes(format_version: str) -> str:
    return (
        f'xmlns="{MD}" xmlns:app="{APP}" xmlns:cfg="{CFG}" '
        f'xmlns:v8="{V8}" xmlns:xr="{XR}" xmlns:xs="{XS}" '
        f'xmlns:xsi="{XSI}" version="{format_version}"'
    )


class MetadataAuthoringContractError(ValueError):
    """Ошибка закрытого входного контракта компилятора."""

    def __init__(self, diagnostics: list[dict[str, str]]) -> None:
        self.diagnostics = diagnostics
        super().__init__("Спецификация метаданных не прошла проверку контракта.")


@dataclass(frozen=True, slots=True)
class _Kind:
    ru: str
    xml: str
    directory: str
    fields: tuple[tuple[str, str], ...]
    generated_types: tuple[str, ...]
    allowed: frozenset[str]


_COMMON = frozenset({
    "schema_version", "object_ref", "format_version", "identity", "synonym",
    "attributes", "forms",
})
_KINDS = {
    "Справочник": _Kind(
        "Справочник", "Catalog", "Catalogs", (("attributes", "Attribute"),),
        ("Object", "Ref", "Selection", "List", "Manager"),
        _COMMON | {"code_length", "description_length"},
    ),
    "РегистрСведений": _Kind(
        "РегистрСведений", "InformationRegister", "InformationRegisters",
        (("dimensions", "Dimension"), ("resources", "Resource"), ("attributes", "Attribute")),
        ("Record", "Manager", "Selection", "List", "RecordSet", "RecordKey", "RecordManager"),
        _COMMON | {"periodicity", "dimensions", "resources"},
    ),
}


def _fail(code: str, path: str, message: str) -> None:
    raise MetadataAuthoringContractError(
        [{"status": "failed", "code": code, "path": path, "message": message}]
    )


def _mapping(value: object, path: str) -> dict[str, object]:
    if not isinstance(value, dict) or any(not isinstance(key, str) for key in value):
        _fail("invalid_type", path, "Ожидается объект со строковыми именами полей.")
    return value


def _strict(value: dict[str, object], allowed: set[str] | frozenset[str], path: str) -> None:
    unknown = sorted(set(value) - set(allowed))
    if unknown:
        _fail("unknown_field", f"{path}.{unknown[0]}", f"Неизвестное поле `{unknown[0]}`.")


def _required(value: dict[str, object], names: tuple[str, ...], path: str) -> None:
    for name in names:
        if name not in value:
            _fail("missing_required_field", f"{path}.{name}", f"Отсутствует обязательное поле `{name}`.")


def _text(value: object, path: str, *, name: bool = False) -> str:
    if not isinstance(value, str) or not value or (name and _NAME.fullmatch(value) is None):
        _fail("invalid_value", path, "Ожидается непустая строка" + (" с корректным именем 1С." if name else "."))
    return value


def _artifact_size(value: str, path: str) -> int:
    try:
        size = len(value.encode("utf-8"))
    except UnicodeEncodeError:
        _fail("invalid_unicode", path, "Артефакт содержит недопустимый Unicode.")
    if size > MAX_ARTIFACT_BYTES:
        _fail(
            "artifact_too_large",
            path,
            f"Артефакт превышает {MAX_ARTIFACT_BYTES} байт.",
        )
    return size


def _list(value: object, path: str) -> list[object]:
    if not isinstance(value, list):
        _fail("invalid_type", path, "Ожидается массив.")
    return value


def _validate_type(raw: object, path: str) -> dict[str, object]:
    value = _mapping(raw, path)
    _required(value, ("kind",), path)
    kind = value["kind"]
    if kind == "string":
        _strict(value, {"kind", "length", "allowed_length"}, path)
        length = value.get("length")
        if not isinstance(length, int) or isinstance(length, bool) or not 1 <= length <= 1024:
            _fail("invalid_value", f"{path}.length", "length должен быть целым числом от 1 до 1024.")
        allowed_length = value.get("allowed_length", "Variable")
        if allowed_length != "Variable":
            _fail("invalid_value", f"{path}.allowed_length", "Поддерживается только `Variable`.")
    elif kind == "boolean":
        _strict(value, {"kind"}, path)
    elif kind == "number":
        _strict(value, {"kind", "digits", "fraction_digits", "allowed_sign"}, path)
        digits = value.get("digits")
        fraction = value.get("fraction_digits")
        if not isinstance(digits, int) or isinstance(digits, bool) or not 1 <= digits <= 32:
            _fail("invalid_value", f"{path}.digits", "digits должен быть целым числом от 1 до 32.")
        if not isinstance(fraction, int) or isinstance(fraction, bool) or not 0 <= fraction <= digits:
            _fail("invalid_value", f"{path}.fraction_digits", "fraction_digits должен быть от 0 до digits.")
        if value.get("allowed_sign", "Any") != "Any":
            _fail("invalid_value", f"{path}.allowed_sign", "Поддерживается только `Any`.")
    elif kind == "date":
        _strict(value, {"kind", "fractions"}, path)
        if value.get("fractions") not in {"date", "time", "date_time"}:
            _fail("invalid_value", f"{path}.fractions", "Допустимы date, time и date_time.")
    elif kind == "catalog_ref":
        _strict(value, {"kind", "object"}, path)
        object_ref = value.get("object")
        if not isinstance(object_ref, str) or re.fullmatch(r"Справочник\.[A-Za-zА-Яа-яЁё_][A-Za-zА-Яа-яЁё0-9_]*", object_ref) is None:
            _fail("invalid_value", f"{path}.object", "Ожидается `Справочник.<Имя>`.")
    else:
        _fail("unsupported_field_type", f"{path}.kind", "Тип поля не поддержан схемой v1.")
    return value


def _validate_field(raw: object, path: str, xml_kind: str) -> dict[str, object]:
    value = _mapping(raw, path)
    allowed = {"name", "synonym", "type"}
    if xml_kind == "Dimension":
        allowed.add("main_filter")
    _strict(value, allowed, path)
    _required(value, ("name", "synonym", "type"), path)
    _text(value["name"], f"{path}.name", name=True)
    _text(value["synonym"], f"{path}.synonym")
    _validate_type(value["type"], f"{path}.type")
    if "main_filter" in value and not isinstance(value["main_filter"], bool):
        _fail("invalid_type", f"{path}.main_filter", "main_filter должен быть boolean.")
    return value


def _validate_form(
    raw: object, path: str, format_version: str
) -> dict[str, object]:
    value = _mapping(raw, path)
    _strict(value, {"name", "synonym", "default", "form_xml", "module_bsl"}, path)
    _required(value, ("name", "synonym", "default", "form_xml", "module_bsl"), path)
    _text(value["name"], f"{path}.name", name=True)
    _text(value["synonym"], f"{path}.synonym")
    if not isinstance(value["default"], bool):
        _fail("invalid_type", f"{path}.default", "default должен быть boolean.")
    form_xml = _text(value["form_xml"], f"{path}.form_xml")
    if _FORBIDDEN_XML.search(form_xml):
        _fail("forbidden_xml_declaration", f"{path}.form_xml", "DTD и ENTITY запрещены.")
    try:
        root = ET.fromstring(form_xml)
    except (ET.ParseError, ValueError) as error:
        _fail("invalid_form_xml", f"{path}.form_xml", f"Form.xml не разобран: {error}.")
    if root.tag != f"{{{LOGFORM}}}Form":
        _fail("invalid_form_xml_root", f"{path}.form_xml", "Корень должен быть logform Form.")
    actual_version = root.attrib.get("version", "").strip()
    if not actual_version:
        _fail(
            "missing_form_format_version",
            f"{path}.form_xml",
            "Корень Form.xml обязан содержать атрибут version.",
        )
    if actual_version != format_version:
        _fail(
            "form_format_version_mismatch",
            f"{path}.form_xml",
            (
                f"Версия Form.xml `{actual_version}` не совпадает с "
                f"specification.format_version `{format_version}`."
            ),
        )
    attributes_node = root.find(f"{{{LOGFORM}}}Attributes")
    form_attributes = [] if attributes_node is None else list(attributes_node)
    main_names = {
        str(attribute.get("name"))
        for attribute in form_attributes
        if attribute.findtext(f"{{{LOGFORM}}}MainAttribute") == "true"
    }
    data_paths = {
        node.text
        for node in root.iter(f"{{{LOGFORM}}}DataPath")
        if node.text
    }
    for main_name in main_names:
        used_object_fields = {
            data_path.split(".", 2)[1].casefold()
            for data_path in data_paths
            if data_path.startswith(main_name + ".")
        }
        shadowed = next(
            (
                str(attribute.get("name"))
                for attribute in form_attributes
                if str(attribute.get("name")) != main_name
                and str(attribute.get("name")).casefold() in used_object_fields
            ),
            None,
        )
        if shadowed is not None:
            _fail(
                "shadowed_main_object_attribute",
                f"{path}.form_xml",
                f"Реквизит `{shadowed}` уже доступен как `{main_name}.{shadowed}` "
                "и не должен дублироваться собственным реквизитом формы.",
            )
    if not isinstance(value["module_bsl"], str):
        _fail("invalid_type", f"{path}.module_bsl", "module_bsl должен быть строкой.")
    _artifact_size(form_xml, f"{path}.form_xml")
    _artifact_size(value["module_bsl"], f"{path}.module_bsl")
    return value


def _validate(specification: object) -> tuple[dict[str, object], _Kind, str, uuid.UUID]:
    value = _mapping(specification, "$specification")
    _required(value, ("schema_version", "object_ref", "format_version", "identity", "synonym", "attributes", "forms"), "$specification")
    if value["schema_version"] != 1 or isinstance(value["schema_version"], bool):
        _fail("unsupported_schema_version", "$specification.schema_version", "Поддерживается только schema_version=1.")
    object_ref = _text(value["object_ref"], "$specification.object_ref")
    match = _OBJECT_REF.fullmatch(object_ref)
    if match is None:
        _fail("unsupported_object_ref", "$specification.object_ref", "Поддержаны Справочник.<Имя> и РегистрСведений.<Имя>.")
    kind = _KINDS[match.group(1)]
    _strict(value, kind.allowed, "$specification")
    format_version = _text(value["format_version"], "$specification.format_version")
    if _FORMAT_VERSION.fullmatch(format_version) is None:
        _fail(
            "invalid_format_version",
            "$specification.format_version",
            "format_version должен иметь вид числовой точечной версии, например `2.20`.",
        )
    if kind.ru == "Справочник":
        _required(
            value,
            ("code_length", "description_length"),
            "$specification",
        )
        for field, maximum in (("code_length", 50), ("description_length", 150)):
            length = value[field]
            if (
                not isinstance(length, int)
                or isinstance(length, bool)
                or not 0 <= length <= maximum
            ):
                _fail(
                    "invalid_value",
                    f"$specification.{field}",
                    f"{field} должен быть целым числом от 0 до {maximum}.",
                )
    else:
        _required(
            value,
            ("periodicity", "dimensions", "resources"),
            "$specification",
        )
    try:
        identity = uuid.UUID(_text(value["identity"], "$specification.identity"))
    except ValueError:
        _fail("invalid_uuid", "$specification.identity", "identity должен быть UUID.")
    _text(value["synonym"], "$specification.synonym")
    if kind.ru == "РегистрСведений" and value.get("periodicity") != "nonperiodical":
        _fail("unsupported_periodicity", "$specification.periodicity", "Схема v1 поддерживает только nonperiodical.")
    names: set[str] = set()
    for collection, xml_kind in kind.fields:
        items = _list(value.get(collection, []), f"$specification.{collection}")
        for index, item in enumerate(items):
            field = _validate_field(item, f"$specification.{collection}[{index}]", xml_kind)
            name = str(field["name"])
            key = name.casefold()
            if key in names:
                _fail("duplicate_name", f"$specification.{collection}[{index}].name", f"Имя `{name}` уже использовано.")
            names.add(key)
    forms = _list(value["forms"], "$specification.forms")
    defaults = 0
    for index, item in enumerate(forms):
        form = _validate_form(
            item, f"$specification.forms[{index}]", format_version
        )
        name = str(form["name"])
        key = name.casefold()
        if key in names:
            _fail("duplicate_name", f"$specification.forms[{index}].name", f"Имя `{name}` уже использовано.")
        names.add(key)
        defaults += int(bool(form["default"]))
    if forms and defaults != 1:
        _fail("invalid_default_form", "$specification.forms", "При наличии форм ровно одна должна иметь default=true.")
    if kind.ru == "РегистрСведений" and not any(value.get(collection, []) for collection, _ in kind.fields):
        _fail("missing_register_field", "$specification", "Регистр должен содержать хотя бы одно поле.")
    return value, kind, match.group(2), identity


def _uuid(identity: uuid.UUID, label: str) -> str:
    return str(uuid.uuid5(identity, label))


def _synonym(text: object) -> str:
    return f"<Synonym><v8:item><v8:lang>ru</v8:lang><v8:content>{escape(str(text))}</v8:content></v8:item></Synonym>"


def _type_xml(value: dict[str, object]) -> str:
    kind = value["kind"]
    if kind == "string":
        return f"<Type><v8:Type>xs:string</v8:Type><v8:StringQualifiers><v8:Length>{value['length']}</v8:Length><v8:AllowedLength>{value.get('allowed_length', 'Variable')}</v8:AllowedLength></v8:StringQualifiers></Type>"
    if kind == "boolean":
        return "<Type><v8:Type>xs:boolean</v8:Type></Type>"
    if kind == "number":
        return f"<Type><v8:Type>xs:decimal</v8:Type><v8:NumberQualifiers><v8:Digits>{value['digits']}</v8:Digits><v8:FractionDigits>{value['fraction_digits']}</v8:FractionDigits><v8:AllowedSign>{value.get('allowed_sign', 'Any')}</v8:AllowedSign></v8:NumberQualifiers></Type>"
    if kind == "date":
        fractions = {"date": "Date", "time": "Time", "date_time": "DateTime"}[str(value["fractions"])]
        return f"<Type><v8:Type>xs:dateTime</v8:Type><v8:DateQualifiers><v8:DateFractions>{fractions}</v8:DateFractions></v8:DateQualifiers></Type>"
    name = str(value["object"]).split(".", 1)[1]
    return f"<Type><v8:Type>cfg:CatalogRef.{escape(name)}</v8:Type></Type>"


def _field_xml(field: dict[str, object], xml_kind: str, identity: uuid.UUID, collection: str) -> str:
    name = str(field["name"])
    main_filter = f"<MainFilter>{str(field.get('main_filter', False)).lower()}</MainFilter>" if xml_kind == "Dimension" else ""
    return (
        f'<{xml_kind} uuid="{_uuid(identity, f"field:{collection}:{name}")}"><Properties>'
        f"<Name>{escape(name)}</Name>{_synonym(field['synonym'])}<Comment/>"
        f"{_type_xml(field['type'])}{main_filter}</Properties></{xml_kind}>"
    )


def _generated_types(kind: _Kind, name: str, identity: uuid.UUID) -> str:
    return "".join(
        f'<xr:GeneratedType name="{kind.xml}{category}.{escape(name)}" category="{category}">'
        f"<xr:TypeId>{_uuid(identity, f'generated:{category}:type')}</xr:TypeId>"
        f"<xr:ValueId>{_uuid(identity, f'generated:{category}:value')}</xr:ValueId>"
        "</xr:GeneratedType>"
        for category in kind.generated_types
    )


def _catalog_properties(
    name: str,
    synonym: object,
    default: str,
    code_length: int,
    description_length: int,
) -> str:
    input_fields = ""
    if description_length > 0:
        input_fields += (
            f"<xr:Field>Catalog.{escape(name)}.StandardAttribute.Description</xr:Field>"
        )
    if code_length > 0:
        input_fields += (
            f"<xr:Field>Catalog.{escape(name)}.StandardAttribute.Code</xr:Field>"
        )
    default_presentation = "AsDescription" if description_length > 0 else "AsCode"
    return (
        f"<Name>{escape(name)}</Name>{_synonym(synonym)}<Comment/>"
        "<Hierarchical>false</Hierarchical><HierarchyType>HierarchyFoldersAndItems</HierarchyType>"
        "<LimitLevelCount>false</LimitLevelCount><LevelCount>2</LevelCount><FoldersOnTop>true</FoldersOnTop>"
        "<UseStandardCommands>true</UseStandardCommands><Owners/><SubordinationUse>ToItems</SubordinationUse>"
        f"<CodeLength>{code_length}</CodeLength><DescriptionLength>{description_length}</DescriptionLength><CodeType>String</CodeType>"
        "<CodeAllowedLength>Variable</CodeAllowedLength><CodeSeries>WholeCatalog</CodeSeries><CheckUnique>true</CheckUnique>"
        f"<Autonumbering>true</Autonumbering><DefaultPresentation>{default_presentation}</DefaultPresentation><Characteristics/>"
        "<PredefinedDataUpdate>Auto</PredefinedDataUpdate><EditType>InDialog</EditType><QuickChoice>false</QuickChoice>"
        f"<ChoiceMode>BothWays</ChoiceMode><InputByString>{input_fields}</InputByString>"
        "<SearchStringModeOnInputByString>Begin</SearchStringModeOnInputByString>"
        "<FullTextSearchOnInputByString>DontUse</FullTextSearchOnInputByString>"
        "<ChoiceDataGetModeOnInputByString>Directly</ChoiceDataGetModeOnInputByString>"
        f"<DefaultObjectForm>{escape(default)}</DefaultObjectForm>"
        "<DefaultFolderForm/><DefaultListForm/><DefaultChoiceForm/><DefaultFolderChoiceForm/>"
        "<AuxiliaryObjectForm/><AuxiliaryFolderForm/><AuxiliaryListForm/><AuxiliaryChoiceForm/><AuxiliaryFolderChoiceForm/>"
        "<IncludeHelpInContents>false</IncludeHelpInContents><BasedOn/><DataLockFields/><DataLockControlMode>Managed</DataLockControlMode>"
        "<FullTextSearch>Use</FullTextSearch><ObjectPresentation/><ExtendedObjectPresentation/><ListPresentation/>"
        "<ExtendedListPresentation/><Explanation/><CreateOnInput>Use</CreateOnInput><ChoiceHistoryOnInput>Auto</ChoiceHistoryOnInput>"
        "<DataHistory>DontUse</DataHistory><UpdateDataHistoryImmediatelyAfterWrite>false</UpdateDataHistoryImmediatelyAfterWrite>"
        "<ExecuteAfterWriteDataHistoryVersionProcessing>false</ExecuteAfterWriteDataHistoryVersionProcessing>"
    )


def _register_properties(name: str, synonym: object, default: str) -> str:
    return (
        f"<Name>{escape(name)}</Name>{_synonym(synonym)}<Comment/>"
        "<InformationRegisterPeriodicity>Nonperiodical</InformationRegisterPeriodicity><WriteMode>Independent</WriteMode>"
        f"<UseStandardCommands>true</UseStandardCommands><DefaultRecordForm>{escape(default)}</DefaultRecordForm>"
        "<FullTextSearch>DontUse</FullTextSearch><DataLockControlMode>Managed</DataLockControlMode>"
    )


def _form_descriptor(
    form: dict[str, object], identity: uuid.UUID, format_version: str
) -> str:
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        f"<MetaDataObject {_root_attributes(format_version)}><Form uuid=\"{_uuid(identity, f'form:{form["name"]}')}\"><Properties>"
        f"<Name>{escape(str(form['name']))}</Name>{_synonym(form['synonym'])}<Comment/><FormType>Managed</FormType>"
        "<IncludeHelpInContents>false</IncludeHelpInContents>"
        '<UsePurposes><v8:Value xsi:type="app:ApplicationUsePurpose">PlatformApplication</v8:Value></UsePurposes>'
        "</Properties></Form></MetaDataObject>"
    ).replace("\n", "\r\n")


def compile_metadata_object(specification: dict[str, object]) -> dict[str, object]:
    """Скомпилировать schema v1 в памяти, не читая и не записывая файлы."""

    value, kind, name, identity = _validate(specification)
    forms = value["forms"]
    default_form = next((str(form["name"]) for form in forms if form["default"]), "")
    default_value = f"{kind.xml}.{name}.Form.{default_form}" if default_form else ""
    properties = (
        _catalog_properties(
            name,
            value["synonym"],
            default_value,
            int(value["code_length"]),
            int(value["description_length"]),
        )
        if kind.ru == "Справочник"
        else _register_properties(name, value["synonym"], default_value)
    )
    children = "".join(f"<Form>{escape(str(form['name']))}</Form>" for form in forms)
    for collection, xml_kind in kind.fields:
        children += "".join(
            _field_xml(field, xml_kind, identity, collection)
            for field in value.get(collection, [])
        )
    descriptor = (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        f"<MetaDataObject {_root_attributes(str(value['format_version']))}><{kind.xml} uuid=\"{identity}\">"
        f"<InternalInfo>{_generated_types(kind, name, identity)}</InternalInfo>"
        f"<Properties>{properties}</Properties><ChildObjects>{children}</ChildObjects>"
        f"</{kind.xml}></MetaDataObject>"
    ).replace("\n", "\r\n")
    owner = f"{kind.directory}/{name}"
    artifacts: list[dict[str, str]] = [{"path": f"{owner}.xml", "content": descriptor}]
    for form in forms:
        form_name = str(form["name"])
        base = f"{owner}/Forms/{form_name}"
        artifacts.extend(
            [
                {
                    "path": f"{base}.xml",
                    "content": _form_descriptor(
                        form, identity, str(value["format_version"])
                    ),
                },
                {"path": f"{base}/Ext/Form.xml", "content": str(form["form_xml"])},
                {"path": f"{base}/Ext/Form/Module.bsl", "content": str(form["module_bsl"])},
            ]
        )
    if len(artifacts) > MAX_ARTIFACTS:
        _fail(
            "too_many_artifacts",
            "$specification.forms",
            f"Результат содержит более {MAX_ARTIFACTS} артефактов.",
        )
    total_size = sum(
        _artifact_size(item["content"], item["path"]) for item in artifacts
    )
    if total_size > MAX_TOTAL_BYTES:
        _fail(
            "artifacts_too_large",
            "$specification",
            f"Суммарный размер артефактов превышает {MAX_TOTAL_BYTES} байт.",
        )
    diagnostics: list[dict[str, str]] = []
    if kind.ru == "Справочник":
        required_paths = []
        if int(value["description_length"]) > 0:
            required_paths.append("Объект.Наименование")
        if int(value["code_length"]) > 0:
            required_paths.append("Объект.Код")
        default_form_spec = next(
            (form for form in forms if bool(form["default"])),
            None,
        )
        if default_form_spec is not None:
            root = ET.fromstring(str(default_form_spec["form_xml"]))
            actual_paths = {
                node.text
                for node in root.iter(f"{{{LOGFORM}}}DataPath")
                if node.text
            }
            missing_diagnostics = []
            for required_path in required_paths:
                if required_path not in actual_paths:
                    missing_diagnostics.append(
                        {
                            "status": "failed",
                            "code": "required_standard_field_missing",
                            "path": "$specification.forms",
                            "message": (
                                f"Основная форма справочника обязана выводить "
                                f"`{required_path}`, потому что соответствующая "
                                "длина стандартного реквизита больше нуля."
                            ),
                        }
                    )
            if missing_diagnostics:
                raise MetadataAuthoringContractError(missing_diagnostics)
    return {
        "status": "compiled",
        "schema_version": 1,
        "object_ref": str(value["object_ref"]),
        "format_version": str(value["format_version"]),
        "artifacts": artifacts,
        "diagnostics": diagnostics,
    }


__all__ = ["MetadataAuthoringContractError", "compile_metadata_object"]
