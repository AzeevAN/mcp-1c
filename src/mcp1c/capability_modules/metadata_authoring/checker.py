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
_FORBIDDEN_XML_DECLARATION = re.compile(r"<!\s*(?:DOCTYPE|ENTITY)\b", re.I)
_QNAME = re.compile(r"^([A-Za-z_][A-Za-z0-9_.-]*):[^\s:]+$")
_OBJECT_REF = re.compile(
    r"^(Справочник|РегистрСведений)\.([A-Za-zА-Яа-яЁё_][A-Za-zА-Яа-яЁё0-9_]*)$"
)
_FORMAT_VERSION = re.compile(r"^\d+\.\d+(?:\.\d+)*$")
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
    default_form_property: str


_KINDS = {
    "Справочник": _Kind(
        "Справочник",
        "Catalog",
        "Catalogs",
        ("Object", "Ref", "Selection", "List", "Manager"),
        "DefaultObjectForm",
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
        "DefaultRecordForm",
    ),
}


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


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
                    "Статическая проверка пройдена; нативный импорт в 1С не проверен."
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
                if type_kind in {"Catalogs", "InformationRegisters"}:
                    report.fail(
                        "namespaces",
                        "unsupported_metadata_type",
                        path,
                        (
                            f"QName `{value}` использует физическое имя каталога. "
                            "Для ссылки на справочник нужен "
                            "`cfg:CatalogRef.<ИмяСправочника>`."
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
    child_objects = _find_child(metadata_object, "ChildObjects")
    form_names = [
        (child.text or "").strip()
        for child in (child_objects if child_objects is not None else ())
        if _local(child.tag) == "Form" and (child.text or "").strip()
    ]
    if len(set(form_names)) != len(form_names):
        report.fail(
            "forms",
            "duplicate_form",
            f"{owner_path}.xml",
            "Имя формы нельзя повторять в ChildObjects/Form.",
        )
    for form_name in form_names:
        descriptor_path = f"{owner_path}/Forms/{form_name}.xml"
        form_xml_path = f"{owner_path}/Forms/{form_name}/Ext/Form.xml"
        module_path = f"{owner_path}/Forms/{form_name}/Ext/Form/Module.bsl"
        for path, code, message in (
            (
                descriptor_path,
                "missing_form_descriptor",
                f"Отсутствует descriptor формы `{form_name}`.",
            ),
            (form_xml_path, "missing_form_xml", f"Отсутствует Form.xml формы `{form_name}`."),
            (module_path, "missing_form_module", f"Отсутствует Module.bsl формы `{form_name}`."),
        ):
            if path not in artifacts:
                report.fail("forms", code, path, message)
        internal_form_entry = parsed.get(form_xml_path)
        if internal_form_entry is not None:
            _check_format_version(
                internal_form_entry[0], form_xml_path, report, "forms", format_version
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
    default_form = _child_text(properties, kind.default_form_property)
    if form_names and default_form is None:
        report.fail(
            "forms",
            "missing_default_form",
            f"{owner_path}.xml:{kind.default_form_property}",
            f"При объявленных формах требуется {kind.default_form_property}.",
        )
    if default_form is not None:
        allowed = {
            f"{kind.xml_kind}.{object_name}.Form.{form_name}"
            for form_name in form_names
        }
        if default_form not in allowed:
            report.fail(
                "forms",
                "unknown_default_form",
                f"{owner_path}.xml:{kind.default_form_property}",
                (
                    f"Форма по умолчанию `{default_form}` должна иметь вид "
                    f"`{kind.xml_kind}.{object_name}.Form.<Форма>`, где "
                    "<Форма> — короткое значение ChildObjects/Form."
                ),
            )
        elif kind.object_kind == "Справочник":
            required_paths = []
            for property_name, data_path in (
                ("DescriptionLength", "Объект.Наименование"),
                ("CodeLength", "Объект.Код"),
            ):
                raw_length = _child_text(properties, property_name)
                try:
                    exists = raw_length is not None and int(raw_length) > 0
                except ValueError:
                    exists = False
                if exists:
                    required_paths.append(data_path)
            default_form_name = default_form.rsplit(".", 1)[-1]
            form_xml_path = (
                f"{owner_path}/Forms/{default_form_name}/Ext/Form.xml"
            )
            form_entry = parsed.get(form_xml_path)
            if form_entry is not None:
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
                            (
                                f"Основная форма справочника обязана выводить "
                                f"`{required_path}`, потому что соответствующая "
                                "длина стандартного реквизита больше нуля."
                            ),
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
            "Поддержаны только Справочник.<Имя> и РегистрСведений.<Имя>.",
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
