"""Ленивое чтение полной модели одного XDTO-пакета из generation member."""

from __future__ import annotations

import hashlib
import io
import xml.etree.ElementTree as ET
from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType

from .intake_v2_collector import CollectionError, open_collection_member
from .model import Configuration, MetadataObject


NS_XDTO = "http://v8.1c.ru/8.1/xdto"
NS_XML_SCHEMA = "http://www.w3.org/2001/XMLSchema"
PLATFORM_NAMESPACES = frozenset(
    {
        NS_XDTO,
        NS_XML_SCHEMA,
        "http://v8.1c.ru/8.1/data/core",
        "http://v8.1c.ru/8.1/data/enterprise",
    }
)
MAX_PACKAGE_SIZE = 64 << 20
MAX_PACKAGE_ELEMENTS = 1_000_000
MAX_NAMESPACE_DECLARATIONS = 100_000
MAX_NAMESPACE_SCOPE_SIZE = 10_000
MAX_PACKAGE_DEPTH = 512
QNAME_ATTRIBUTES = frozenset({"type", "ref", "base", "itemType", "memberTypes"})


class XDTOReadError(ValueError):
    """Сохранённый XDTO member нельзя безопасно прочитать."""


class _NamespaceScope(Mapping[str, str]):
    """Неизменяемый слой namespace без копирования родительской области."""

    __slots__ = ("_local", "_parent", "_size")

    def __init__(
        self,
        parent: Mapping[str, str] | None = None,
        local: Mapping[str, str] | None = None,
    ) -> None:
        self._parent = parent
        self._local = MappingProxyType(dict(local or {}))
        inherited = len(parent) if parent is not None else 0
        self._size = (
            inherited + sum(key not in parent for key in self._local)
            if parent is not None
            else len(self._local)
        )

    def __getitem__(self, key: str) -> str:
        try:
            return self._local[key]
        except KeyError:
            if self._parent is None:
                raise
            return self._parent[key]

    def __iter__(self) -> Iterator[str]:
        yield from self._local
        if self._parent is not None:
            yield from (key for key in self._parent if key not in self._local)

    def __len__(self) -> int:
        return self._size


_EMPTY_NAMESPACE_SCOPE: Mapping[str, str] = _NamespaceScope()


@dataclass(frozen=True, slots=True)
class XDTOReference:
    raw: str
    namespace: str
    name: str
    state: str
    target: str = ""


@dataclass(frozen=True, slots=True)
class XDTOMemberDetails:
    properties: tuple[Mapping[str, str], ...]
    references: tuple[XDTOReference, ...]
    enumerations: tuple[str, ...]
    patterns: tuple[str, ...]
    unknown_nodes: tuple[str, ...]


def read_package_member(
    root: str | Path,
    relative_path: str,
    *,
    expected_size: int,
    expected_sha256: str,
) -> bytes:
    """Прочитать только выбранный member и повторно проверить identity."""
    if expected_size < 0 or expected_size > MAX_PACKAGE_SIZE:
        raise XDTOReadError("размер XDTO package выходит за допустимый предел")
    try:
        with open_collection_member(Path(root), relative_path) as stream:
            payload = stream.read(expected_size + 1)
    except (CollectionError, OSError) as error:
        raise XDTOReadError("XDTO package недоступен") from error
    if (
        len(payload) != expected_size
        or hashlib.sha256(payload).hexdigest() != expected_sha256
    ):
        raise XDTOReadError("XDTO package изменён после публикации")
    return payload


def _parse_with_namespaces(
    payload: bytes,
) -> tuple[ET.Element, dict[int, Mapping[str, str]]]:
    if len(payload) > MAX_PACKAGE_SIZE:
        raise XDTOReadError("размер XDTO package выходит за допустимый предел")
    namespaces: dict[int, Mapping[str, str]] = {}
    stack: list[Mapping[str, str]] = []
    pending: dict[str, str] = {}
    root: ET.Element | None = None
    elements = 0
    declarations = 0
    try:
        events = ET.iterparse(
            io.BytesIO(payload), events=("start", "end", "start-ns", "end-ns")
        )
        for event, value in events:
            if event == "start-ns":
                prefix, namespace = value
                pending[prefix or ""] = namespace
                declarations += 1
                if declarations > MAX_NAMESPACE_DECLARATIONS:
                    raise XDTOReadError(
                        "XDTO package превышает предел объявлений namespace"
                    )
            elif event == "start":
                element = value
                elements += 1
                if elements > MAX_PACKAGE_ELEMENTS:
                    raise XDTOReadError("XDTO package превышает предел XML-элементов")
                parent = stack[-1] if stack else _EMPTY_NAMESPACE_SCOPE
                if pending:
                    current = _NamespaceScope(parent, pending)
                    if len(current) > MAX_NAMESPACE_SCOPE_SIZE:
                        raise XDTOReadError(
                            "XDTO package превышает предел namespace в области"
                        )
                else:
                    current = parent
                pending.clear()
                stack.append(current)
                if len(stack) > MAX_PACKAGE_DEPTH:
                    raise XDTOReadError("XDTO package превышает предел глубины XML")
                if any(
                    attribute.rsplit("}", 1)[-1] in QNAME_ATTRIBUTES
                    for attribute in element.attrib
                ):
                    namespaces[id(element)] = current
                if root is None:
                    root = element
            elif event == "end":
                stack.pop()
    except ET.ParseError as error:
        raise XDTOReadError("XDTO package не является XML") from error
    if root is None or _tag(root) != "package" or _namespace(root) != NS_XDTO:
        raise XDTOReadError("XDTO package имеет неверный корень")
    return root, namespaces


def _tag(element: ET.Element) -> str:
    return element.tag.rsplit("}", 1)[-1]


def _namespace(element: ET.Element) -> str:
    return element.tag[1:].split("}", 1)[0] if element.tag.startswith("{") else ""


def _catalog(configuration: Configuration) -> dict[str, dict[str, str]]:
    result: dict[str, dict[str, str]] = {}
    for obj in configuration.objects.values():
        if obj.kind != "ПакетXDTO":
            continue
        namespace = obj.extended.get("target_namespace")
        if not isinstance(namespace, str) or not namespace:
            continue
        names: dict[str, str] = {}
        for key, public_kind in (
            ("object_types", "ТипОбъекта"),
            ("value_types", "ТипЗначения"),
            ("properties", "Свойство"),
        ):
            values = obj.extended.get(key, [])
            if not isinstance(values, list):
                continue
            for name in values:
                if isinstance(name, str) and name:
                    symbol_space = "property" if key == "properties" else "type"
                    names[f"{symbol_space}:{name}"] = (
                        f"{obj.full_name}.{public_kind}.{name}"
                    )
        result[namespace] = names
    return result


def _qname(
    value: str,
    element: ET.Element,
    namespaces: Mapping[int, Mapping[str, str]],
) -> tuple[str, str]:
    value = value.strip()
    if value.startswith("{") and "}" in value:
        namespace, name = value[1:].split("}", 1)
        return namespace, name
    if ":" in value:
        prefix, name = value.split(":", 1)
        return namespaces.get(id(element), {}).get(prefix, ""), name
    return namespaces.get(id(element), {}).get("", ""), value


def _canonical_qname(value: str, scope: Mapping[str, str]) -> str:
    value = value.strip()
    if value.startswith("{") and "}" in value:
        namespace, name = value[1:].split("}", 1)
        return f"{{{namespace}}}{name}"
    if ":" in value:
        prefix, name = value.split(":", 1)
        namespace = scope.get(prefix)
        return f"{{{namespace}}}{name}" if namespace is not None else value
    namespace = scope.get("")
    return f"{{{namespace}}}{value}" if namespace is not None else value


def semantic_package_bytes(payload: bytes) -> bytes:
    """Канонизировать XML вместе со смыслом QName в значениях атрибутов."""
    root, namespaces = _parse_with_namespaces(payload)
    for element in root.iter():
        scope = namespaces.get(id(element), {})
        for attribute, raw in tuple(element.attrib.items()):
            local_name = attribute.rsplit("}", 1)[-1]
            if local_name not in QNAME_ATTRIBUTES:
                continue
            values = raw.split() if local_name == "memberTypes" else [raw]
            element.set(
                attribute,
                " ".join(_canonical_qname(value, scope) for value in values),
            )
    try:
        canonical = ET.canonicalize(
            xml_data=ET.tostring(root, encoding="unicode"),
            with_comments=False,
            strip_text=True,
        )
    except (ET.ParseError, ValueError) as error:
        raise XDTOReadError("XDTO package не является XML") from error
    return canonical.encode("utf-8")


def _reference(
    raw: str,
    element: ET.Element,
    namespaces: Mapping[int, Mapping[str, str]],
    package_namespace: str,
    imports: frozenset[str],
    catalog: Mapping[str, Mapping[str, str]],
    symbol_space: str,
) -> XDTOReference:
    namespace, name = _qname(raw, element, namespaces)
    target = catalog.get(namespace, {}).get(f"{symbol_space}:{name}", "")
    if target and namespace == package_namespace:
        state = "local"
    elif target and namespace in imports:
        state = "imported"
    elif namespace in PLATFORM_NAMESPACES:
        state = "platform"
        target = f"{{{namespace}}}{name}"
    else:
        state = "unresolved"
    return XDTOReference(raw, namespace, name, state, target)


def member_details(
    payload: bytes,
    obj: MetadataObject,
    configuration: Configuration,
) -> XDTOMemberDetails:
    """Разобрать выбранное определение; остальные package roots не удерживать."""
    root, namespaces = _parse_with_namespaces(payload)
    package_namespace = root.attrib.get("targetNamespace", "")
    imports = frozenset(
        child.attrib.get("namespace", "")
        for child in root
        if _tag(child) == "import"
    )
    catalog = _catalog(configuration)
    source_kind = {
        "ТипОбъекта": "objectType",
        "ТипЗначения": "valueType",
        "Свойство": "property",
    }.get(str(obj.extended.get("member_kind", "")))
    selected = root
    if source_kind is not None:
        selected = next(
            (
                child
                for child in root
                if _tag(child) == source_kind
                and child.attrib.get("name") == obj.name
            ),
            None,
        )
        if selected is None:
            raise XDTOReadError("определение XDTO отсутствует в package member")

    references: list[XDTOReference] = []
    properties: list[Mapping[str, str]] = []
    enumerations: list[str] = []
    patterns: list[str] = []
    known = {
        "package",
        "import",
        "property",
        "valueType",
        "objectType",
        "typeDef",
        "enumeration",
        "pattern",
    }
    unknown: set[str] = set()
    for element in selected.iter():
        tag = _tag(element)
        if tag not in known:
            unknown.add(tag)
        if tag == "property" and (element is not selected or source_kind == "property"):
            property_data = {}
            accepted = {
                "name",
                "ref",
                "type",
                "form",
                "lowerBound",
                "upperBound",
                "qualified",
            }
            for key, value in element.attrib.items():
                local_name = key.rsplit("}", 1)[-1]
                if local_name in accepted:
                    property_data[local_name] = value
            if any(_tag(child) == "typeDef" for child in element):
                property_data["typeDef"] = "anonymous"
            elif not any(
                key in property_data for key in ("type", "ref", "typeDef")
            ):
                property_data["type"] = "anyType (implicit)"
            properties.append(property_data)
        if tag == "enumeration":
            value = element.attrib.get("value", (element.text or "").strip())
            if value:
                enumerations.append(value)
        elif tag == "pattern":
            value = element.attrib.get("value", (element.text or "").strip())
            if value:
                patterns.append(value)
        for attribute, raw in element.attrib.items():
            attribute_namespace = (
                attribute[1:].split("}", 1)[0] if attribute.startswith("{") else ""
            )
            local_name = attribute.rsplit("}", 1)[-1]
            if attribute_namespace or local_name not in QNAME_ATTRIBUTES:
                continue
            values = raw.split() if local_name == "memberTypes" else [raw]
            references.extend(
                _reference(
                    value,
                    element,
                    namespaces,
                    package_namespace,
                    imports,
                    catalog,
                    "property" if local_name == "ref" else "type",
                )
                for value in values
                if value.strip()
            )
    return XDTOMemberDetails(
        tuple(properties),
        tuple(references),
        tuple(enumerations),
        tuple(patterns),
        tuple(sorted(unknown)),
    )


def package_references(
    payload: bytes,
    catalog: Mapping[str, Mapping[str, str]],
) -> tuple[XDTOReference, ...]:
    """Разрешить все доказанные QName/Clark-ссылки одного пакета."""
    root, namespaces = _parse_with_namespaces(payload)
    package_namespace = root.attrib.get("targetNamespace", "")
    imports = frozenset(
        child.attrib.get("namespace", "")
        for child in root
        if _tag(child) == "import"
    )
    result: list[XDTOReference] = []
    for element in root.iter():
        for attribute, raw in element.attrib.items():
            attribute_namespace = (
                attribute[1:].split("}", 1)[0] if attribute.startswith("{") else ""
            )
            local_name = attribute.rsplit("}", 1)[-1]
            if attribute_namespace or local_name not in QNAME_ATTRIBUTES:
                continue
            values = raw.split() if local_name == "memberTypes" else [raw]
            result.extend(
                _reference(
                    value,
                    element,
                    namespaces,
                    package_namespace,
                    imports,
                    catalog,
                    "property" if local_name == "ref" else "type",
                )
                for value in values
                if value.strip()
            )
    return tuple(result)


__all__ = [
    "XDTOMemberDetails",
    "XDTOReadError",
    "XDTOReference",
    "member_details",
    "package_references",
    "read_package_member",
    "semantic_package_bytes",
]
