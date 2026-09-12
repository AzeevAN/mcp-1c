"""Общий JSON-совместимый результат операций Forms без blanket-valid."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal, Mapping, TypeAlias


DiagnosticLevel: TypeAlias = Literal[
    "xml_parse",
    "structural",
    "configuration_links",
    "bsl_static",
    "platform_import",
    "runtime_visual",
]
CheckStatus: TypeAlias = Literal[
    "passed",
    "failed",
    "warning",
    "not_checked",
    "unsupported",
]
ResultStatus: TypeAlias = Literal[
    "checked",
    "compiled",
    "decompiled",
    "rejected",
]

_DIAGNOSTIC_LEVELS = frozenset(
    {
        "xml_parse",
        "structural",
        "configuration_links",
        "bsl_static",
        "platform_import",
        "runtime_visual",
    }
)
_CHECK_STATUSES = frozenset(
    {"passed", "failed", "warning", "not_checked", "unsupported"}
)
_RESULT_STATUSES = frozenset({"checked", "compiled", "decompiled", "rejected"})


@dataclass(frozen=True, slots=True)
class Artifact:
    """Один текстовый артефакт, возвращаемый вызывающему агенту."""

    path: str
    media_type: str
    encoding: str
    content: str

    def __post_init__(self) -> None:
        for name in ("path", "media_type", "encoding"):
            value = getattr(self, name)
            if not isinstance(value, str) or not value:
                raise ValueError(f"artifact.{name} должен быть непустой строкой")
        if not isinstance(self.content, str):
            raise ValueError("artifact.content должен быть строкой")

    def to_dict(self) -> dict[str, str]:
        return {
            "path": self.path,
            "media_type": self.media_type,
            "encoding": self.encoding,
            "content": self.content,
        }


@dataclass(frozen=True, slots=True)
class Diagnostic:
    """Одно утверждение с точным уровнем доказательства и адресом."""

    level: DiagnosticLevel
    status: CheckStatus
    code: str
    path: str
    message: str

    def __post_init__(self) -> None:
        if self.level not in _DIAGNOSTIC_LEVELS:
            raise ValueError(f"неизвестный diagnostic level: {self.level}")
        if self.status not in _CHECK_STATUSES:
            raise ValueError(f"неизвестный diagnostic status: {self.status}")
        for name in ("code", "path", "message"):
            value = getattr(self, name)
            if not isinstance(value, str) or not value:
                raise ValueError(f"diagnostic.{name} должен быть непустой строкой")

    def to_dict(self) -> dict[str, str]:
        return {
            "level": self.level,
            "status": self.status,
            "code": self.code,
            "path": self.path,
            "message": self.message,
        }


@dataclass(frozen=True, slots=True)
class Coverage:
    """Раздельное состояние каждого контура, включая внешнюю приёмку."""

    xml_parse: CheckStatus = "not_checked"
    structural: CheckStatus = "not_checked"
    configuration_links: CheckStatus = "not_checked"
    bsl_static: CheckStatus = "not_checked"
    platform_import: CheckStatus = "not_checked"
    runtime_visual: CheckStatus = "not_checked"

    def __post_init__(self) -> None:
        for name in (
            "xml_parse",
            "structural",
            "configuration_links",
            "bsl_static",
            "platform_import",
            "runtime_visual",
        ):
            value = getattr(self, name)
            if value not in _CHECK_STATUSES:
                raise ValueError(f"coverage.{name}: неизвестный статус {value}")

    def to_dict(self) -> dict[str, str]:
        return {
            "xml_parse": self.xml_parse,
            "structural": self.structural,
            "configuration_links": self.configuration_links,
            "bsl_static": self.bsl_static,
            "platform_import": self.platform_import,
            "runtime_visual": self.runtime_visual,
        }


@dataclass(frozen=True, slots=True)
class FormsResult:
    """Стабильный envelope compiler/decompiler/check."""

    status: ResultStatus
    artifacts: tuple[Artifact, ...] = ()
    specification: Mapping[str, object] | None = None
    diagnostics: tuple[Diagnostic, ...] = ()
    coverage: Coverage = field(default_factory=Coverage)
    instructions: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if self.status not in _RESULT_STATUSES:
            raise ValueError(f"неизвестный result status: {self.status}")
        if not all(isinstance(item, Artifact) for item in self.artifacts):
            raise ValueError("artifacts должен содержать только Artifact")
        if self.specification is not None and not isinstance(
            self.specification, Mapping
        ):
            raise ValueError("specification должен быть mapping или None")
        if not all(isinstance(item, Diagnostic) for item in self.diagnostics):
            raise ValueError("diagnostics должен содержать только Diagnostic")
        if not isinstance(self.coverage, Coverage):
            raise ValueError("coverage должен быть Coverage")
        if not all(isinstance(item, str) and item for item in self.instructions):
            raise ValueError("instructions должен содержать непустые строки")

    def to_dict(self) -> dict[str, object]:
        return {
            "status": self.status,
            "artifacts": [item.to_dict() for item in self.artifacts],
            "specification": (
                dict(self.specification) if self.specification is not None else None
            ),
            "diagnostics": [item.to_dict() for item in self.diagnostics],
            "coverage": self.coverage.to_dict(),
            "instructions": list(self.instructions),
        }


__all__ = [
    "Artifact",
    "CheckStatus",
    "Coverage",
    "Diagnostic",
    "DiagnosticLevel",
    "FormsResult",
    "ResultStatus",
]
