"""Строгий контракт active Source A/B generation.

Модуль намеренно не читает и не переписывает Registry: manifest является
атомарным описанием кандидата, а классификация старого формата сохраняет его
как read-only evidence до явной новой загрузки.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from enum import Enum
from typing import Mapping


_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
_PLATFORM_VERSION = re.compile(r"^[0-9]+\.[0-9]+\.[0-9]+(?:\.[0-9]+)?$")


def _text(value: object, label: str) -> str:
    if not isinstance(value, str) or not value or value.strip() != value:
        raise ValueError(f"{label} должен быть непустой строкой")
    if "\x00" in value:
        raise ValueError(f"{label} содержит недопустимый символ")
    return value


def _sha(value: object, label: str) -> str:
    if not isinstance(value, str) or not _SHA256.fullmatch(value):
        raise ValueError(f"{label} должен быть sha256 в нижнем регистре")
    return value


class ActivationMode(str, Enum):
    A_ONLY = "A_ONLY"
    B_FULL = "B_FULL"


class ActivationStatus(str, Enum):
    ACTIVE = "ACTIVE"
    RELOAD_REQUIRED = "RELOAD_REQUIRED"


@dataclass(frozen=True, slots=True)
class PlatformDeclaration:
    version: str
    source: str = "user"
    status: str = "declared"

    def __post_init__(self) -> None:
        if not _PLATFORM_VERSION.fullmatch(self.version):
            raise ValueError("platform_version должен содержать 3 или 4 числовых компонента")
        if self.source != "user" or self.status != "declared":
            raise ValueError("platform declaration допускает только source=user/status=declared")

    def to_dict(self) -> dict[str, str]:
        return {"version": self.version, "source": self.source, "status": self.status}


@dataclass(frozen=True, slots=True)
class ActivationComponent:
    """Provenance одного payload внутри общего physical root."""

    source: str
    origin: str
    raw_sha256: str
    payload_sha256: str

    def __post_init__(self) -> None:
        _text(self.source, "component.source")
        _text(self.origin, "component.origin")
        _sha(self.raw_sha256, "component.raw_sha256")
        _sha(self.payload_sha256, "component.payload_sha256")

    def to_dict(self) -> dict[str, str]:
        return {
            "source": self.source,
            "origin": self.origin,
            "raw_sha256": self.raw_sha256,
            "payload_sha256": self.payload_sha256,
        }

    @classmethod
    def from_dict(cls, raw: object) -> "ActivationComponent":
        if not isinstance(raw, Mapping):
            raise ValueError("component должен быть объектом")
        try:
            return cls(
                source=raw["source"],
                origin=raw["origin"],
                raw_sha256=raw["raw_sha256"],
                payload_sha256=raw["payload_sha256"],
            )
        except (KeyError, TypeError) as error:
            raise ValueError("component содержит неверные поля") from error


@dataclass(frozen=True, slots=True)
class ActivationManifest:
    """Единая активируемая единица: main и extensions меняются одним switch."""

    mode: ActivationMode
    identity_incarnation: str
    physical_generation_root_id: str
    configuration_version: str
    main: ActivationComponent
    extensions: tuple[ActivationComponent, ...]
    expected_previous_activation: str | None
    transaction_id: str
    recovery_id: str
    schema_version: int = 1

    def __post_init__(self) -> None:
        if not isinstance(self.mode, ActivationMode):
            raise ValueError("mode должен быть A_ONLY или B_FULL")
        if self.schema_version != 1:
            raise ValueError("неподдерживаемая schema_version activation manifest")
        for label, value in (
            ("identity_incarnation", self.identity_incarnation),
            ("physical_generation_root_id", self.physical_generation_root_id),
            ("configuration_version", self.configuration_version),
            ("transaction_id", self.transaction_id),
            ("recovery_id", self.recovery_id),
        ):
            _text(value, label)
        if self.expected_previous_activation is not None:
            _sha(self.expected_previous_activation, "expected_previous_activation")
        if not isinstance(self.main, ActivationComponent):
            raise ValueError("main должен быть ActivationComponent")
        if not isinstance(self.extensions, tuple) or not all(
            isinstance(item, ActivationComponent) for item in self.extensions
        ):
            raise ValueError("extensions должны быть tuple[ActivationComponent, ...]")
        if self.mode is ActivationMode.A_ONLY and self.extensions:
            raise ValueError("A_ONLY не может содержать extensions")
        if self.mode is ActivationMode.A_ONLY and self.main.source != "source-a":
            raise ValueError("A_ONLY должен иметь source-a main component")
        if self.mode is ActivationMode.B_FULL and self.main.source != "source-b":
            raise ValueError("B_FULL должен иметь source-b main component")
        if any(item.source != "source-b" for item in self.extensions):
            raise ValueError("B_FULL extensions должны иметь source-b provenance")

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "mode": self.mode.value,
            "identity_incarnation": self.identity_incarnation,
            "physical_generation_root_id": self.physical_generation_root_id,
            "configuration_version": self.configuration_version,
            "main": self.main.to_dict(),
            "extensions": [item.to_dict() for item in self.extensions],
            "expected_previous_activation": self.expected_previous_activation,
            "transaction_id": self.transaction_id,
            "recovery_id": self.recovery_id,
        }

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n"

    @property
    def sha256(self) -> str:
        return hashlib.sha256(self.to_json().encode("utf-8")).hexdigest()

    @classmethod
    def from_dict(cls, raw: object) -> "ActivationManifest":
        if not isinstance(raw, Mapping):
            raise ValueError("activation manifest должен быть объектом")
        expected = {
            "schema_version", "mode", "identity_incarnation",
            "physical_generation_root_id", "configuration_version", "main",
            "extensions", "expected_previous_activation", "transaction_id",
            "recovery_id",
        }
        if set(raw) != expected:
            raise ValueError("activation manifest содержит неизвестные или отсутствующие поля")
        try:
            mode = ActivationMode(raw["mode"])
        except (KeyError, TypeError, ValueError) as error:
            raise ValueError("mode должен быть A_ONLY или B_FULL") from error
        try:
            extensions = raw["extensions"]
            if not isinstance(extensions, list):
                raise ValueError("extensions должны быть массивом")
            return cls(
                schema_version=raw["schema_version"],
                mode=mode,
                identity_incarnation=raw["identity_incarnation"],
                physical_generation_root_id=raw["physical_generation_root_id"],
                configuration_version=raw["configuration_version"],
                main=ActivationComponent.from_dict(raw["main"]),
                extensions=tuple(ActivationComponent.from_dict(item) for item in extensions),
                expected_previous_activation=raw["expected_previous_activation"],
                transaction_id=raw["transaction_id"],
                recovery_id=raw["recovery_id"],
            )
        except (KeyError, TypeError) as error:
            raise ValueError("activation manifest содержит неверные поля") from error

    @classmethod
    def from_json(cls, raw: str) -> "ActivationManifest":
        if not isinstance(raw, str):
            raise ValueError("activation manifest должен быть JSON-строкой")
        try:
            return cls.from_dict(json.loads(raw))
        except json.JSONDecodeError as error:
            raise ValueError("activation manifest не является корректным JSON") from error


@dataclass(frozen=True, slots=True)
class ActivationClassification:
    status: ActivationStatus
    mode: ActivationMode | None
    raw: object


def classify_activation(raw: object) -> ActivationClassification:
    """Классифицировать старый state без его переписывания или миграции."""

    if not isinstance(raw, Mapping) or "mode" not in raw:
        return ActivationClassification(ActivationStatus.RELOAD_REQUIRED, None, raw)
    try:
        manifest = ActivationManifest.from_dict(raw)
    except ValueError:
        return ActivationClassification(ActivationStatus.RELOAD_REQUIRED, None, raw)
    return ActivationClassification(ActivationStatus.ACTIVE, manifest.mode, raw)


__all__ = [
    "ActivationClassification",
    "ActivationComponent",
    "ActivationManifest",
    "ActivationMode",
    "ActivationStatus",
    "PlatformDeclaration",
    "classify_activation",
]
