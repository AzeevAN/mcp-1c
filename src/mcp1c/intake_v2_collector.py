"""Один потоковый проход source B до предметного converter.

Collector не публикует Registry и не строит поисковые индексы. Он читает
каждый выбранный член ``VirtualExportTree`` один раз и атомарно сохраняет
проверяемый staging-снимок metadata, кода, форм и канонических role XML.
"""

from __future__ import annotations

import errno
import gzip
import hashlib
import json
import os
import re
import shutil
import stat
import tempfile
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from enum import Enum
from pathlib import Path, PurePosixPath
from typing import BinaryIO, Iterable, Mapping

from . import module_address
from .intake_v2 import (
    LayerKind,
    LayerState,
    MetadataKindPolicy,
    MetadataKindSpec,
    VirtualExportTree,
)
from .intake_v2_probe import CandidateProbe, ProbeError
from .intake_v2_transport import TransportError


COLLECTION_FORMAT_VERSION = 1
SELECTION_VERSION = 4
_READ_CHUNK = 1 << 20
_MANIFEST_LIMIT = 64 * 1024 * 1024
_SHA256_RE = re.compile(r"[0-9a-f]{64}\Z")
_MAX_DIAGNOSTIC_EXAMPLES = 3
_STRUCTURE_LAYERS = frozenset(
    {LayerKind.BASE_STRUCTURE, LayerKind.EXTENDED_STRUCTURE}
)


class CollectorError(RuntimeError):
    """Source B нельзя целиком и достоверно собрать в staging."""


class CollectionError(RuntimeError):
    """Сохранённый collection manifest или его payload недостоверен."""


class ArtifactKind(str, Enum):
    METADATA = "metadata"
    CODE = "code"
    FORMS = "forms"


def _required_text(value: object, label: str) -> str:
    if not isinstance(value, str) or not value or value.strip() != value:
        raise CollectionError(f"{label} должен быть непустой строкой")
    if "\x00" in value:
        raise CollectionError(f"{label} содержит недопустимый символ")
    return value


def _sha256(value: object, label: str, *, allow_empty: bool = False) -> str:
    if allow_empty and value == "":
        return ""
    if not isinstance(value, str) or not _SHA256_RE.fullmatch(value):
        raise CollectionError(f"{label} должен быть SHA-256")
    return value


def _nonnegative(value: object, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise CollectionError(f"{label} должен быть неотрицательным целым")
    return value


def _relative_path(value: object, label: str) -> str:
    value = _required_text(value, label)
    if "\\" in value:
        raise CollectionError(f"{label} должен быть POSIX-путём")
    path = PurePosixPath(value)
    if (
        path.is_absolute()
        or not path.parts
        or any(part in {"", ".", ".."} for part in path.parts)
        or path.as_posix() != value
    ):
        raise CollectionError(f"{label} должен быть безопасным относительным путём")
    return value


@dataclass(frozen=True, slots=True)
class CollectionArtifact:
    kind: ArtifactKind
    source_path: str
    relative_path: str
    size: int
    sha256: str
    source_name: str
    address: str = ""

    def __post_init__(self) -> None:
        if not isinstance(self.kind, ArtifactKind):
            raise CollectionError("kind артефакта должен быть ArtifactKind")
        _relative_path(self.source_path, "source_path")
        _relative_path(self.relative_path, "relative_path")
        _nonnegative(self.size, "size")
        _sha256(self.sha256, "sha256")
        _required_text(self.source_name, "source_name")
        if not isinstance(self.address, str):
            raise CollectionError("address должен быть строкой")
        expected = f"{self.kind.value}/{self.source_path}"
        if self.relative_path != expected:
            raise CollectionError("relative_path артефакта не совпадает с его видом")

    def to_dict(self) -> dict[str, object]:
        return {
            "kind": self.kind.value,
            "source_path": self.source_path,
            "relative_path": self.relative_path,
            "size": self.size,
            "sha256": self.sha256,
            "source_name": self.source_name,
            "address": self.address,
        }

    @classmethod
    def from_dict(cls, raw: object) -> CollectionArtifact:
        if not isinstance(raw, dict):
            raise CollectionError("artifact должен быть объектом")
        try:
            return cls(
                kind=ArtifactKind(raw["kind"]),
                source_path=raw["source_path"],
                relative_path=raw["relative_path"],
                size=raw["size"],
                sha256=raw["sha256"],
                source_name=raw["source_name"],
                address=raw.get("address", ""),
            )
        except (KeyError, TypeError, ValueError) as error:
            if isinstance(error, CollectionError):
                raise
            raise CollectionError("artifact содержит неверные поля") from error


@dataclass(frozen=True, slots=True)
class RoleArtifact:
    source_path: str
    relative_path: str
    size: int
    sha256: str

    def __post_init__(self) -> None:
        _relative_path(self.source_path, "role source_path")
        _relative_path(self.relative_path, "role relative_path")
        _nonnegative(self.size, "role size")
        _sha256(self.sha256, "role sha256")
        if self.relative_path != f"roles/payload/{self.source_path}.gz":
            raise CollectionError("role relative_path не совпадает с source_path")

    def to_dict(self) -> dict[str, object]:
        return {
            "source_path": self.source_path,
            "relative_path": self.relative_path,
            "size": self.size,
            "sha256": self.sha256,
        }

    @classmethod
    def from_dict(cls, raw: object) -> RoleArtifact:
        if not isinstance(raw, dict):
            raise CollectionError("role artifact должен быть объектом")
        try:
            return cls(
                source_path=raw["source_path"],
                relative_path=raw["relative_path"],
                size=raw["size"],
                sha256=raw["sha256"],
            )
        except (KeyError, TypeError, ValueError) as error:
            if isinstance(error, CollectionError):
                raise
            raise CollectionError("role artifact содержит неверные поля") from error


@dataclass(frozen=True, slots=True)
class RoleSnapshot:
    state: LayerState
    roles_total: int = 0
    artifacts: tuple[RoleArtifact, ...] = ()
    content_sha256: str = ""
    relative_path: str = ""
    error: str = ""

    def __post_init__(self) -> None:
        if not isinstance(self.state, LayerState):
            raise CollectionError("role state должен быть LayerState")
        _nonnegative(self.roles_total, "roles_total")
        if not isinstance(self.artifacts, tuple) or not all(
            isinstance(item, RoleArtifact) for item in self.artifacts
        ):
            raise CollectionError("role artifacts должен быть tuple[RoleArtifact, ...]")
        ordered = tuple(sorted(self.artifacts, key=lambda item: item.source_path))
        if len({item.source_path for item in ordered}) != len(ordered):
            raise CollectionError("role snapshot дублирует source_path")
        object.__setattr__(self, "artifacts", ordered)
        if self.state is LayerState.READY:
            _sha256(self.content_sha256, "role content_sha256")
            _relative_path(self.relative_path, "role relative_path")
            if self.relative_path != "roles/manifest.json":
                raise CollectionError("role manifest должен иметь канонический путь")
            if self.error:
                raise CollectionError("ready role snapshot не должен иметь error")
            return
        if self.state is not LayerState.ERROR:
            raise CollectionError("collector создаёт только ready или error роли")
        if self.roles_total or self.artifacts or self.content_sha256 or self.relative_path:
            raise CollectionError("error role snapshot не ссылается на payload")
        _required_text(self.error, "role error")

    def to_dict(self) -> dict[str, object]:
        return {
            "state": self.state.value,
            "roles_total": self.roles_total,
            "artifacts": [item.to_dict() for item in self.artifacts],
            "content_sha256": self.content_sha256,
            "relative_path": self.relative_path,
            "error": self.error,
        }

    @classmethod
    def from_dict(cls, raw: object) -> RoleSnapshot:
        if not isinstance(raw, dict):
            raise CollectionError("roles должен быть объектом")
        try:
            artifacts = raw.get("artifacts", [])
            if not isinstance(artifacts, list):
                raise CollectionError("role artifacts должен быть массивом")
            return cls(
                state=LayerState(raw["state"]),
                roles_total=raw.get("roles_total", 0),
                artifacts=tuple(RoleArtifact.from_dict(item) for item in artifacts),
                content_sha256=raw.get("content_sha256", ""),
                relative_path=raw.get("relative_path", ""),
                error=raw.get("error", ""),
            )
        except (KeyError, TypeError, ValueError) as error:
            if isinstance(error, CollectionError):
                raise
            raise CollectionError("roles содержит неверные поля") from error


@dataclass(frozen=True, slots=True)
class CollectionDiagnostic:
    code: str
    signature: str
    count: int
    examples: tuple[str, ...]
    severity: str = "info"

    def __post_init__(self) -> None:
        _required_text(self.code, "diagnostic code")
        _required_text(self.signature, "diagnostic signature")
        if self.count <= 0:
            raise CollectionError("diagnostic count должен быть положительным")
        if self.severity not in {"info", "warning"}:
            raise CollectionError("diagnostic severity должен быть info|warning")
        if not isinstance(self.examples, tuple) or not all(
            isinstance(item, str) and item for item in self.examples
        ):
            raise CollectionError("diagnostic examples должен быть tuple[str, ...]")
        if len(self.examples) > _MAX_DIAGNOSTIC_EXAMPLES:
            raise CollectionError("diagnostic содержит слишком много примеров")

    def to_dict(self) -> dict[str, object]:
        return {
            "code": self.code,
            "signature": self.signature,
            "count": self.count,
            "examples": list(self.examples),
            "severity": self.severity,
        }

    @classmethod
    def from_dict(cls, raw: object) -> CollectionDiagnostic:
        if not isinstance(raw, dict):
            raise CollectionError("diagnostic должен быть объектом")
        try:
            examples = raw["examples"]
            if not isinstance(examples, list):
                raise CollectionError("diagnostic examples должен быть массивом")
            return cls(
                code=raw["code"],
                signature=raw["signature"],
                count=raw["count"],
                examples=tuple(examples),
                severity=raw.get("severity", "info"),
            )
        except (KeyError, TypeError, ValueError) as error:
            if isinstance(error, CollectionError):
                raise
            raise CollectionError("diagnostic содержит неверные поля") from error


@dataclass(frozen=True, slots=True)
class CollectionResult:
    root: Path
    format_version: int
    selection_version: int
    probe: CandidateProbe
    artifacts: tuple[CollectionArtifact, ...]
    roles: RoleSnapshot
    diagnostics: tuple[CollectionDiagnostic, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "root", Path(self.root))
        if self.format_version != COLLECTION_FORMAT_VERSION:
            raise CollectionError("collection format_version несовместим")
        if self.selection_version != SELECTION_VERSION:
            raise CollectionError("collection selection_version несовместим")
        if not isinstance(self.probe, CandidateProbe):
            raise CollectionError("probe должен быть CandidateProbe")
        if not isinstance(self.artifacts, tuple) or not all(
            isinstance(item, CollectionArtifact) for item in self.artifacts
        ):
            raise CollectionError("artifacts должен быть tuple")
        ordered = tuple(
            sorted(self.artifacts, key=lambda item: (item.kind.value, item.source_path))
        )
        keys = {(item.kind, item.source_path) for item in ordered}
        if len(keys) != len(ordered):
            raise CollectionError("collection дублирует артефакт")
        object.__setattr__(self, "artifacts", ordered)
        if not isinstance(self.roles, RoleSnapshot):
            raise CollectionError("roles должен быть RoleSnapshot")
        if not isinstance(self.diagnostics, tuple) or not all(
            isinstance(item, CollectionDiagnostic) for item in self.diagnostics
        ):
            raise CollectionError("diagnostics должен быть tuple")
        object.__setattr__(
            self,
            "diagnostics",
            tuple(sorted(self.diagnostics, key=lambda item: (item.code, item.signature))),
        )

    @property
    def snapshot_fingerprint(self) -> str:
        return self.probe.snapshot_fingerprint

    @property
    def metadata(self) -> tuple[CollectionArtifact, ...]:
        return tuple(item for item in self.artifacts if item.kind is ArtifactKind.METADATA)

    @property
    def code(self) -> tuple[CollectionArtifact, ...]:
        return tuple(item for item in self.artifacts if item.kind is ArtifactKind.CODE)

    @property
    def forms(self) -> tuple[CollectionArtifact, ...]:
        return tuple(item for item in self.artifacts if item.kind is ArtifactKind.FORMS)

    def to_dict(self) -> dict[str, object]:
        return {
            "format_version": self.format_version,
            "selection_version": self.selection_version,
            "probe": self.probe.to_dict(),
            "artifacts": [item.to_dict() for item in self.artifacts],
            "roles": self.roles.to_dict(),
            "diagnostics": [item.to_dict() for item in self.diagnostics],
        }

    @classmethod
    def from_dict(cls, root: Path, raw: object) -> CollectionResult:
        if not isinstance(raw, dict):
            raise CollectionError("collection manifest должен быть объектом")
        try:
            artifacts = raw["artifacts"]
            diagnostics = raw.get("diagnostics", [])
            if not isinstance(artifacts, list) or not isinstance(diagnostics, list):
                raise CollectionError("manifest содержит неверные массивы")
            return cls(
                root=root,
                format_version=raw["format_version"],
                selection_version=raw["selection_version"],
                probe=CandidateProbe.from_dict(raw["probe"]),
                artifacts=tuple(CollectionArtifact.from_dict(item) for item in artifacts),
                roles=RoleSnapshot.from_dict(raw["roles"]),
                diagnostics=tuple(
                    CollectionDiagnostic.from_dict(item) for item in diagnostics
                ),
            )
        except (KeyError, TypeError, ValueError, ProbeError) as error:
            if isinstance(error, CollectionError):
                raise
            raise CollectionError("collection manifest содержит неверные поля") from error


def _supported(
    source_name: str,
    canonical_kind: str,
    aliases: Iterable[str],
    layers: Iterable[LayerKind],
    *,
    base_adapter: str = "",
    extended_adapter: str = "",
) -> MetadataKindSpec:
    aliases = tuple(aliases)
    return MetadataKindSpec(
        source_name=source_name,
        canonical_kind=canonical_kind,
        policy=MetadataKindPolicy.SUPPORTED,
        layers=frozenset(layers),
        layouts=frozenset({"tree", *(("flat",) if aliases else ())}),
        aliases=frozenset(aliases),
        base_adapter=base_adapter,
        extended_adapter=extended_adapter,
    )


def _inactive(
    source_name: str,
    canonical_kind: str,
    policy: MetadataKindPolicy,
    aliases: Iterable[str] = (),
) -> MetadataKindSpec:
    return MetadataKindSpec(
        source_name=source_name,
        canonical_kind=canonical_kind,
        policy=policy,
        aliases=frozenset(aliases),
    )


_BASE_CONTENT = frozenset(
    {LayerKind.BASE_STRUCTURE, LayerKind.CODE, LayerKind.FORMS}
)


def _base(
    source_name: str,
    canonical_kind: str,
    aliases: Iterable[str],
    layers: Iterable[LayerKind] = _BASE_CONTENT,
    *,
    extended_adapter: str = "",
) -> MetadataKindSpec:
    return _supported(
        source_name,
        canonical_kind,
        aliases,
        layers,
        base_adapter="schema_v1",
        extended_adapter=extended_adapter,
    )


# Flat aliases перечислены только для уже доказанной грамматики из
# ``module_address``. Наличие tree-вида само по себе не доказывает имя его
# плоского представления; неизвестный вариант останется диагностируемым.
DEFAULT_KIND_SPECS = (
    _base("Catalogs", "Справочник", ("Catalog",)),
    _base("Documents", "Документ", ("Document",)),
    _base(
        "InformationRegisters",
        "РегистрСведений",
        ("InformationRegister",),
    ),
    _base(
        "AccumulationRegisters",
        "РегистрНакопления",
        ("AccumulationRegister",),
    ),
    _base(
        "AccountingRegisters",
        "РегистрБухгалтерии",
        (),
    ),
    _base(
        "CalculationRegisters",
        "РегистрРасчета",
        (),
    ),
    _base("Constants", "Константа", ("Constant",)),
    _base("Enums", "Перечисление", ("Enum",)),
    _base(
        "ChartsOfCharacteristicTypes",
        "ПланВидовХарактеристик",
        ("ChartOfCharacteristicTypes",),
    ),
    _base(
        "ChartsOfAccounts",
        "ПланСчетов",
        (),
    ),
    _base(
        "ChartsOfCalculationTypes",
        "ПланВидовРасчета",
        (),
    ),
    _base(
        "ExchangePlans",
        "ПланОбмена",
        ("ExchangePlan",),
        (*_BASE_CONTENT, LayerKind.EXTENDED_STRUCTURE),
        extended_adapter="exchange_plan",
    ),
    _base(
        "BusinessProcesses",
        "БизнесПроцесс",
        (),
    ),
    _base("Tasks", "Задача", ()),
    _base(
        "DefinedTypes",
        "ОпределяемыйТип",
        ("DefinedType",),
        (LayerKind.BASE_STRUCTURE,),
    ),
    _base(
        "CommonModules",
        "ОбщийМодуль",
        ("CommonModule",),
        (LayerKind.BASE_STRUCTURE, LayerKind.CODE),
    ),
    _base(
        "EventSubscriptions",
        "ПодпискаНаСобытие",
        ("EventSubscription",),
        (LayerKind.BASE_STRUCTURE, LayerKind.EXTENDED_STRUCTURE),
        extended_adapter="event_subscription",
    ),
    _base(
        "ScheduledJobs",
        "РегламентноеЗадание",
        ("ScheduledJob",),
        (LayerKind.BASE_STRUCTURE, LayerKind.EXTENDED_STRUCTURE),
        extended_adapter="scheduled_job",
    ),
    _base("Reports", "Отчет", ("Report",)),
    _base("DataProcessors", "Обработка", ("DataProcessor",)),
    _supported(
        "DocumentJournals",
        "ЖурналДокументов",
        ("DocumentJournal",),
        (LayerKind.EXTENDED_STRUCTURE, LayerKind.CODE, LayerKind.FORMS),
        extended_adapter="document_journal",
    ),
    _supported(
        "DocumentNumerators",
        "Нумератор",
        ("DocumentNumerator",),
        (LayerKind.BASE_STRUCTURE,),
        base_adapter="numbering_rules",
    ),
    _supported(
        "CommonAttributes",
        "ОбщийРеквизит",
        ("CommonAttribute",),
        (LayerKind.EXTENDED_STRUCTURE,),
        extended_adapter="common_attribute",
    ),
    _supported(
        "SessionParameters",
        "ПараметрСеанса",
        ("SessionParameter",),
        (LayerKind.EXTENDED_STRUCTURE,),
        extended_adapter="session_parameter",
    ),
    _supported(
        "CommonForms",
        "ОбщаяФорма",
        ("CommonForm",),
        (LayerKind.EXTENDED_STRUCTURE, LayerKind.CODE, LayerKind.FORMS),
        extended_adapter="common_form",
    ),
    _supported(
        "Bots",
        "Бот",
        (),
        (LayerKind.EXTENDED_STRUCTURE, LayerKind.CODE),
        extended_adapter="bot",
    ),
    _supported(
        "CommonCommands",
        "ОбщаяКоманда",
        ("CommonCommand",),
        (LayerKind.CODE,),
    ),
    _supported(
        "FilterCriteria",
        "КритерийОтбора",
        ("FilterCriterion",),
        (LayerKind.CODE, LayerKind.FORMS),
    ),
    _supported("WebServices", "WebСервис", ("WebService",), (LayerKind.CODE,)),
    _supported("HTTPServices", "HTTPСервис", ("HTTPService",), (LayerKind.CODE,)),
    _supported(
        "Sequences",
        "Последовательность",
        ("Sequence",),
        (LayerKind.CODE,),
    ),
    _inactive(
        "Subsystems",
        "Подсистема",
        MetadataKindPolicy.DEFERRED,
        ("Subsystem",),
    ),
    _inactive(
        "XDTOPackages",
        "ПакетXDTO",
        MetadataKindPolicy.DEFERRED,
        ("XDTOPackage",),
    ),
    _inactive(
        "WSReferences",
        "WSСсылка",
        MetadataKindPolicy.DEFERRED,
        ("WSReference",),
    ),
    *(
        _inactive(source, canonical, MetadataKindPolicy.IGNORED, aliases)
        for source, canonical, aliases in (
            ("SettingsStorages", "ХранилищеНастроек", ("SettingsStorage",)),
            ("Languages", "Язык", ("Language",)),
            ("Styles", "Стиль", ("Style",)),
            ("StyleItems", "ЭлементСтиля", ("StyleItem",)),
            ("CommonPictures", "ОбщаяКартинка", ("CommonPicture",)),
            ("CommandGroups", "ГруппаКоманд", ("CommandGroup",)),
            (
                "FunctionalOptions",
                "ФункциональнаяОпция",
                ("FunctionalOption",),
            ),
            (
                "FunctionalOptionsParameters",
                "ПараметрФункциональнойОпции",
                ("FunctionalOptionsParameter",),
            ),
            ("CommonTemplates", "ОбщийМакет", ("CommonTemplate",)),
            ("ExternalDataSources", "ВнешнийИсточникДанных", ()),
            ("Interfaces", "Интерфейс", ("Interface",)),
        )
    ),
)


class _Diagnostics:
    def __init__(self) -> None:
        self._items: dict[tuple[str, str, str], list[object]] = {}

    def add(
        self,
        code: str,
        signature: str,
        example: str,
        *,
        severity: str = "info",
    ) -> None:
        key = code, signature, severity
        item = self._items.setdefault(key, [0, []])
        item[0] = int(item[0]) + 1
        examples = item[1]
        assert isinstance(examples, list)
        if example not in examples and len(examples) < _MAX_DIAGNOSTIC_EXAMPLES:
            examples.append(example)

    def freeze(self) -> tuple[CollectionDiagnostic, ...]:
        return tuple(
            CollectionDiagnostic(
                code=code,
                signature=signature,
                count=int(payload[0]),
                examples=tuple(payload[1]),
                severity=severity,
            )
            for (code, signature, severity), payload in sorted(self._items.items())
        )


class _CanonicalWriter:
    def __init__(self, output: gzip.GzipFile):
        self.output = output
        self.digest = hashlib.sha256()
        self.size = 0

    def write(self, value: str) -> int:
        payload = value.encode("utf-8")
        self.output.write(payload)
        self.digest.update(payload)
        self.size += len(payload)
        return len(value)


def _kind_maps(
    specs: Iterable[MetadataKindSpec],
) -> tuple[dict[str, MetadataKindSpec], dict[str, MetadataKindSpec]]:
    tree: dict[str, MetadataKindSpec] = {}
    flat: dict[str, MetadataKindSpec] = {}
    for spec in specs:
        if not isinstance(spec, MetadataKindSpec):
            raise CollectorError("kind_specs содержит не MetadataKindSpec")
        if spec.source_name in tree:
            raise CollectorError("kind_specs дублирует source_name")
        tree[spec.source_name] = spec
        for alias in spec.aliases:
            if alias in flat:
                raise CollectorError("kind_specs дублирует flat alias")
            flat[alias] = spec
    return tree, flat


def _without_wrapper(path: str, wrapper: str) -> str:
    parts = PurePosixPath(path).parts
    if not wrapper:
        return path
    if len(parts) < 2 or parts[0] != wrapper:
        raise CollectorError("virtual tree не соответствует wrapper из probe")
    return PurePosixPath(*parts[1:]).as_posix()


def _spec_for(
    path: str,
    tree_specs: Mapping[str, MetadataKindSpec],
    flat_specs: Mapping[str, MetadataKindSpec],
) -> MetadataKindSpec | None:
    parts = PurePosixPath(path).parts
    if len(parts) > 1:
        return tree_specs.get(parts[0])
    flat_kind = parts[0].split(".", 1)[0]
    return flat_specs.get(flat_kind)


def _flat_metadata_path(
    path: str,
    spec: MetadataKindSpec,
) -> str | None:
    """Вернуть только доказанный descriptor или обязательный supplement."""
    if "/" in path:
        return None
    parts = path.split(".")
    if (
        len(parts) == 3
        and parts[0] in spec.aliases
        and bool(parts[1])
        and parts[2] == "xml"
    ):
        return path
    if (
        spec.source_name == "ExchangePlans"
        and len(parts) == 4
        and parts[0] == "ExchangePlan"
        and bool(parts[1])
        and parts[2:] == ["Content", "xml"]
    ):
        return f"ExchangePlans/{parts[1]}/Ext/Content.xml"
    return None


def _flat_code_address(path: str) -> str | None:
    if "/" in path or not (path.endswith(".txt") or path.endswith(".Module")):
        return None
    parsed = module_address.разобрать_плоское_имя(path)
    return parsed.address


def _code_address(path: str) -> str | None:
    if path.endswith(".bsl"):
        return module_address.адрес_модуля(path)
    return _flat_code_address(path)


def _form_address(path: str, spec: MetadataKindSpec) -> str | None:
    parts = PurePosixPath(path).parts
    if len(parts) == 1 and path.endswith(".Form"):
        parsed = module_address.разобрать_плоское_имя(path)
        if not parsed.is_form or parsed.representation != "container":
            raise ValueError("плоский контейнер не является формой")
        return parsed.address
    if spec.source_name == "CommonForms":
        if len(parts) == 2 and PurePosixPath(parts[1]).suffix == ".xml":
            return f"{spec.canonical_kind}.{PurePosixPath(parts[1]).stem}"
        if (
            len(parts) == 4
            and parts[2] == "Ext"
            and parts[3] in {"Form.xml", "Form.bin"}
        ):
            return f"{spec.canonical_kind}.{parts[1]}"
        return None
    if (
        len(parts) == 4
        and parts[2] == "Forms"
        and PurePosixPath(parts[3]).suffix == ".xml"
    ):
        return (
            f"{spec.canonical_kind}.{parts[1]}.Форма."
            f"{PurePosixPath(parts[3]).stem}"
        )
    if (
        len(parts) == 6
        and parts[2] == "Forms"
        and parts[4] == "Ext"
        and parts[5] in {"Form.xml", "Form.bin"}
    ):
        return f"{spec.canonical_kind}.{parts[1]}.Форма.{parts[3]}"
    return None


def _looks_like_code(path: str) -> bool:
    return path.endswith((".bsl", ".txt", ".Module"))


def _looks_like_form(path: str) -> bool:
    return path.endswith(".Form") or PurePosixPath(path).name in {
        "Form.xml",
        "Form.bin",
    }


def _unknown_signature(path: str) -> str:
    parts = PurePosixPath(path).parts
    root = parts[0].split(".", 1)[0]
    return root


def _copy_artifact(
    tree: VirtualExportTree,
    raw_path: str,
    source_path: str,
    root: Path,
    kind: ArtifactKind,
    source_name: str,
    address: str = "",
) -> CollectionArtifact:
    expected_size = tree.size(raw_path)
    relative_path = f"{kind.value}/{source_path}"
    target = root.joinpath(*PurePosixPath(relative_path).parts)
    target.parent.mkdir(parents=True, exist_ok=True)
    digest = hashlib.sha256()
    total = 0
    with tree.open(raw_path) as source, target.open("xb") as output:
        while True:
            block = source.read(_READ_CHUNK)
            if not block:
                break
            if not isinstance(block, bytes):
                raise CollectorError("virtual tree должен возвращать bytes")
            total += len(block)
            if total > expected_size:
                raise CollectorError("член source B превысил объявленный размер")
            output.write(block)
            digest.update(block)
    if total != expected_size:
        raise CollectorError("член source B изменился или прочитан не полностью")
    return CollectionArtifact(
        kind=kind,
        source_path=source_path,
        relative_path=relative_path,
        size=total,
        sha256=digest.hexdigest(),
        source_name=source_name,
        address=address,
    )


def _role_path_kind(path: str) -> tuple[str, str, str] | None:
    parts = PurePosixPath(path).parts
    if len(parts) == 2 and parts[0] == "Roles" and parts[1].endswith(".xml"):
        name = parts[1][:-4]
        return ("descriptor", name, path) if name else None
    if len(parts) == 4 and parts[0] == "Roles" and parts[2:] == (
        "Ext",
        "Rights.xml",
    ):
        return ("rights", parts[1], path) if parts[1] else None
    if len(parts) == 1 and path.startswith("Role."):
        if path.endswith(".Rights.xml"):
            name = path[len("Role.") : -len(".Rights.xml")]
            if name:
                return ("rights", name, f"Roles/{name}/Ext/Rights.xml")
        elif path.endswith(".xml"):
            name = path[len("Role.") : -len(".xml")]
            if name:
                return ("descriptor", name, f"Roles/{name}.xml")
    return None


def _is_schedule_payload(path: str) -> bool:
    if path.endswith("/Ext/Schedule.xml"):
        return True
    return (
        "/" not in path
        and path.startswith("ScheduledJob.")
        and path.endswith(".Schedule.xml")
    )


def _canonicalize_role(
    tree: VirtualExportTree,
    raw_path: str,
    source_path: str,
    root: Path,
) -> RoleArtifact:
    relative_path = f"roles/payload/{source_path}.gz"
    target = root.joinpath(*PurePosixPath(relative_path).parts)
    target.parent.mkdir(parents=True, exist_ok=True)
    try:
        with tree.open(raw_path) as source, target.open("xb") as raw_output:
            with gzip.GzipFile(
                filename="",
                mode="wb",
                fileobj=raw_output,
                mtime=0,
            ) as compressed:
                writer = _CanonicalWriter(compressed)
                ET.canonicalize(
                    from_file=source,
                    out=writer,
                    with_comments=False,
                    # ElementTree 3.12 при rewrite_prefixes=True добавляет
                    # xmlns:n="" для обычных атрибутов (например version),
                    # а затем сам же не может прочитать полученный XML.
                    rewrite_prefixes=False,
                )
    except ET.ParseError:
        target.unlink(missing_ok=True)
        raise
    return RoleArtifact(
        source_path=source_path,
        relative_path=relative_path,
        size=writer.size,
        sha256=writer.digest.hexdigest(),
    )


def _content_sha256(prefix: bytes, artifacts: Iterable[object]) -> str:
    digest = hashlib.sha256(prefix)
    for artifact in artifacts:
        source_path = getattr(artifact, "source_path")
        content_sha = getattr(artifact, "sha256")
        for value in (source_path, content_sha):
            encoded = value.encode("utf-8")
            digest.update(len(encoded).to_bytes(8, "big"))
            digest.update(encoded)
    return digest.hexdigest()


def _write_json(path: Path, value: object) -> None:
    payload = (
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        + "\n"
    ).encode("utf-8")
    with path.open("xb") as output:
        output.write(payload)
        output.flush()
        os.fsync(output.fileno())


def _sync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _ready_roles(
    root: Path,
    artifacts: list[RoleArtifact],
    descriptors: set[str],
    rights: set[str],
) -> RoleSnapshot:
    missing_descriptors = sorted(rights - descriptors)
    if missing_descriptors:
        raise ValueError(
            "неполные пары role XML; нет descriptor: "
            + ", ".join(missing_descriptors[:3])
        )
    ordered = tuple(sorted(artifacts, key=lambda item: item.source_path))
    snapshot = RoleSnapshot(
        state=LayerState.READY,
        roles_total=len(descriptors),
        artifacts=ordered,
        content_sha256=_content_sha256(b"mcp1c-role-snapshot-v1\0", ordered),
        relative_path="roles/manifest.json",
    )
    manifest_path = root / snapshot.relative_path
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    _write_json(manifest_path, snapshot.to_dict())
    return snapshot


def _role_error(root: Path, errors: list[tuple[str, str]]) -> RoleSnapshot:
    roles_root = root / "roles"
    if roles_root.exists():
        shutil.rmtree(roles_root)
    first_path, first_reason = errors[0]
    suffix = f"; ещё ошибок: {len(errors) - 1}" if len(errors) > 1 else ""
    return RoleSnapshot(
        state=LayerState.ERROR,
        error=f"{first_path}: {first_reason}{suffix}",
    )


def _target_exists(path: Path) -> bool:
    return path.is_symlink() or path.exists()


def collect_source_b(
    tree: VirtualExportTree,
    probe: CandidateProbe,
    target: Path,
    *,
    kind_specs: Iterable[MetadataKindSpec] = DEFAULT_KIND_SPECS,
) -> CollectionResult:
    """Собрать один атомарный staging без публикации рабочего Registry."""
    if not isinstance(tree, VirtualExportTree):
        raise CollectorError("tree не реализует VirtualExportTree")
    if not isinstance(probe, CandidateProbe):
        raise CollectorError("probe должен быть CandidateProbe")
    fingerprint = tree.fingerprint()
    if fingerprint != probe.snapshot_fingerprint:
        raise CollectorError("probe относится к другому снимку virtual tree")
    if tree.transport is not probe.transport or tree.origin_name != probe.origin_name:
        raise CollectorError("provenance virtual tree не совпадает с probe")
    tree_specs, flat_specs = _kind_maps(kind_specs)
    target = Path(target)
    if _target_exists(target):
        raise CollectorError("target collection уже существует")
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.parent.is_symlink() or not target.parent.is_dir():
        raise CollectorError("родитель target должен быть обычным каталогом")
    temporary = Path(
        tempfile.mkdtemp(
            dir=target.parent,
            prefix=f".{target.name}.",
            suffix=".tmp",
        )
    )
    artifacts: list[CollectionArtifact] = []
    role_artifacts: list[RoleArtifact] = []
    role_descriptors: set[str] = set()
    role_rights: set[str] = set()
    role_errors: list[tuple[str, str]] = []
    diagnostics = _Diagnostics()
    try:
        for raw_path in tree.paths():
            source_path = _without_wrapper(raw_path, probe.wrapper)
            if source_path == "Configuration.xml":
                artifacts.append(
                    _copy_artifact(
                        tree,
                        raw_path,
                        source_path,
                        temporary,
                        ArtifactKind.METADATA,
                        "Configuration",
                    )
                )
                continue
            role_path = _role_path_kind(source_path)
            if role_path is not None:
                role_kind, role_name, canonical_role_path = role_path
                if role_kind == "descriptor":
                    role_descriptors.add(role_name)
                else:
                    role_rights.add(role_name)
                try:
                    role_artifacts.append(
                        _canonicalize_role(
                            tree,
                            raw_path,
                            canonical_role_path,
                            temporary,
                        )
                    )
                except ET.ParseError as error:
                    role_errors.append((source_path, f"XML не разбирается: {error}"))
                continue
            if source_path.startswith("Roles/"):
                diagnostics.add(
                    "unsupported_layout",
                    "Roles",
                    source_path,
                )
                continue
            if _is_schedule_payload(source_path):
                continue
            if (
                source_path.startswith("__MACOSX/")
                or PurePosixPath(source_path).name.startswith("._")
                or PurePosixPath(source_path).name == ".DS_Store"
            ):
                continue

            configuration_code = source_path.startswith("Ext/") or (
                "/" not in source_path and source_path.startswith("Configuration.")
            )
            if configuration_code:
                try:
                    address = _code_address(source_path)
                except ValueError:
                    address = None
                if address is not None:
                    artifacts.append(
                        _copy_artifact(
                            tree,
                            raw_path,
                            source_path,
                            temporary,
                            ArtifactKind.CODE,
                            "Configuration",
                            address,
                        )
                    )
                elif _looks_like_code(source_path):
                    diagnostics.add(
                        "unsupported_layout",
                        "Configuration",
                        source_path,
                    )
                continue

            spec = _spec_for(source_path, tree_specs, flat_specs)
            if spec is None:
                if source_path.endswith(".xml"):
                    diagnostics.add(
                        "unsupported_metadata",
                        _unknown_signature(source_path),
                        source_path,
                    )
                elif _looks_like_code(source_path) or _looks_like_form(source_path):
                    diagnostics.add(
                        "unsupported_layout",
                        _unknown_signature(source_path),
                        source_path,
                    )
                continue
            if spec.policy is not MetadataKindPolicy.SUPPORTED:
                continue

            try:
                code_address = _code_address(source_path)
            except ValueError:
                code_address = None
            if code_address is not None:
                if spec.supports(LayerKind.CODE):
                    artifacts.append(
                        _copy_artifact(
                            tree,
                            raw_path,
                            source_path,
                            temporary,
                            ArtifactKind.CODE,
                            spec.source_name,
                            code_address,
                        )
                    )
                else:
                    diagnostics.add(
                        "unsupported_layout",
                        spec.source_name,
                        source_path,
                    )
                continue

            try:
                form_address = _form_address(source_path, spec)
            except ValueError:
                form_address = None
            if form_address is not None:
                if spec.supports(LayerKind.FORMS):
                    artifacts.append(
                        _copy_artifact(
                            tree,
                            raw_path,
                            source_path,
                            temporary,
                            ArtifactKind.FORMS,
                            spec.source_name,
                            form_address,
                        )
                    )
                else:
                    diagnostics.add(
                        "unsupported_layout",
                        spec.source_name,
                        source_path,
                    )
                continue

            metadata_source_path = source_path
            if "/" not in source_path and source_path.endswith(".xml"):
                normalized = _flat_metadata_path(source_path, spec)
                if normalized is None:
                    continue
                metadata_source_path = normalized
            if source_path.endswith(".xml") and spec.layers & _STRUCTURE_LAYERS:
                artifacts.append(
                    _copy_artifact(
                        tree,
                        raw_path,
                        metadata_source_path,
                        temporary,
                        ArtifactKind.METADATA,
                        spec.source_name,
                    )
                )
            elif _looks_like_code(source_path) or _looks_like_form(source_path):
                diagnostics.add(
                    "unsupported_layout",
                    spec.source_name,
                    source_path,
                )

        if role_errors:
            for path, _reason in role_errors:
                diagnostics.add(
                    "role_parse_error",
                    "role_xml",
                    path,
                    severity="warning",
                )
            roles = _role_error(temporary, role_errors)
        else:
            try:
                roles = _ready_roles(
                    temporary,
                    role_artifacts,
                    role_descriptors,
                    role_rights,
                )
            except ValueError as error:
                diagnostics.add(
                    "role_pair_error",
                    "role_pair",
                    "Roles",
                    severity="warning",
                )
                roles = _role_error(temporary, [("Roles", str(error))])

        if not tree.verify_stable(fingerprint):
            raise CollectorError("source B изменился во время collection")
        result = CollectionResult(
            root=temporary,
            format_version=COLLECTION_FORMAT_VERSION,
            selection_version=SELECTION_VERSION,
            probe=probe,
            artifacts=tuple(artifacts),
            roles=roles,
            diagnostics=diagnostics.freeze(),
        )
        _write_json(temporary / "collection.json", result.to_dict())
        _sync_directory(temporary)
        if _target_exists(target):
            raise CollectorError("target collection появился во время сборки")
        os.rename(temporary, target)
        _sync_directory(target.parent)
        temporary = Path()
        return CollectionResult.from_dict(target, result.to_dict())
    except CollectorError:
        raise
    except TransportError as error:
        message = str(error).strip() or "source-B transport недоступен"
        raise CollectorError(message[:2048]) from error
    except Exception as error:
        raise CollectorError("не удалось собрать source-B collection") from error
    finally:
        if temporary != Path() and temporary.exists():
            shutil.rmtree(temporary, ignore_errors=True)


def open_collection_member(root: Path, relative_path: str) -> BinaryIO:
    """Открыть payload через dirfd, не следуя ни по одному symlink пути."""
    relative_path = _relative_path(relative_path, "relative_path")
    try:
        before = root.lstat()
    except OSError as error:
        raise CollectionError("collection root недоступен") from error
    if stat.S_ISLNK(before.st_mode) or not stat.S_ISDIR(before.st_mode):
        raise CollectionError("collection root должен быть обычным каталогом")
    directory_flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(
        os, "O_NOFOLLOW", 0
    )
    file_flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    opened: list[int] = []
    file_descriptor: int | None = None
    try:
        current = os.open(root, directory_flags)
        opened.append(current)
        parts = PurePosixPath(relative_path).parts
        for part in parts[:-1]:
            current = os.open(part, directory_flags, dir_fd=current)
            opened.append(current)
        file_descriptor = os.open(parts[-1], file_flags, dir_fd=current)
        info = os.fstat(file_descriptor)
        if not stat.S_ISREG(info.st_mode):
            raise CollectionError("collection payload должен быть обычным файлом")
        stream = os.fdopen(file_descriptor, "rb")
        file_descriptor = None
        return stream
    except CollectionError:
        raise
    except OSError as error:
        message = "collection payload не является обычным файлом"
        if error.errno == errno.ELOOP:
            message = "collection payload содержит символическую ссылку"
        raise CollectionError(message) from error
    finally:
        if file_descriptor is not None:
            os.close(file_descriptor)
        for descriptor in reversed(opened):
            os.close(descriptor)


def _read_json_member(root: Path, relative_path: str) -> object:
    try:
        with open_collection_member(root, relative_path) as source:
            payload = source.read(_MANIFEST_LIMIT + 1)
    except CollectionError:
        raise
    except OSError as error:
        raise CollectionError("collection manifest недоступен") from error
    if len(payload) > _MANIFEST_LIMIT:
        raise CollectionError("collection manifest превышает предел")
    try:
        return json.loads(payload.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError) as error:
        raise CollectionError("collection manifest повреждён") from error


def _verify_artifact(root: Path, artifact: CollectionArtifact) -> None:
    digest = hashlib.sha256()
    total = 0
    with open_collection_member(root, artifact.relative_path) as source:
        for block in iter(lambda: source.read(_READ_CHUNK), b""):
            total += len(block)
            if total > artifact.size:
                raise CollectionError("collection payload изменил размер")
            digest.update(block)
    if total != artifact.size or digest.hexdigest() != artifact.sha256:
        raise CollectionError("collection payload не совпал по размеру или хешу")


def _read_role_artifact(
    root: Path,
    artifact: RoleArtifact,
    *,
    return_payload: bool,
) -> bytes:
    digest = hashlib.sha256()
    total = 0
    chunks: list[bytes] = []
    try:
        with open_collection_member(root, artifact.relative_path) as raw:
            with gzip.GzipFile(fileobj=raw, mode="rb") as source:
                while True:
                    block = source.read(min(_READ_CHUNK, artifact.size - total + 1))
                    if not block:
                        break
                    total += len(block)
                    if total > artifact.size:
                        raise CollectionError("role payload изменил размер")
                    digest.update(block)
                    if return_payload:
                        chunks.append(block)
    except CollectionError:
        raise
    except (OSError, EOFError) as error:
        raise CollectionError("role payload повреждён") from error
    if total != artifact.size or digest.hexdigest() != artifact.sha256:
        raise CollectionError("role payload не совпал по размеру или хешу")
    return b"".join(chunks)


def load_collection(root: Path) -> CollectionResult:
    """После рестарта проверить manifest и каждый сохранённый payload."""
    root = Path(root)
    raw = _read_json_member(root, "collection.json")
    result = CollectionResult.from_dict(root, raw)
    for artifact in result.artifacts:
        _verify_artifact(root, artifact)
    if result.roles.state is LayerState.READY:
        role_manifest = _read_json_member(root, result.roles.relative_path)
        if role_manifest != result.roles.to_dict():
            raise CollectionError("role manifest не совпал с collection manifest")
        for artifact in result.roles.artifacts:
            _read_role_artifact(root, artifact, return_payload=False)
    return result


def read_role_member(result: CollectionResult, source_path: str) -> bytes:
    """Прочитать один проверенный XML из канонического role snapshot."""
    if not isinstance(result, CollectionResult):
        raise CollectionError("result должен быть CollectionResult")
    if result.roles.state is not LayerState.READY:
        raise CollectionError("role snapshot не готов")
    source_path = _relative_path(source_path, "role source_path")
    artifact = next(
        (item for item in result.roles.artifacts if item.source_path == source_path),
        None,
    )
    if artifact is None:
        raise KeyError(source_path)
    return _read_role_artifact(result.root, artifact, return_payload=True)


__all__ = [
    "ArtifactKind",
    "COLLECTION_FORMAT_VERSION",
    "CollectionArtifact",
    "CollectionDiagnostic",
    "CollectionError",
    "CollectionResult",
    "CollectorError",
    "DEFAULT_KIND_SPECS",
    "RoleArtifact",
    "RoleSnapshot",
    "SELECTION_VERSION",
    "collect_source_b",
    "load_collection",
    "open_collection_member",
    "read_role_member",
]
