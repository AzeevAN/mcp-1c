"""Дешёвый probe личности и раскладки до доменного разбора source B."""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass, replace
from enum import Enum
from pathlib import PurePosixPath
from types import MappingProxyType
from typing import Iterable, Mapping

from .intake_v2 import (
    CandidateTransport,
    ExportIdentity,
    SourceKind,
    VirtualExportTree,
)


_NS_MDCLASSES = "http://v8.1c.ru/8.3/MDClasses"
_MAX_CONFIGURATION_XML_SIZE = 8 * 1024 * 1024
_SHA256_RE = re.compile(r"[0-9a-f]{64}\Z")
_CANDIDATE_ID_RE = re.compile(r"[A-Za-z0-9._-]+\Z")
_TREE_ROOTS = frozenset(
    {
        "AccumulationRegisters",
        "BusinessProcesses",
        "Bots",
        "Catalogs",
        "ChartsOfAccounts",
        "ChartsOfCalculationTypes",
        "ChartsOfCharacteristicTypes",
        "CommonAttributes",
        "CommonCommands",
        "CommonForms",
        "CommonModules",
        "DataProcessors",
        "DocumentJournals",
        "Documents",
        "Enums",
        "EventSubscriptions",
        "ExchangePlans",
        "FilterCriteria",
        "HTTPServices",
        "InformationRegisters",
        "Reports",
        "Roles",
        "ScheduledJobs",
        "Sequences",
        "SessionParameters",
        "Tasks",
        "WebServices",
    }
)


class ProbeError(ValueError):
    """Кандидат не даёт доказуемой личности или устойчивого снимка."""


class ExportLayout(str, Enum):
    UNKNOWN = "unknown"
    FLAT = "flat"
    TREE = "tree"
    MIXED = "mixed"


class CandidateState(str, Enum):
    CURRENT = "current"
    DUPLICATE = "duplicate"
    REPARSE = "reparse"
    DIFFERENT_SNAPSHOT = "different_snapshot"
    FOREIGN_IDENTITY = "foreign_identity"


def _required_text(value: object, label: str) -> str:
    if not isinstance(value, str) or not value or value.strip() != value:
        raise ProbeError(f"{label} должен быть непустой строкой")
    if "\x00" in value:
        raise ProbeError(f"{label} содержит недопустимый символ")
    return value


def _sha256(value: object, label: str) -> str:
    if not isinstance(value, str) or not _SHA256_RE.fullmatch(value):
        raise ProbeError(f"{label} должен быть SHA-256 в нижнем регистре")
    return value


def _positive_int(value: object, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ProbeError(f"{label} должен быть положительным целым")
    return value


@dataclass(frozen=True, slots=True)
class CandidateProbe:
    source_kind: SourceKind
    internal_name: str
    configuration_version: str
    layout: ExportLayout
    wrapper: str
    raw_sha256: str
    snapshot_fingerprint: str
    transport: CandidateTransport
    origin_name: str

    def __post_init__(self) -> None:
        if not isinstance(self.source_kind, SourceKind):
            raise ProbeError("source_kind должен быть SourceKind")
        _required_text(self.internal_name, "internal_name")
        if not isinstance(self.configuration_version, str):
            raise ProbeError("configuration_version должен быть строкой")
        if not isinstance(self.layout, ExportLayout):
            raise ProbeError("layout должен быть ExportLayout")
        if not isinstance(self.wrapper, str) or "/" in self.wrapper:
            raise ProbeError("wrapper должен быть одним компонентом пути")
        _sha256(self.raw_sha256, "raw_sha256")
        _sha256(self.snapshot_fingerprint, "snapshot_fingerprint")
        if not isinstance(self.transport, CandidateTransport):
            raise ProbeError("transport должен быть CandidateTransport")
        _required_text(self.origin_name, "origin_name")

    def bind(
        self,
        candidate_id: str,
        *,
        parent_configuration: str = "",
    ) -> BoundCandidate:
        if not isinstance(candidate_id, str) or not _CANDIDATE_ID_RE.fullmatch(
            candidate_id
        ):
            raise ProbeError("candidate_id имеет недопустимый формат")
        if self.source_kind is SourceKind.CONFIGURATION:
            if parent_configuration:
                raise ProbeError("основная конфигурация не принимает родителя")
            identity = ExportIdentity.configuration(self.internal_name)
        else:
            if not parent_configuration:
                raise ProbeError("для расширения обязательна родительская конфигурация")
            identity = ExportIdentity.extension(
                self.internal_name,
                parent_configuration=parent_configuration,
            )
        return BoundCandidate(candidate_id, identity, self.raw_sha256, self)

    def to_dict(self) -> dict[str, str]:
        return {
            "source_kind": self.source_kind.value,
            "internal_name": self.internal_name,
            "configuration_version": self.configuration_version,
            "layout": self.layout.value,
            "wrapper": self.wrapper,
            "raw_sha256": self.raw_sha256,
            "snapshot_fingerprint": self.snapshot_fingerprint,
            "transport": self.transport.value,
            "origin_name": self.origin_name,
        }

    @classmethod
    def from_dict(cls, raw: object) -> CandidateProbe:
        if not isinstance(raw, dict):
            raise ProbeError("probe должен быть объектом")
        try:
            return cls(
                source_kind=SourceKind(raw["source_kind"]),
                internal_name=raw["internal_name"],
                configuration_version=raw.get("configuration_version", ""),
                layout=ExportLayout(raw["layout"]),
                wrapper=raw.get("wrapper", ""),
                raw_sha256=raw["raw_sha256"],
                snapshot_fingerprint=raw["snapshot_fingerprint"],
                transport=CandidateTransport(raw["transport"]),
                origin_name=raw["origin_name"],
            )
        except (KeyError, TypeError, ValueError) as error:
            if isinstance(error, ProbeError):
                raise
            raise ProbeError("probe содержит неверные поля") from error


@dataclass(frozen=True, slots=True)
class BoundCandidate:
    candidate_id: str
    identity: ExportIdentity
    raw_sha256: str
    probe: CandidateProbe

    def __post_init__(self) -> None:
        if not isinstance(self.candidate_id, str) or not _CANDIDATE_ID_RE.fullmatch(
            self.candidate_id
        ):
            raise ProbeError("candidate_id имеет недопустимый формат")
        if not isinstance(self.identity, ExportIdentity):
            raise ProbeError("identity должен быть ExportIdentity")
        _sha256(self.raw_sha256, "raw_sha256")
        if not isinstance(self.probe, CandidateProbe):
            raise ProbeError("probe должен быть CandidateProbe")

    @property
    def grouping_key(self) -> tuple[str, ...]:
        return self.identity.grouping_key

    def with_raw_sha256(self, raw_sha256: str) -> BoundCandidate:
        return replace(self, raw_sha256=raw_sha256)


def _semantic_hashes(value: Mapping[str, str]) -> Mapping[str, str]:
    if not isinstance(value, Mapping) or not value:
        raise ProbeError("semantic_hashes должен быть непустым mapping")
    normalized: dict[str, str] = {}
    for name, digest in value.items():
        _required_text(name, "имя semantic hash")
        normalized[name] = _sha256(digest, f"semantic hash {name}")
    return MappingProxyType(dict(sorted(normalized.items())))


@dataclass(frozen=True, slots=True)
class ActiveSnapshot:
    identity: ExportIdentity
    raw_sha256: str
    semantic_hashes: Mapping[str, str]
    parser_version: int
    selection_version: int

    def __post_init__(self) -> None:
        if not isinstance(self.identity, ExportIdentity):
            raise ProbeError("identity должен быть ExportIdentity")
        _sha256(self.raw_sha256, "raw_sha256")
        object.__setattr__(self, "semantic_hashes", _semantic_hashes(self.semantic_hashes))
        _positive_int(self.parser_version, "parser_version")
        _positive_int(self.selection_version, "selection_version")


def _configuration_path(paths: tuple[str, ...]) -> tuple[str, str]:
    candidates = tuple(
        path for path in paths if PurePosixPath(path).name == "Configuration.xml"
    )
    if not candidates:
        raise ProbeError("в кандидате не найден Configuration.xml")
    if len(candidates) != 1:
        raise ProbeError("найдено несколько Configuration.xml: корень неоднозначен")
    path = candidates[0]
    parts = PurePosixPath(path).parts
    if len(parts) == 1:
        return path, ""
    if len(parts) != 2 or any(
        PurePosixPath(member).parts[:1] != parts[:1] for member in paths
    ):
        raise ProbeError(
            "Configuration.xml должен быть в корне или одном общем wrapper"
        )
    return path, parts[0]


def _layout(paths: tuple[str, ...], wrapper: str) -> ExportLayout:
    flat = False
    tree = False
    for path in paths:
        parts = PurePosixPath(path).parts
        if wrapper:
            parts = parts[1:]
        if not parts:
            continue
        relative = PurePosixPath(*parts)
        if relative.name == "Configuration.xml":
            continue
        flat = flat or relative.suffix in {".txt", ".Form"}
        tree = tree or (
            relative.suffix == ".bsl"
            or relative.name in {"Form.xml", "Form.bin"}
            or (len(relative.parts) > 1 and relative.parts[0] in _TREE_ROOTS)
        )
        if flat and tree:
            return ExportLayout.MIXED
    if flat:
        return ExportLayout.FLAT
    if tree:
        return ExportLayout.TREE
    return ExportLayout.UNKNOWN


def _properties(payload: bytes) -> dict[str, str]:
    try:
        root = ET.fromstring(payload)
    except ET.ParseError as error:
        raise ProbeError("Configuration.xml не разбирается как XML") from error
    properties = root.find(
        f"{{{_NS_MDCLASSES}}}Configuration/"
        f"{{{_NS_MDCLASSES}}}Properties"
    )
    if properties is None:
        raise ProbeError("в Configuration.xml нет Properties MDClasses")

    def value(tag: str) -> str:
        node = properties.find(f"{{{_NS_MDCLASSES}}}{tag}")
        return (node.text or "").strip() if node is not None else ""

    return {
        name: value(name)
        for name in (
            "Name",
            "Version",
            "NamePrefix",
            "ObjectBelonging",
            "ConfigurationExtensionPurpose",
            "CompatibilityMode",
        )
    }


def _identity_from_properties(properties: Mapping[str, str]) -> tuple[SourceKind, str]:
    name = _required_text(properties["Name"], "Name в Configuration.xml")
    belonging = properties["ObjectBelonging"]
    purpose = properties["ConfigurationExtensionPurpose"]
    compatibility = properties["CompatibilityMode"]
    strong_extension = bool(belonging) or bool(purpose)
    if strong_extension:
        if (
            belonging
            and purpose
            and properties["NamePrefix"]
            and not compatibility
        ):
            return SourceKind.EXTENSION, name
        raise ProbeError(
            "признаки расширения в Configuration.xml неполны или противоречивы"
        )
    if compatibility:
        return SourceKind.CONFIGURATION, name
    raise ProbeError(
        "нет положительного признака конфигурации или полного набора расширения"
    )


def probe_export(tree: VirtualExportTree) -> CandidateProbe:
    if not isinstance(tree, VirtualExportTree):
        raise ProbeError("tree не реализует VirtualExportTree")
    paths = tree.paths()
    descriptor_path, wrapper = _configuration_path(paths)
    size = tree.size(descriptor_path)
    if size > _MAX_CONFIGURATION_XML_SIZE:
        raise ProbeError("Configuration.xml превышает предел размера 8 МиБ")
    fingerprint = tree.fingerprint()
    _sha256(fingerprint, "snapshot_fingerprint")
    try:
        with tree.open(descriptor_path) as stream:
            payload = stream.read(_MAX_CONFIGURATION_XML_SIZE + 1)
    except (OSError, RuntimeError, ValueError) as error:
        raise ProbeError("Configuration.xml недоступен для чтения") from error
    if len(payload) != size or len(payload) > _MAX_CONFIGURATION_XML_SIZE:
        raise ProbeError("Configuration.xml изменился или превышает предел размера")
    properties = _properties(payload)
    source_kind, internal_name = _identity_from_properties(properties)
    try:
        raw_sha256 = _sha256(tree.source_sha256(), "raw_sha256")
        stable = tree.verify_stable(fingerprint)
    except (OSError, RuntimeError, ValueError) as error:
        raise ProbeError("кандидат изменился или недоступен во время probe") from error
    if not stable:
        raise ProbeError("кандидат изменился во время probe")
    return CandidateProbe(
        source_kind=source_kind,
        internal_name=internal_name,
        configuration_version=properties["Version"],
        layout=_layout(paths, wrapper),
        wrapper=wrapper,
        raw_sha256=raw_sha256,
        snapshot_fingerprint=fingerprint,
        transport=tree.transport,
        origin_name=tree.origin_name,
    )


def group_candidates(
    candidates: Iterable[BoundCandidate],
) -> dict[tuple[str, ...], tuple[str, ...]]:
    grouped: dict[tuple[str, ...], list[str]] = {}
    seen: set[str] = set()
    for candidate in candidates:
        if not isinstance(candidate, BoundCandidate):
            raise ProbeError("grouping принимает только BoundCandidate")
        if candidate.candidate_id in seen:
            raise ProbeError("candidate_id продублирован в grouping")
        seen.add(candidate.candidate_id)
        grouped.setdefault(candidate.grouping_key, []).append(candidate.candidate_id)
    return {
        key: tuple(sorted(ids))
        for key, ids in sorted(grouped.items())
    }


def compare_with_active(
    candidate: BoundCandidate,
    semantic_hashes: Mapping[str, str],
    active: ActiveSnapshot,
    *,
    parser_version: int,
    selection_version: int,
) -> CandidateState:
    if not isinstance(candidate, BoundCandidate) or not isinstance(
        active, ActiveSnapshot
    ):
        raise ProbeError("compare требует BoundCandidate и ActiveSnapshot")
    current_hashes = _semantic_hashes(semantic_hashes)
    parser_version = _positive_int(parser_version, "parser_version")
    selection_version = _positive_int(selection_version, "selection_version")
    if candidate.identity != active.identity:
        return CandidateState.FOREIGN_IDENTITY
    same_semantics = dict(current_hashes) == dict(active.semantic_hashes)
    if same_semantics and (
        active.parser_version < parser_version
        or active.selection_version < selection_version
    ):
        return CandidateState.REPARSE
    if same_semantics:
        if candidate.raw_sha256 == active.raw_sha256:
            return CandidateState.CURRENT
        return CandidateState.DUPLICATE
    return CandidateState.DIFFERENT_SNAPSHOT


__all__ = [
    "ActiveSnapshot",
    "BoundCandidate",
    "CandidateProbe",
    "CandidateState",
    "ExportLayout",
    "ProbeError",
    "compare_with_active",
    "group_candidates",
    "probe_export",
]
