"""Статическая проверка owner-relative артефактов метаданных 1С."""

from __future__ import annotations

from dataclasses import dataclass
from io import StringIO
import re
import uuid
import xml.etree.ElementTree as ET


MAX_ARTIFACTS = 32
MAX_ARTIFACT_BYTES = 2 * 1024 * 1024
MAX_TOTAL_BYTES = 8 * 1024 * 1024
MD_NAMESPACE = "http://v8.1c.ru/8.3/MDClasses"
LOGFORM_NAMESPACE = "http://v8.1c.ru/8.3/xcf/logform"
DCS_SCHEMA_NAMESPACE = "http://v8.1c.ru/8.1/data-composition-system/schema"
_FORBIDDEN_XML_DECLARATION = re.compile(r"<!\s*(?:DOCTYPE|ENTITY)\b", re.I)
_QNAME = re.compile(r"^([A-Za-z_][A-Za-z0-9_.-]*):[^\s:]+$")
_OBJECT_REF = re.compile(
    r"^(Справочник|РегистрСведений|Документ|Обработка|Отчет)\.([A-Za-zА-Яа-яЁё_][A-Za-zА-Яа-яЁё0-9_]*)$"
)
_FORMAT_VERSION = re.compile(r"^\d+\.\d+(?:\.\d+)*$")
_METADATA_NAME = re.compile(r"^[A-Za-zА-Яа-яЁё_][A-Za-zА-Яа-яЁё0-9_]*$")
_COVERAGE_KEYS = (
    "xml",
    "descriptor",
    "generated_types",
    "forms",
    "namespaces",
)


@dataclass(frozen=True, slots=True)
class _Kind:
    object_kind: str
    xml_kind: str
    directory: str
    generated_types: tuple[str, ...]
    default_form_properties: tuple[str, ...]


_KINDS = {
    "Справочник": _Kind(
        "Справочник",
        "Catalog",
        "Catalogs",
        ("Object", "Ref", "Selection", "List", "Manager"),
        ("DefaultObjectForm", "DefaultListForm", "DefaultChoiceForm"),
    ),
    "РегистрСведений": _Kind(
        "РегистрСведений",
        "InformationRegister",
        "InformationRegisters",
        (
            "Record",
            "Manager",
            "Selection",
            "List",
            "RecordSet",
            "RecordKey",
            "RecordManager",
        ),
        ("DefaultRecordForm", "DefaultListForm"),
    ),
    "Документ": _Kind(
        "Документ",
        "Document",
        "Documents",
        ("Object", "Ref", "Selection", "List", "Manager"),
        ("DefaultObjectForm", "DefaultListForm", "DefaultChoiceForm"),
    ),
    "Обработка": _Kind(
        "Обработка",
        "DataProcessor",
        "DataProcessors",
        ("Object", "Manager"),
        ("DefaultForm",),
    ),
    "Отчет": _Kind(
        "Отчет",
        "Report",
        "Reports",
        ("Object", "Manager"),
        ("DefaultForm",),
    ),
}


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _scaffold_only_module(module_bsl: str) -> bool:
    for line in module_bsl.lstrip("\ufeff").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("//"):
            continue
        if stripped.casefold().startswith(("#область", "#конецобласти")):
            continue
        return False
    return True


def _diagnostic(code: str, path: str, message: str) -> dict[str, str]:
    return {"status": "failed", "code": code, "path": path, "message": message}


class _Report:
    def __init__(self, object_ref: object) -> None:
        self.object_ref = object_ref
        self.diagnostics: list[dict[str, str]] = []
        self.failed_coverage: set[str] = set()

    def fail(self, coverage: str, code: str, path: str, message: str) -> None:
        self.failed_coverage.add(coverage)
        self.diagnostics.append(_diagnostic(code, path, message))

    def fail_all(self, code: str, path: str, message: str) -> None:
        self.failed_coverage.update(_COVERAGE_KEYS)
        self.diagnostics.append(_diagnostic(code, path, message))

    def result(self) -> dict[str, object]:
        failed = bool(self.diagnostics)
        return {
            "status": "failed" if failed else "passed",
            "object_ref": self.object_ref,
            "coverage": {
                key: "failed" if key in self.failed_coverage else "passed"
                for key in _COVERAGE_KEYS
            },
            "diagnostics": self.diagnostics,
            "instructions": (
                ["Исправьте diagnostics и повторите check_metadata_artifacts."]
                if failed
                else [
                    "Статическая проверка пройдена; нативный импорт в 1С не проверен.",
                    "Semantic role Form.xml здесь не проверяется: используйте результат check_managed_form.",
                ]
            ),
        }


def _parse_xml(
    path: str,
    text: str,
    report: _Report,
) -> tuple[ET.Element | None, set[str]]:
    if _FORBIDDEN_XML_DECLARATION.search(text):
        report.fail(
            "xml",
            "forbidden_xml_declaration",
            path,
            "DTD и ENTITY в XML-артефактах запрещены.",
        )
        return None, set()
    namespaces: set[str] = set()
    try:
        for _, declaration in ET.iterparse(StringIO(text), events=("start-ns",)):
            namespaces.add(declaration[0] or "")
        root = ET.fromstring(text)
    except (ET.ParseError, ValueError) as error:
        report.fail("xml", "invalid_xml", path, f"XML не разобран: {error}.")
        return None, namespaces
    return root, namespaces


def _find_child(parent: ET.Element | None, name: str) -> ET.Element | None:
    if parent is None:
        return None
    return next((item for item in parent if _local(item.tag) == name), None)


def _child_text(parent: ET.Element | None, name: str) -> str | None:
    child = _find_child(parent, name)
    if child is None or child.text is None:
        return None
    value = child.text.strip()
    return value or None


def _normalize_artifacts(
    artifacts: dict[str, str] | list[dict[str, str]], report: _Report
) -> dict[str, str] | None:
    """Принять как каноническую карту, так и прямой результат compiler."""

    if isinstance(artifacts, dict):
        if any(
            not isinstance(path, str) or not isinstance(text, str)
            for path, text in artifacts.items()
        ):
            report.fail_all(
                "invalid_artifacts",
                "$artifacts",
                "artifacts должен содержать только пары path → text.",
            )
            return None
        return dict(artifacts)
    if not isinstance(artifacts, list):
        report.fail_all(
            "invalid_artifacts",
            "$artifacts",
            "artifacts должен быть словарём path → text или массивом {path, content}.",
        )
        return None
    normalized: dict[str, str] = {}
    for index, item in enumerate(artifacts):
        if not isinstance(item, dict):
            report.fail_all(
                "invalid_compiler_artifact",
                f"$artifacts[{index}]",
                "Элемент compiler artifacts должен быть объектом {path, content}.",
            )
            return None
        path = item.get("path")
        content = item.get("content")
        if not isinstance(path, str) or not isinstance(content, str):
            report.fail_all(
                "invalid_compiler_artifact",
                f"$artifacts[{index}]",
                "Compiler artifact должен иметь строковые path и content.",
            )
            return None
        if path in normalized:
            report.fail_all(
                "duplicate_artifact_path",
                f"$artifacts[{index}].path",
                f"Путь `{path}` повторяется в compiler artifacts.",
            )
            return None
        normalized[path] = content
    return normalized


def _require_metadata_root(
    root: ET.Element | None,
    path: str,
    report: _Report,
    coverage: str,
) -> bool:
    if root is None:
        report.failed_coverage.add(coverage)
        return False
    if root.tag != f"{{{MD_NAMESPACE}}}MetaDataObject":
        report.fail(
            coverage,
            "invalid_metadata_root",
            path,
            "Корень descriptor должен быть MDClasses MetaDataObject.",
        )
        return False
    return True


def _check_qnames(
    parsed: dict[str, tuple[ET.Element, set[str]]], report: _Report
) -> None:
    for path, (root, namespaces) in parsed.items():
        for element in root.iter():
            if _local(element.tag) != "Type" or not element.text:
                continue
            value = element.text.strip()
            match = _QNAME.fullmatch(value)
            if match and match.group(1) not in namespaces:
                report.fail(
                    "namespaces",
                    "undeclared_qname_prefix",
                    path,
                    f"Префикс `{match.group(1)}` в QName `{value}` не объявлен в этом XML.",
                )
            if match and match.group(1) == "cfg":
                type_kind = value.split(":", 1)[1].split(".", 1)[0]
                if type_kind in {"Catalogs", "Documents", "InformationRegisters"}:
                    report.fail(
                        "namespaces",
                        "unsupported_metadata_type",
                        path,
                        (
                            f"QName `{value}` использует физическое имя каталога. "
                            "Для ссылки нужен `cfg:CatalogRef.<ИмяСправочника>` "
                            "или `cfg:DocumentRef.<ИмяДокумента>`."
                        ),
                    )


def _check_unique_ids(
    parsed: dict[str, tuple[ET.Element, set[str]]], report: _Report
) -> None:
    seen: dict[uuid.UUID, str] = {}
    for path, (root, _) in parsed.items():
        candidates: list[tuple[str, str, str]] = []
        for element in root.iter():
            for attribute, value in element.attrib.items():
                if _local(attribute).casefold() == "uuid":
                    candidates.append((f"{path}@uuid", value.strip(), "xml"))
            if _local(element.tag) in {"TypeId", "ValueId"} and element.text:
                candidates.append(
                    (
                        f"{path}:{_local(element.tag)}",
                        element.text.strip(),
                        "generated_types",
                    )
                )
        for location, value, coverage in candidates:
            try:
                parsed_id = uuid.UUID(value)
            except (ValueError, AttributeError):
                report.fail(
                    coverage,
                    "invalid_uuid",
                    location,
                    f"`{value}` не является UUID.",
                )
                continue
            if parsed_id in seen:
                report.fail(
                    coverage,
                    "duplicate_uuid",
                    location,
                    f"UUID уже использован в `{seen[parsed_id]}`.",
                )
            else:
                seen[parsed_id] = location


def _check_generated_types(
    descriptor: ET.Element,
    descriptor_path: str,
    kind: _Kind,
    object_name: str,
    report: _Report,
) -> None:
    found: dict[str, int] = {}
    for element in descriptor.iter():
        if _local(element.tag) != "GeneratedType":
            continue
        category = element.attrib.get("category", "").strip()
        found[category] = found.get(category, 0) + 1
        if not element.attrib.get("name", "").strip():
            report.fail(
                "generated_types",
                "missing_generated_type_name",
                descriptor_path,
                f"GeneratedType `{category}` не имеет name.",
            )
        elif category in kind.generated_types:
            expected_name = f"{kind.xml_kind}{category}.{object_name}"
            if element.attrib["name"].strip() != expected_name:
                report.fail(
                    "generated_types",
                    "generated_type_name_mismatch",
                    descriptor_path,
                    (
                        f"GeneratedType `{category}` должен иметь name "
                        f"`{expected_name}`."
                    ),
                )
        for identifier in ("TypeId", "ValueId"):
            if _child_text(element, identifier) is None:
                report.fail(
                    "generated_types",
                    "missing_generated_type_id",
                    descriptor_path,
                    f"GeneratedType `{category}` не имеет {identifier}.",
                )
    for category in kind.generated_types:
        if found.get(category, 0) == 0:
            report.fail(
                "generated_types",
                "missing_generated_type",
                descriptor_path,
                f"Отсутствует GeneratedType категории `{category}`.",
            )
        elif found[category] > 1:
            report.fail(
                "generated_types",
                "duplicate_generated_type",
                descriptor_path,
                f"GeneratedType категории `{category}` объявлен более одного раза.",
            )
    for category in sorted(set(found) - set(kind.generated_types)):
        report.fail(
            "generated_types",
            "unexpected_generated_type",
            descriptor_path,
            f"GeneratedType категории `{category}` не входит в профиль {kind.object_kind}.",
        )


def _check_format_version(
    root: ET.Element,
    path: str,
    report: _Report,
    coverage: str,
    expected: str,
) -> None:
    actual = root.attrib.get("version", "").strip()
    if not actual:
        report.fail(
            coverage,
            "missing_format_version",
            path,
            "Корень XML descriptor обязан содержать атрибут version.",
        )
    elif actual != expected:
        report.fail(
            coverage,
            "format_version_mismatch",
            path,
            (
                f"Версия `{actual}` не совпадает с переданным "
                f"format_version `{expected}`."
            ),
        )


def _infer_supported_form_role(
    form_root: ET.Element,
    object_kind: str,
    object_name: str,
) -> str | None:
    """Вывести роль только из подтверждённого закрытого XML-профиля Forms."""

    main_attributes = [
        attribute
        for attribute in form_root.iter()
        if _local(attribute.tag) == "Attribute"
        and _child_text(attribute, "MainAttribute") == "true"
    ]
    if len(main_attributes) != 1:
        return None
    main = main_attributes[0]
    main_name = main.get("name", "")
    main_types = [
        node.text.strip()
        for node in main.iter()
        if _local(node.tag) == "Type" and node.text and node.text.strip()
    ]
    if object_kind == "Справочник" and (
        main_name == "Объект"
        and main_types == [f"cfg:CatalogObject.{object_name}"]
    ):
        return "object"
    if object_kind == "Документ" and (
        main_name == "Объект"
        and main_types == [f"cfg:DocumentObject.{object_name}"]
    ):
        return "object"
    if object_kind == "РегистрСведений":
        if (
            main_name == "Запись"
            and main_types
            == [f"cfg:InformationRegisterRecordManager.{object_name}"]
            and _child_text(main, "SavedData") == "true"
        ):
            return "record"
        if (
            main_name == object_name
            and main_types == [f"cfg:InformationRegisterRecordSet.{object_name}"]
            and _child_text(main, "SavedData") == "true"
        ):
            return "record_set"
    if (
        object_kind in {"Справочник", "Документ", "РегистрСведений"}
        and main_name == "Список"
        and main_types == ["cfg:DynamicList"]
    ):
        bound_tables = [
            table
            for table in form_root.iter()
            if _local(table.tag) == "Table"
            and table.get("name") == "Список"
            and _child_text(table, "DataPath") == "Список"
        ]
        if len(bound_tables) != 1:
            return None
        choice_mode = _find_child(bound_tables[0], "ChoiceMode")
        opening_mode = _find_child(form_root, "WindowOpeningMode")
        if choice_mode is None and opening_mode is None:
            return "list"
        if (
            object_kind in {"Справочник", "Документ"}
            and _child_text(bound_tables[0], "ChoiceMode") == "true"
            and _child_text(form_root, "WindowOpeningMode") == "LockOwnerWindow"
        ):
            return "choice"
    return None


def _check_forms(
    owner_path: str,
    metadata_object: ET.Element,
    artifacts: dict[str, str],
    parsed: dict[str, tuple[ET.Element, set[str]]],
    format_version: str,
    object_name: str,
    kind: _Kind,
    report: _Report,
) -> None:
    properties = _find_child(metadata_object, "Properties")
    if (
        kind.object_kind == "РегистрСведений"
        and _find_child(properties, "DefaultRecordSetForm") is not None
    ):
        report.fail(
            "forms",
            "unsupported_default_form_property",
            f"{owner_path}.xml:DefaultRecordSetForm",
            "DefaultRecordSetForm не входит в подтверждённый контракт регистра сведений.",
        )
    child_objects = _find_child(metadata_object, "ChildObjects")
    form_names = [
        (child.text or "").strip()
        for child in (child_objects if child_objects is not None else ())
        if _local(child.tag) == "Form" and (child.text or "").strip()
    ]
    template_names = [
        (child.text or "").strip()
        for child in (child_objects if child_objects is not None else ())
        if _local(child.tag) == "Template" and (child.text or "").strip()
    ]
    if kind.object_kind == "Обработка" and not form_names:
        report.fail(
            "forms",
            "missing_data_processor_form",
            f"{owner_path}.xml:ChildObjects",
            "Базовая встроенная обработка требует одну объявленную форму.",
        )
    elif kind.object_kind == "Обработка" and len(form_names) != 1:
        report.fail(
            "forms",
            "invalid_data_processor_form_count",
            f"{owner_path}.xml:ChildObjects",
            "Базовая встроенная обработка поддерживает ровно одну объявленную форму.",
        )
    if kind.object_kind == "Отчет" and len(form_names) != 1:
        report.fail(
            "forms",
            "invalid_report_form_count",
            f"{owner_path}.xml:ChildObjects",
            "Базовый отчет требует ровно одну объявленную форму.",
        )
    if kind.object_kind == "Отчет" and child_objects is not None:
        expected_template = "ОсновнаяСхемаКомпоновкиДанных"
        unsupported_children = [
            _local(child.tag)
            for child in child_objects
            if _local(child.tag) not in {"Form", "Template"}
        ]
        if unsupported_children or template_names != [expected_template]:
            report.fail(
                "forms",
                "invalid_report_dcs_child",
                f"{owner_path}.xml:ChildObjects",
                "Отчет требует ровно один DCS-шаблон `ОсновнаяСхемаКомпоновкиДанных`.",
            )
    if len(set(form_names)) != len(form_names):
        report.fail(
            "forms",
            "duplicate_form",
            f"{owner_path}.xml",
            "Имя формы нельзя повторять в ChildObjects/Form.",
        )
    valid_form_names: list[str] = []
    form_main_types: dict[str, list[str]] = {}
    form_main_names: dict[str, list[str]] = {}
    inferred_form_roles: dict[str, str] = {}
    expected_paths = {f"{owner_path}.xml"}
    for form_name in form_names:
        if _METADATA_NAME.fullmatch(form_name) is None:
            report.fail(
                "forms",
                "invalid_form_name",
                f"{owner_path}.xml:ChildObjects/Form",
                "Имя формы должно быть идентификатором метаданных без сегментов пути.",
            )
            continue
        valid_form_names.append(form_name)
        descriptor_path = f"{owner_path}/Forms/{form_name}.xml"
        form_xml_path = f"{owner_path}/Forms/{form_name}/Ext/Form.xml"
        module_path = f"{owner_path}/Forms/{form_name}/Ext/Form/Module.bsl"
        expected_paths.update({descriptor_path, form_xml_path})
        if kind.object_kind != "Отчет" and module_path in artifacts:
            expected_paths.add(module_path)
        for path, code, message in (
            (
                descriptor_path,
                "missing_form_descriptor",
                f"Отсутствует descriptor формы `{form_name}`.",
            ),
            (form_xml_path, "missing_form_xml", f"Отсутствует Form.xml формы `{form_name}`."),
        ):
            if path not in artifacts:
                report.fail("forms", code, path, message)
        internal_form_entry = parsed.get(form_xml_path)
        if internal_form_entry is not None:
            inferred_role = _infer_supported_form_role(
                internal_form_entry[0], kind.object_kind, object_name
            )
            if inferred_role is not None:
                inferred_form_roles[form_name] = inferred_role
            _check_format_version(
                internal_form_entry[0], form_xml_path, report, "forms", format_version
            )
            main_types: list[str] = []
            main_names: list[str] = []
            for attribute in internal_form_entry[0].iter():
                if _local(attribute.tag) != "Attribute":
                    continue
                if _child_text(attribute, "MainAttribute") != "true":
                    continue
                main_names.append(attribute.get("name", ""))
                main_types.extend(
                    node.text.strip()
                    for node in attribute.iter()
                    if _local(node.tag) == "Type"
                    and node.text
                    and node.text.strip()
                )
            form_main_types[form_name] = main_types
            form_main_names[form_name] = main_names
            if kind.object_kind in {"Документ", "Обработка", "Отчет"}:
                object_type = {
                    "Документ": "DocumentObject",
                    "Обработка": "DataProcessorObject",
                    "Отчет": "ReportObject",
                }[kind.object_kind]
                expected_type = f"cfg:{object_type}.{object_name}"
                foreign_object_type = next(
                    (
                        type_name
                        for type_name in main_types
                        if type_name.startswith(f"cfg:{object_type}.")
                        and type_name != expected_type
                    ),
                    None,
                )
                if foreign_object_type is not None:
                    report.fail(
                        "forms",
                        "form_owner_mismatch",
                        form_xml_path,
                        (
                            f"Главный реквизит формы имеет тип `{foreign_object_type}`, "
                            f"ожидается владелец `{expected_type}`."
                        ),
                    )
            if kind.object_kind == "Обработка":
                if main_names != ["Объект"]:
                    report.fail(
                        "forms",
                        "invalid_data_processor_main_attribute",
                        form_xml_path,
                        "Основная форма обработки требует единственный главный реквизит `Объект`.",
                    )
                structural_behavior = {
                    _local(node.tag) for node in internal_form_entry[0].iter()
                } & {
                    "Button",
                    "ButtonGroup",
                    "Command",
                    "CommandBar",
                    "CommandName",
                    "CommandSource",
                    "Event",
                    "Popup",
                }
                if any(
                    _local(node.tag) in {"AutoCommandBar", "ContextMenu"}
                    and len(node) > 0
                    for node in internal_form_entry[0].iter()
                ):
                    structural_behavior.add("CommandContainer")
                if structural_behavior:
                    report.fail(
                        "forms",
                        "unsupported_data_processor_form_behavior",
                        form_xml_path,
                        "Команды и события формы обработки не входят в базовый контракт.",
                    )
                module_bsl = artifacts.get(module_path)
                if module_bsl is not None and not _scaffold_only_module(module_bsl):
                    report.fail(
                        "forms",
                        "unsupported_data_processor_module_bsl",
                        module_path,
                        "Прикладной BSL формы обработки не входит в базовый контракт.",
                    )
            if kind.object_kind == "Отчет":
                if main_names != ["Отчет"]:
                    report.fail(
                        "forms",
                        "invalid_report_main_attribute",
                        form_xml_path,
                        "Основная форма отчета требует единственный главный реквизит `Отчет`.",
                    )
                expected_report_type = f"cfg:ReportObject.{object_name}"
                if main_types != [expected_report_type]:
                    report.fail(
                        "forms",
                        "form_owner_mismatch",
                        form_xml_path,
                        "Главный реквизит Отчет должен иметь ровно один тип "
                        f"`{expected_report_type}`.",
                    )
                structural_behavior = {
                    _local(node.tag) for node in internal_form_entry[0].iter()
                } & {
                    "Button", "ButtonGroup", "Command", "CommandBar",
                    "CommandName", "CommandSource", "Event", "Popup", "SavedData",
                }
                if any(
                    _local(node.tag) in {"AutoCommandBar", "ContextMenu"}
                    and len(node) > 0
                    for node in internal_form_entry[0].iter()
                ):
                    structural_behavior.add("CommandContainer")
                if structural_behavior:
                    report.fail(
                        "forms",
                        "unsupported_report_form_behavior",
                        form_xml_path,
                        "Команды, события и SavedData формы отчета не поддерживаются.",
                    )
                module_bsl = artifacts.get(module_path)
                if module_bsl is not None and not _scaffold_only_module(module_bsl):
                    report.fail(
                        "forms",
                        "unsupported_report_module_bsl",
                        module_path,
                        "Прикладной BSL формы отчета не входит в базовый контракт.",
                    )
        form_entry = parsed.get(descriptor_path)
        if form_entry is None:
            if descriptor_path in artifacts:
                report.failed_coverage.add("forms")
            continue
        form_root = form_entry[0]
        _check_format_version(
            form_root, descriptor_path, report, "forms", format_version
        )
        if not _require_metadata_root(
            form_root, descriptor_path, report, "forms"
        ):
            continue
        forms = [child for child in form_root if _local(child.tag) == "Form"]
        if len(forms) != 1:
            report.fail(
                "forms",
                "invalid_form_descriptor",
                descriptor_path,
                "Descriptor формы должен содержать ровно один Form.",
            )
            continue
        form = forms[0]
        if not form.attrib.get("uuid", "").strip():
            report.fail(
                "forms", "missing_form_uuid", descriptor_path, "Form не имеет uuid."
            )
        form_properties = _find_child(form, "Properties")
        if _child_text(form_properties, "Name") != form_name:
            report.fail(
                "forms",
                "form_name_mismatch",
                descriptor_path,
                f"Properties/Name формы должен быть `{form_name}`.",
            )
        if _child_text(form_properties, "FormType") != "Managed":
            report.fail(
                "forms",
                "unsupported_form_type",
                descriptor_path,
                "Поддерживается только FormType `Managed`.",
            )
    if kind.object_kind == "Отчет":
        template_name = "ОсновнаяСхемаКомпоновкиДанных"
        template_descriptor_path = (
            f"{owner_path}/Templates/{template_name}.xml"
        )
        template_xml_path = (
            f"{owner_path}/Templates/{template_name}/Ext/Template.xml"
        )
        expected_paths.update({template_descriptor_path, template_xml_path})
        if template_descriptor_path not in artifacts:
            report.fail(
                "forms", "missing_report_dcs_descriptor",
                template_descriptor_path,
                "Отсутствует descriptor основной СКД.",
            )
        if template_xml_path not in artifacts:
            report.fail(
                "forms", "missing_report_dcs_xml", template_xml_path,
                "Отсутствует Template.xml основной СКД.",
            )
        template_entry = parsed.get(template_descriptor_path)
        if template_entry is not None:
            template_root = template_entry[0]
            _check_format_version(
                template_root, template_descriptor_path, report, "forms",
                format_version,
            )
            if _require_metadata_root(
                template_root, template_descriptor_path, report, "forms"
            ):
                templates = [
                    child for child in template_root
                    if _local(child.tag) == "Template"
                ]
                if len(templates) != 1:
                    report.fail(
                        "forms", "invalid_report_dcs_descriptor",
                        template_descriptor_path,
                        "Descriptor СКД должен содержать ровно один Template.",
                    )
                else:
                    template = templates[0]
                    template_properties = _find_child(template, "Properties")
                    if (
                        not template.attrib.get("uuid", "").strip()
                        or _child_text(template_properties, "Name") != template_name
                        or _child_text(template_properties, "TemplateType")
                        != "DataCompositionSchema"
                    ):
                        report.fail(
                            "forms", "invalid_report_dcs_descriptor",
                            template_descriptor_path,
                            "Template должен описывать DataCompositionSchema с каноническим именем.",
                        )
        dcs_entry = parsed.get(template_xml_path)
        if dcs_entry is not None:
            dcs_root = dcs_entry[0]
            variants = [
                child for child in dcs_root
                if _local(child.tag) == "settingsVariant"
            ]
            if len(dcs_root) != 1 or len(variants) != 1:
                report.fail(
                    "forms", "invalid_report_dcs_default_variant",
                    template_xml_path,
                    "Минимальная СКД требует ровно один вариант `Основной`.",
                )
            else:
                variant = variants[0]
                settings_nodes = [
                    child for child in variant
                    if _local(child.tag) == "settings"
                ]
                if (
                    [_local(child.tag) for child in variant]
                    != ["name", "presentation", "settings"]
                    or _child_text(variant, "name") != "Основной"
                    or _child_text(variant, "presentation") != "Основной"
                    or len(settings_nodes) != 1
                    or len(settings_nodes[0]) != 0
                    or (settings_nodes[0].text or "").strip()
                    or settings_nodes[0].attrib
                ):
                    report.fail(
                        "forms", "invalid_report_dcs_default_variant",
                        template_xml_path,
                        "Вариант СКД должен иметь имя и представление `Основной`, а также settings.",
                    )
    for path in sorted(set(artifacts) - expected_paths):
        report.fail(
            "forms",
            "unexpected_artifact",
            path,
            "Артефакт не входит в descriptor и закрытый owner-relative комплект.",
        )
    default_forms = {
        property_name: _child_text(properties, property_name)
        for property_name in kind.default_form_properties
    }
    default_property_by_role = {
        "Справочник": {
            "object": "DefaultObjectForm",
            "list": "DefaultListForm",
            "choice": "DefaultChoiceForm",
        },
        "Документ": {
            "object": "DefaultObjectForm",
            "list": "DefaultListForm",
            "choice": "DefaultChoiceForm",
        },
        "РегистрСведений": {
            "record": "DefaultRecordForm",
            "list": "DefaultListForm",
        },
    }.get(kind.object_kind, {})
    for role in sorted(set(inferred_form_roles.values())):
        property_name = default_property_by_role.get(role)
        if property_name is None or default_forms.get(property_name) is not None:
            continue
        report.fail(
            "forms",
            "missing_default_form_role",
            f"{owner_path}.xml:{property_name}",
            (
                f"Для представленной роли `{role}` требуется непустой "
                f"{property_name}."
            ),
        )
    if kind.object_kind == "Обработка":
        default_form_nodes = [
            child
            for child in (properties if properties is not None else ())
            if _local(child.tag) == "DefaultForm"
        ]
        if len(default_form_nodes) != 1:
            report.fail(
                "forms",
                "invalid_default_data_processor_form_count",
                f"{owner_path}.xml:DefaultForm",
                "Descriptor обработки должен содержать ровно один DefaultForm.",
            )
        for unsupported_property in (
            "DefaultObjectForm",
            "DefaultListForm",
            "DefaultChoiceForm",
            "DefaultRecordForm",
            "DefaultRecordSetForm",
        ):
            if _find_child(properties, unsupported_property) is not None:
                report.fail(
                    "forms",
                    "unsupported_default_form_property",
                    f"{owner_path}.xml:{unsupported_property}",
                    "Встроенная обработка поддерживает только DefaultForm.",
                )
        if _child_text(properties, "AuxiliaryForm") is not None:
            report.fail(
                "forms",
                "unsupported_auxiliary_form",
                f"{owner_path}.xml:AuxiliaryForm",
                "Дополнительная форма обработки не входит в базовый контракт.",
            )
        if not _child_text(properties, "DefaultForm"):
            report.fail(
                "forms",
                "missing_default_data_processor_form",
                f"{owner_path}.xml:DefaultForm",
                "Единственная основная форма обработки должна быть DefaultForm.",
            )
    if kind.object_kind == "Отчет":
        default_form_nodes = [
            child for child in (properties if properties is not None else ())
            if _local(child.tag) == "DefaultForm"
        ]
        if len(default_form_nodes) != 1:
            report.fail(
                "forms", "invalid_default_report_form_count",
                f"{owner_path}.xml:DefaultForm",
                "Descriptor отчета должен содержать ровно один DefaultForm.",
            )
        for unsupported_property in (
            "DefaultObjectForm", "DefaultListForm", "DefaultChoiceForm",
            "DefaultRecordForm", "DefaultRecordSetForm",
        ):
            if _find_child(properties, unsupported_property) is not None:
                report.fail(
                    "forms", "unsupported_default_form_property",
                    f"{owner_path}.xml:{unsupported_property}",
                    "Отчет поддерживает только DefaultForm.",
                )
        auxiliary = _find_child(properties, "AuxiliaryForm")
        if auxiliary is not None and (
            (auxiliary.text or "").strip() or len(auxiliary) or auxiliary.attrib
        ):
            report.fail(
                "forms", "unsupported_auxiliary_form",
                f"{owner_path}.xml:AuxiliaryForm",
                "Дополнительные формы отчета не входят в базовый контракт.",
            )
        expected_dcs = (
            f"Report.{object_name}.Template."
            "ОсновнаяСхемаКомпоновкиДанных"
        )
        if _child_text(properties, "MainDataCompositionSchema") != expected_dcs:
            report.fail(
                "forms", "invalid_report_data_composition_schema",
                f"{owner_path}.xml:MainDataCompositionSchema",
                "Отчет должен ссылаться на owner-relative основную СКД.",
            )
        for property_name in (
            "DefaultSettingsForm", "AuxiliarySettingsForm", "DefaultVariantForm",
            "VariantsStorage", "SettingsStorage",
        ):
            node = _find_child(properties, property_name)
            if node is not None and (
                (node.text or "").strip() or len(node) or node.attrib
            ):
                report.fail(
                    "forms", "unsupported_report_settings",
                    f"{owner_path}.xml:{property_name}",
                    "Настройки и варианты отчета не входят в базовый контракт.",
                )
        if not _child_text(properties, "DefaultForm"):
            report.fail(
                "forms", "missing_default_report_form",
                f"{owner_path}.xml:DefaultForm",
                "Единственная основная форма отчета должна быть DefaultForm.",
            )
        if _child_text(properties, "UseStandardCommands") != "true":
            report.fail(
                "forms", "invalid_report_standard_commands",
                f"{owner_path}.xml:UseStandardCommands",
                "Дефолтная форма отчета требует UseStandardCommands=true.",
            )
    allowed = {
        f"{kind.xml_kind}.{object_name}.Form.{form_name}"
        for form_name in valid_form_names
    }
    field_names = [
        _child_text(_find_child(child, "Properties"), "Name")
        for child in (child_objects if child_objects is not None else ())
        if _local(child.tag) in {"Attribute", "Dimension", "Resource"}
    ]
    field_names = [name for name in field_names if name is not None]
    for property_name, default_form in default_forms.items():
        if default_form is None:
            continue
        if default_form not in allowed:
            report.fail(
                "forms",
                "unknown_default_form",
                f"{owner_path}.xml:{property_name}",
                (
                    f"Форма по умолчанию `{default_form}` должна иметь вид "
                    f"`{kind.xml_kind}.{object_name}.Form.<Форма>`, где "
                    "<Форма> — короткое значение ChildObjects/Form."
                ),
            )
        else:
            default_form_name = default_form.rsplit(".", 1)[-1]
            expected_role = {
                "DefaultObjectForm": "object",
                "DefaultListForm": "list",
                "DefaultChoiceForm": "choice",
                "DefaultRecordForm": "record",
            }.get(property_name)
            inferred_role = inferred_form_roles.get(default_form_name)
            if inferred_role is not None and inferred_role != expected_role:
                report.fail(
                    "forms",
                    "default_form_role_mismatch",
                    f"{owner_path}.xml:{property_name}",
                    (
                        f"{property_name} ссылается на форму роли "
                        f"`{inferred_role}`, ожидалась роль `{expected_role}`."
                    ),
                )
            form_xml_path = f"{owner_path}/Forms/{default_form_name}/Ext/Form.xml"
            form_entry = parsed.get(form_xml_path)
            required_paths: list[str] = []
            if kind.object_kind == "Справочник":
                prefix = "Объект" if property_name == "DefaultObjectForm" else "Список"
                for length_property, standard_name in (
                    ("DescriptionLength", "Description"),
                    ("CodeLength", "Code"),
                ):
                    raw_length = _child_text(properties, length_property)
                    try:
                        exists = raw_length is not None and int(raw_length) > 0
                    except ValueError:
                        exists = False
                    if exists:
                        required_paths.append(f"{prefix}.{standard_name}")
                if property_name == "DefaultObjectForm":
                    required_paths.extend(f"Объект.{name}" for name in field_names)
            elif kind.object_kind == "Документ":
                if property_name == "DefaultObjectForm":
                    required_paths = ["Объект.Number", "Объект.Date"]
                    required_paths.extend(f"Объект.{name}" for name in field_names)
                elif property_name in {"DefaultListForm", "DefaultChoiceForm"}:
                    required_paths = ["Список.Date", "Список.Number"]
            elif kind.object_kind == "РегистрСведений":
                prefix = (
                    "Запись" if property_name == "DefaultRecordForm" else "Список"
                )
                required_paths = [f"{prefix}.{name}" for name in field_names]
            if form_entry is not None and required_paths:
                actual_paths = {
                    (node.text or "").strip()
                    for node in form_entry[0].iter()
                    if _local(node.tag) == "DataPath" and (node.text or "").strip()
                }
                for required_path in required_paths:
                    if required_path not in actual_paths:
                        report.fail(
                            "forms",
                            "required_standard_field_missing",
                            form_xml_path,
                            "Форма по умолчанию обязана выводить "
                            f"`{required_path}` по профилю Конфигуратора.",
                        )

            expected_type = None
            if kind.object_kind == "Документ" and property_name == "DefaultObjectForm":
                expected_type = f"cfg:DocumentObject.{object_name}"
            elif kind.object_kind == "Обработка" and property_name == "DefaultForm":
                expected_type = f"cfg:DataProcessorObject.{object_name}"
            elif kind.object_kind == "Отчет" and property_name == "DefaultForm":
                expected_type = f"cfg:ReportObject.{object_name}"
            if (
                expected_type is not None
                and expected_type not in form_main_types.get(default_form_name, [])
            ):
                report.fail(
                    "forms",
                    "form_owner_mismatch",
                    form_xml_path,
                    f"Форма из {property_name} должна иметь главный реквизит "
                    f"типа `{expected_type}`.",
                )


def check_metadata_artifacts(
    object_ref: str,
    format_version: str,
    artifacts: dict[str, str] | list[dict[str, str]],
) -> dict[str, object]:
    """Проверить bundle без чтения или записи файлов и без импорта в 1С."""

    if not isinstance(object_ref, str):
        raise TypeError("object_ref должен быть строкой")
    report = _Report(object_ref)
    match = _OBJECT_REF.fullmatch(object_ref)
    if match is None:
        report.fail_all(
            "unsupported_object_ref",
            "$object_ref",
            "Поддержаны только Справочник.<Имя>, Документ.<Имя>, РегистрСведений.<Имя>, встроенная Обработка.<Имя> и Отчет.<Имя>.",
        )
        return report.result()
    if (
        not isinstance(format_version, str)
        or _FORMAT_VERSION.fullmatch(format_version) is None
    ):
        report.fail_all(
            "invalid_format_version",
            "$format_version",
            "format_version должен иметь вид числовой точечной версии, например `2.20`.",
        )
        return report.result()
    kind = _KINDS[match.group(1)]
    object_name = match.group(2)
    normalized = _normalize_artifacts(artifacts, report)
    if normalized is None:
        return report.result()
    artifacts = normalized
    unsafe_path = next(
        (
            path
            for path in artifacts
            if "\\" in path
            or path.startswith("/")
            or any(segment in {"", ".", ".."} for segment in path.split("/"))
        ),
        None,
    )
    if unsafe_path is not None:
        report.fail_all(
            "unsafe_artifact_path",
            unsafe_path,
            "Путь артефакта должен состоять из безопасных owner-relative сегментов.",
        )
        return report.result()
    configuration_path = next(
        (path for path in artifacts if path == "Configuration.xml" or path.endswith("/Configuration.xml")),
        None,
    )
    if configuration_path is not None:
        report.fail_all(
            "external_configuration_not_supported",
            configuration_path,
            "Configuration.xml не является owner-relative артефактом и не поддерживается checker.",
        )
        return report.result()
    owner_path = f"{kind.directory}/{object_name}"
    invalid_path = next(
        (
            path
            for path in artifacts
            if path != f"{owner_path}.xml" and not path.startswith(owner_path + "/")
        ),
        None,
    )
    if invalid_path is not None:
        report.fail_all(
            "non_owner_relative_artifact",
            invalid_path,
            "Checker принимает только owner-relative артефакты переданного object_ref.",
        )
        return report.result()
    if len(artifacts) > MAX_ARTIFACTS:
        report.fail_all(
            "too_many_artifacts",
            "$artifacts",
            f"Передано более {MAX_ARTIFACTS} артефактов.",
        )
        return report.result()
    sizes: dict[str, int] = {}
    for path, text in artifacts.items():
        try:
            sizes[path] = len(text.encode("utf-8"))
        except UnicodeEncodeError:
            report.fail_all(
                "invalid_unicode", path, "Артефакт содержит недопустимый Unicode."
            )
            return report.result()
        if sizes[path] > MAX_ARTIFACT_BYTES:
            report.fail_all(
                "artifact_too_large",
                path,
                f"Артефакт превышает {MAX_ARTIFACT_BYTES} байт.",
            )
    if any(size > MAX_ARTIFACT_BYTES for size in sizes.values()):
        return report.result()
    if sum(sizes.values()) > MAX_TOTAL_BYTES:
        report.fail_all(
            "artifacts_too_large",
            "$artifacts",
            f"Суммарный размер превышает {MAX_TOTAL_BYTES} байт.",
        )
        return report.result()

    parsed: dict[str, tuple[ET.Element, set[str]]] = {}
    for path, text in artifacts.items():
        if not path.endswith(".xml"):
            continue
        root, namespaces = _parse_xml(path, text, report)
        if root is not None:
            parsed[path] = (root, namespaces)
            if path.endswith("/Ext/Form.xml"):
                if root.tag != f"{{{LOGFORM_NAMESPACE}}}Form":
                    report.fail(
                        "xml",
                        "invalid_form_xml_root",
                        path,
                        "Корень внутреннего Form.xml должен быть Form.",
                    )
            elif path.endswith("/Ext/Template.xml"):
                if root.tag != f"{{{DCS_SCHEMA_NAMESPACE}}}DataCompositionSchema":
                    report.fail(
                        "xml",
                        "invalid_dcs_xml_root",
                        path,
                        "Корень DCS Template.xml должен быть DataCompositionSchema.",
                    )
            elif root.tag != f"{{{MD_NAMESPACE}}}MetaDataObject":
                report.fail(
                    "xml",
                    "invalid_metadata_root",
                    path,
                    "Корень XML descriptor должен быть MDClasses MetaDataObject.",
                )
    _check_qnames(parsed, report)
    _check_unique_ids(parsed, report)

    descriptor_path = f"{owner_path}.xml"
    descriptor_entry = parsed.get(descriptor_path)
    if descriptor_entry is None:
        report.fail(
            "descriptor",
            "missing_metadata_descriptor",
            descriptor_path,
            "Отсутствует descriptor объекта метаданных.",
        )
        report.failed_coverage.update({"generated_types", "forms"})
        return report.result()
    descriptor_root = descriptor_entry[0]
    _check_format_version(
        descriptor_root, descriptor_path, report, "descriptor", format_version
    )
    if not _require_metadata_root(
        descriptor_root, descriptor_path, report, "descriptor"
    ):
        report.failed_coverage.update({"generated_types", "forms"})
        return report.result()
    descriptor_children = list(descriptor_root)
    if (
        len(descriptor_children) != 1
        or _local(descriptor_children[0].tag) != kind.xml_kind
    ):
        report.fail(
            "descriptor",
            "invalid_metadata_descriptor",
            descriptor_path,
            f"Descriptor должен содержать ровно один {kind.xml_kind}.",
        )
        report.failed_coverage.update({"generated_types", "forms"})
        return report.result()
    metadata_object = descriptor_children[0]
    if not metadata_object.attrib.get("uuid", "").strip():
        report.fail(
            "descriptor",
            "missing_metadata_uuid",
            descriptor_path,
            f"{kind.xml_kind} не имеет uuid.",
        )
    properties = _find_child(metadata_object, "Properties")
    if _child_text(properties, "Name") != object_name:
        report.fail(
            "descriptor",
            "metadata_name_mismatch",
            descriptor_path,
            f"Properties/Name должен быть `{object_name}`.",
        )
    if kind.xml_kind == "InformationRegister":
        if _find_child(properties, "Periodicity") is not None:
            report.fail(
                "descriptor",
                "unsupported_periodicity_property",
                descriptor_path,
                "Используйте InformationRegisterPeriodicity вместо Periodicity.",
            )
        if _child_text(properties, "InformationRegisterPeriodicity") is None:
            report.fail(
                "descriptor",
                "missing_information_register_periodicity",
                descriptor_path,
                "Отсутствует InformationRegisterPeriodicity.",
            )
    if kind.xml_kind == "Document":
        number_profile = {
            "NumberType": {"String"},
            "NumberAllowedLength": {"Variable", "Fixed"},
            "NumberPeriodicity": {"Nonperiodical", "Year"},
            "CheckUnique": {"true", "false"},
            "Autonumbering": {"true", "false"},
        }
        for property_name, allowed in number_profile.items():
            if _child_text(properties, property_name) not in allowed:
                report.fail(
                    "descriptor",
                    "invalid_document_number_profile",
                    f"{descriptor_path}:{property_name}",
                    f"{property_name} должен иметь одно из значений: {', '.join(sorted(allowed))}.",
                )
        raw_number_length = _child_text(properties, "NumberLength")
        try:
            valid_number_length = (
                raw_number_length is not None
                and 1 <= int(raw_number_length) <= 50
                and str(int(raw_number_length)) == raw_number_length
            )
        except ValueError:
            valid_number_length = False
        if not valid_number_length:
            report.fail(
                "descriptor",
                "invalid_document_number_profile",
                f"{descriptor_path}:NumberLength",
                "NumberLength должен быть целым числом от 1 до 50.",
            )
        for property_name in ("Posting", "RealTimePosting"):
            if _child_text(properties, property_name) != "Deny":
                report.fail(
                    "descriptor",
                    "unsupported_document_posting",
                    f"{descriptor_path}:{property_name}",
                    (
                        f"{property_name} должен быть `Deny`: schema v1 "
                        "поддерживает только базовый непроводимый документ."
                    ),
                )
        standard_attributes = _find_child(properties, "StandardAttributes")
        found_standard = [
            item.attrib.get("name", "")
            for item in (standard_attributes if standard_attributes is not None else ())
            if _local(item.tag) == "StandardAttribute"
        ]
        expected_standard = ["Posted", "Ref", "DeletionMark", "Date", "Number"]
        if found_standard != expected_standard:
            report.fail(
                "descriptor",
                "invalid_document_standard_attributes",
                f"{descriptor_path}:StandardAttributes",
                "Стандартные реквизиты документа должны быть Posted, Ref, DeletionMark, Date, Number в каноническом порядке.",
            )
        input_by_string = _find_child(properties, "InputByString")
        input_fields = [
            (item.text or "").strip()
            for item in (input_by_string if input_by_string is not None else ())
            if _local(item.tag) == "Field" and (item.text or "").strip()
        ]
        if input_fields != [f"Document.{object_name}.StandardAttribute.Number"]:
            report.fail(
                "descriptor",
                "invalid_document_input_by_string",
                f"{descriptor_path}:InputByString",
                "InputByString документа должен ссылаться только на стандартный реквизит Number.",
            )
        child_objects = _find_child(metadata_object, "ChildObjects")
        unsupported_children = [
            _local(item.tag)
            for item in (child_objects if child_objects is not None else ())
            if _local(item.tag) not in {"Form", "Attribute"}
        ]
        if unsupported_children:
            report.fail(
                "descriptor",
                "unsupported_document_structure",
                f"{descriptor_path}:ChildObjects",
                "Базовый документ поддерживает только Attribute и Form; табличные части и другие дочерние объекты не поддержаны.",
            )
    if kind.xml_kind == "DataProcessor":
        child_objects = _find_child(metadata_object, "ChildObjects")
        unsupported_children = [
            _local(item.tag)
            for item in (child_objects if child_objects is not None else ())
            if _local(item.tag) != "Form"
        ]
        if unsupported_children:
            report.fail(
                "descriptor",
                "unsupported_data_processor_structure",
                f"{descriptor_path}:ChildObjects",
                "Базовая обработка поддерживает только Form; команды и шаблоны не поддержаны.",
            )
    _check_generated_types(
        metadata_object, descriptor_path, kind, object_name, report
    )
    _check_forms(
        owner_path,
        metadata_object,
        artifacts,
        parsed,
        format_version,
        object_name,
        kind,
        report,
    )
    return report.result()


__all__ = [
    "MAX_ARTIFACTS",
    "MAX_ARTIFACT_BYTES",
    "MAX_TOTAL_BYTES",
    "check_metadata_artifacts",
]
