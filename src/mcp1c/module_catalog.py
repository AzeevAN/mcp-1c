"""Единый снимок модулей и доказательств существования форм.

Перечисление диска выполняется ровно здесь. Индексы получают уже готовые
канонические адреса и локаторы, поэтому физические раскладки XML/flat не
могут разойтись между оглавлением, вызовами, формами и поиском.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Callable, Mapping

from .module_address import (
    FlatNameError,
    адрес_модуля,
    адрес_скомпилированного_модуля,
    ключ_адреса,
    разобрать_плоское_имя,
)
from .module_content import (
    ContentReadError,
    LocatorIdentity,
    ModuleLocator,
    read_bsl,
)
from .structure_origin import CATALOG_FILE


_CATEGORIES = (
    "indexed",
    "empty",
    "missing_body",
    "compiled",
    "unknown_address",
    "broken_container",
    "unreadable_body",
    "budget_exceeded",
    "conflict",
)


@dataclass(frozen=True, slots=True)
class CandidateOutcome:
    ordinal: int
    category: str
    address: str | None
    reason: str = ""


@dataclass(frozen=True, slots=True)
class CatalogProblem:
    category: str
    address: str | None
    ordinal: int
    reason: str


@dataclass(frozen=True, slots=True)
class FormSource:
    kind: str
    locator: ModuleLocator


@dataclass(frozen=True, slots=True)
class CatalogEntry:
    address: str
    module_kind: str
    locator: ModuleLocator | None
    is_form: bool
    compiled: bool
    form_sources: tuple[FormSource, ...]
    diagnostics: tuple[str, ...]
    conflict: bool
    address_collision: bool
    sort_key: tuple[str, str]

    @property
    def form_evidence(self) -> tuple[str, ...]:
        return tuple(sorted({source.kind for source in self.form_sources}))


@dataclass(frozen=True, slots=True)
class CatalogCoverage:
    total_candidates: int
    indexed: int = 0
    empty: int = 0
    missing_body: int = 0
    compiled: int = 0
    unknown_address: int = 0
    broken_container: int = 0
    unreadable_body: int = 0
    budget_exceeded: int = 0
    conflict: int = 0

    def as_dict(self) -> dict[str, int]:
        return {name: getattr(self, name) for name in _CATEGORIES}


@dataclass(frozen=True, slots=True)
class ModuleCatalog:
    identity: LocatorIdentity
    entries: Mapping[str, CatalogEntry]
    outcomes: tuple[CandidateOutcome, ...]
    problems: tuple[CatalogProblem, ...]
    coverage: CatalogCoverage
    problem_counts: tuple[tuple[str, int], ...] = ()
    object_problems: Mapping[str, tuple[CatalogProblem, ...]] | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "entries", MappingProxyType(dict(self.entries)))
        if not self.problem_counts:
            counts: dict[str, int] = {}
            for problem in self.problems:
                counts[problem.category] = counts.get(problem.category, 0) + 1
            object.__setattr__(self, "problem_counts", tuple(sorted(counts.items())))
        grouped = self.object_problems
        if grouped is None:
            mutable: dict[str, list[CatalogProblem]] = {}
            for problem in self.problems:
                if problem.address is not None:
                    mutable.setdefault(problem.address, []).append(problem)
            grouped = {key: tuple(value) for key, value in mutable.items()}
        object.__setattr__(
            self,
            "object_problems",
            MappingProxyType(dict(grouped)),
        )

    def to_state(self) -> dict:
        """Состояние без тел и физических корней для cache-roundtrip."""
        return {
            "identity": self.identity.to_state(),
            "entries": [
                (
                    entry.address,
                    entry.module_kind,
                    entry.locator.to_state() if entry.locator else None,
                    entry.is_form,
                    entry.compiled,
                    [
                        (source.kind, source.locator.to_state())
                        for source in entry.form_sources
                    ],
                    entry.diagnostics,
                    entry.conflict,
                    entry.address_collision,
                )
                for entry in self.entries.values()
            ],
            "outcomes": [
                (
                    outcome.ordinal,
                    outcome.category,
                    outcome.address,
                    outcome.reason,
                )
                for outcome in self.outcomes
            ],
            "problems": [
                (
                    problem.category,
                    problem.address,
                    problem.ordinal,
                    problem.reason,
                )
                for problem in sorted(
                    self.problems,
                    key=lambda item: (
                        item.address is None,
                        item.address.casefold() if item.address else "",
                        item.address or "",
                        item.category,
                        item.ordinal,
                        item.reason,
                    ),
                )[:20]
            ],
            "problem_counts": list(self.problem_counts),
            "object_problems": [
                (
                    problem.category,
                    problem.address,
                    problem.ordinal,
                    problem.reason,
                )
                for address in sorted(
                    self.object_problems or {}, key=lambda item: (item.casefold(), item)
                )
                for problem in (self.object_problems or {})[address]
            ],
            "coverage": (
                self.coverage.total_candidates,
                *(getattr(self.coverage, category) for category in _CATEGORIES),
            ),
        }

    @classmethod
    def from_state(
        cls, state: object, expected: LocatorIdentity
    ) -> "ModuleCatalog | None":
        """Восстановить только снимок ожидаемого поколения.

        Любое расхождение формы или identity — промах расходного кэша, а не
        исключение startup. Проверка намеренно строгая: ``bool`` не является
        счётчиком, несмотря на наследование от ``int`` в Python.
        """
        try:
            if not isinstance(state, dict):
                return None
            identity = LocatorIdentity.from_state(state["identity"])
            if identity != expected:
                return None

            raw_entries = state["entries"]
            raw_outcomes = state["outcomes"]
            raw_problems = state["problems"]
            raw_problem_counts = state["problem_counts"]
            raw_object_problems = state["object_problems"]
            raw_coverage = state["coverage"]
            if not isinstance(raw_entries, list):
                return None
            if not isinstance(raw_outcomes, list):
                return None
            if not isinstance(raw_problems, list):
                return None
            if not isinstance(raw_problem_counts, list):
                return None
            if not isinstance(raw_object_problems, list):
                return None
            if not isinstance(raw_coverage, tuple) or len(raw_coverage) != 10:
                return None
            if not all(type(value) is int and value >= 0 for value in raw_coverage):
                return None

            entries: dict[str, CatalogEntry] = {}
            for raw in raw_entries:
                if not isinstance(raw, tuple) or len(raw) != 9:
                    return None
                (
                    address,
                    module_kind,
                    raw_locator,
                    is_form,
                    compiled,
                    raw_sources,
                    diagnostics,
                    conflict,
                    address_collision,
                ) = raw
                if not isinstance(address, str) or not address or address in entries:
                    return None
                if not isinstance(module_kind, str):
                    return None
                if type(is_form) is not bool or type(compiled) is not bool:
                    return None
                if (
                    type(conflict) is not bool
                    or type(address_collision) is not bool
                    or not isinstance(raw_sources, list)
                ):
                    return None
                if not isinstance(diagnostics, tuple) or not all(
                    isinstance(item, str) and item in _CATEGORIES
                    for item in diagnostics
                ):
                    return None
                locator = (
                    None
                    if raw_locator is None
                    else ModuleLocator.from_state(raw_locator)
                )
                sources: list[FormSource] = []
                for raw_source in raw_sources:
                    if not isinstance(raw_source, tuple) or len(raw_source) != 2:
                        return None
                    kind, raw_source_locator = raw_source
                    if not isinstance(kind, str) or not kind:
                        return None
                    sources.append(
                        FormSource(kind, ModuleLocator.from_state(raw_source_locator))
                    )
                entries[address] = CatalogEntry(
                    address,
                    module_kind,
                    locator,
                    is_form,
                    compiled,
                    tuple(sources),
                    diagnostics,
                    conflict,
                    address_collision,
                    (address.casefold(), address),
                )

            outcomes: list[CandidateOutcome] = []
            for raw in raw_outcomes:
                if not isinstance(raw, tuple) or len(raw) != 4:
                    return None
                ordinal, category, address, reason = raw
                if type(ordinal) is not int or ordinal < 1:
                    return None
                if category not in _CATEGORIES:
                    return None
                if address is not None and not isinstance(address, str):
                    return None
                if not isinstance(reason, str):
                    return None
                outcomes.append(
                    CandidateOutcome(ordinal, category, address, reason)
                )

            def parse_problem(raw: object) -> CatalogProblem | None:
                if not isinstance(raw, tuple) or len(raw) != 4:
                    return None
                category, address, ordinal, reason = raw
                if category not in _CATEGORIES:
                    return None
                if address is not None and not isinstance(address, str):
                    return None
                if type(ordinal) is not int or ordinal < 1:
                    return None
                if not isinstance(reason, str):
                    return None
                return CatalogProblem(category, address, ordinal, reason)

            problems: list[CatalogProblem] = []
            for raw in raw_problems:
                problem = parse_problem(raw)
                if problem is None:
                    return None
                problems.append(problem)
            object_problem_rows: list[CatalogProblem] = []
            for raw in raw_object_problems:
                problem = parse_problem(raw)
                if problem is None or problem.address is None:
                    return None
                object_problem_rows.append(problem)
            problem_counts: dict[str, int] = {}
            for raw in raw_problem_counts:
                if (
                    not isinstance(raw, tuple)
                    or len(raw) != 2
                    or raw[0] not in _CATEGORIES
                    or type(raw[1]) is not int
                    or raw[1] <= 0
                    or raw[0] in problem_counts
                ):
                    return None
                problem_counts[raw[0]] = raw[1]

            coverage = CatalogCoverage(
                raw_coverage[0],
                **dict(zip(_CATEGORIES, raw_coverage[1:], strict=True)),
            )
            if coverage.total_candidates != len(outcomes):
                return None
            counted = {category: 0 for category in _CATEGORIES}
            for outcome in outcomes:
                counted[outcome.category] += 1
            if any(
                getattr(coverage, category) != counted[category]
                for category in _CATEGORIES
            ):
                return None
            if [outcome.ordinal for outcome in outcomes] != list(
                range(1, len(outcomes) + 1)
            ):
                return None
            if len({ключ_адреса(address) for address in entries}) != len(entries):
                return None

            entries_by_key = {
                ключ_адреса(address): entry for address, entry in entries.items()
            }
            allowed_sources = {
                "descriptor": ("file", ""),
                "form_xml": ("file", ""),
                "form_bin": ("container", "form"),
                "container": ("container", "form"),
                "module": ("file", ""),
            }
            for entry in entries.values():
                if entry.address_collision and not entry.conflict:
                    return None
                if entry.conflict != ("conflict" in entry.diagnostics):
                    return None
                if entry.conflict and entry.locator is not None:
                    return None
                if entry.compiled != (
                    entry.locator is not None
                    and entry.locator.kind == "compiled"
                ):
                    return None
                if not entry.is_form and entry.form_sources:
                    return None
                if entry.locator is not None and _address_for_locator(
                    entry.locator
                ) != entry.address:
                    return None
                seen_sources: set[tuple[str, tuple[str, str, str]]] = set()
                for source in entry.form_sources:
                    expected_locator = allowed_sources.get(source.kind)
                    if expected_locator is None:
                        return None
                    if (
                        source.locator.kind,
                        source.locator.entry,
                    ) != expected_locator:
                        return None
                    source_key = (source.kind, source.locator.to_state())
                    if source_key in seen_sources:
                        return None
                    seen_sources.add(source_key)
                    if _address_for_locator(source.locator) != entry.address:
                        return None

            for outcome in outcomes:
                if outcome.category == "unknown_address":
                    if outcome.address is not None or not outcome.reason:
                        return None
                    continue
                if outcome.address is None:
                    return None
                if ключ_адреса(outcome.address) not in entries_by_key:
                    return None

            addressed_problem_keys = {
                (problem.category, problem.ordinal, problem.address)
                for problem in object_problem_rows
            }
            if len(addressed_problem_keys) != len(object_problem_rows):
                return None
            for problem in object_problem_rows:
                matching = next(
                    (
                        outcome
                        for outcome in outcomes
                        if outcome.ordinal == problem.ordinal
                        and outcome.category == problem.category
                    ),
                    None,
                )
                if matching is None:
                    return None
                if problem.address != matching.address:
                    return None
            for outcome in outcomes:
                if outcome.category in {
                    "missing_body",
                    "broken_container",
                    "unreadable_body",
                } and (
                    outcome.category,
                    outcome.ordinal,
                    outcome.address,
                ) not in addressed_problem_keys:
                    return None
            conflict_entries = {
                ключ_адреса(entry.address)
                for entry in entries.values()
                if entry.conflict
            }
            conflict_problems = {
                ключ_адреса(problem.address)
                for problem in object_problem_rows
                if problem.category == "conflict" and problem.address is not None
            }
            if conflict_entries != conflict_problems:
                return None
            expected_problem_counts = {
                category: sum(
                    outcome.category == category for outcome in outcomes
                )
                for category in (
                    "missing_body",
                    "unknown_address",
                    "broken_container",
                    "unreadable_body",
                )
            }
            expected_problem_counts["conflict"] = len(conflict_entries)
            expected_problem_counts = {
                key: value for key, value in expected_problem_counts.items() if value
            }
            if problem_counts != expected_problem_counts:
                return None
            reconstructed = list(object_problem_rows)
            reconstructed.extend(
                CatalogProblem(
                    "unknown_address",
                    None,
                    outcome.ordinal,
                    outcome.reason,
                )
                for outcome in outcomes
                if outcome.category == "unknown_address"
            )
            bounded = sorted(
                reconstructed,
                key=lambda item: (
                    item.address is None,
                    item.address.casefold() if item.address else "",
                    item.address or "",
                    item.category,
                    item.ordinal,
                    item.reason,
                ),
            )[:20]
            if problems != bounded:
                return None
            grouped_problems: dict[str, list[CatalogProblem]] = {}
            for problem in object_problem_rows:
                assert problem.address is not None
                grouped_problems.setdefault(problem.address, []).append(problem)
            ordered = {
                entry.address: entry
                for entry in sorted(entries.values(), key=lambda item: item.sort_key)
            }
            return cls(
                identity,
                ordered,
                tuple(outcomes),
                tuple(problems),
                coverage,
                tuple(sorted(problem_counts.items())),
                {key: tuple(value) for key, value in grouped_problems.items()},
            )
        except (KeyError, TypeError, ValueError):
            return None


def _address_for_locator(locator: ModuleLocator) -> str:
    """Доказать, что cache locator действительно принадлежит адресу."""
    relative = locator.relative_path
    if relative.endswith(".bsl"):
        return адрес_модуля(relative)
    if relative.endswith(".Module"):
        return адрес_скомпилированного_модуля(relative)
    if relative.endswith(".txt") or relative.endswith(".Form"):
        return разобрать_плоское_имя(relative).address
    if relative.endswith("Form.xml") or relative.endswith(
        "Form.bin"
    ) or relative.endswith(".xml"):
        return _tree_form(relative)[0]
    raise ValueError("локатор не соответствует поддержанному кандидату")


@dataclass(slots=True)
class _Candidate:
    ordinal: int
    address: str | None
    module_kind: str
    locator: ModuleLocator | None
    is_form: bool
    evidence: str | None
    evidence_locator: ModuleLocator | None
    category: str
    reason: str = ""
    digest: str | None = None


def _unknown_address_reason(relative: str, error: BaseException) -> str:
    """Причина для журнала: какой файл выгрузки и почему адрес не доказан.

    Публичные ответы этот текст не показывают: наружу уходит категория и
    порядковый номер. Путь здесь относительный внутри выгрузки, не хостовый
    корень источника.
    """
    detail = str(error).strip()
    reason = f"канонический адрес не доказан: {relative}"
    if detail:
        return f"{reason}; {detail}"
    return reason


def _is_form_address(address: str) -> bool:
    return address.startswith("ОбщаяФорма.") or ".Форма." in address


def _tree_form(relative: str) -> tuple[str, str]:
    parts = relative.split("/")
    if len(parts) == 2 and parts[0] == "CommonForms" and parts[1].endswith(
        ".xml"
    ):
        name = parts[1][: -len(".xml")]
        pseudo = f"CommonForms/{name}/Ext/Form/Module.bsl"
        return адрес_модуля(pseudo), "descriptor"
    if (
        len(parts) == 4
        and parts[2] == "Forms"
        and parts[3].endswith(".xml")
    ):
        form = parts[3][: -len(".xml")]
        pseudo = f"{parts[0]}/{parts[1]}/Forms/{form}/Ext/Form/Module.bsl"
        return адрес_модуля(pseudo), "descriptor"
    if relative.endswith("/Ext/Form.xml"):
        pseudo = relative[: -len("Form.xml")] + "Form/Module.bsl"
        return адрес_модуля(pseudo), "form_xml"
    if relative.endswith("/Ext/Form.bin"):
        pseudo = relative[: -len("Form.bin")] + "Form/Module.bsl"
        return адрес_модуля(pseudo), "form_bin"
    raise ValueError("неподдержанный путь доказательства формы")


def _classify_readable(
    root: Path,
    address: str,
    locator: ModuleLocator,
) -> tuple[str, str | None, str]:
    try:
        text = read_bsl(root, address, locator)
    except ContentReadError as error:
        if error.category == "container_entry_missing":
            return "missing_body", None, error.reason
        if locator.kind == "container" and error.category == "container_unreadable":
            return "broken_container", None, error.reason
        return "unreadable_body", None, error.reason
    if not text.strip():
        return (
            "empty",
            hashlib.sha256(b"").hexdigest(),
            "тело модуля пусто",
        )
    digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
    return "indexed", digest, ""


def _candidate(root: Path, path: Path, ordinal: int) -> _Candidate:
    relative = path.relative_to(root).as_posix()
    address: str
    locator: ModuleLocator | None = None
    is_form = False
    evidence: str | None = None
    module_kind = ""

    try:
        if relative.endswith(".bsl"):
            address = адрес_модуля(relative)
            locator = ModuleLocator.file(relative)
            module_kind = path.name[: -len(".bsl")]
            is_form = _is_form_address(address)
            evidence = "module" if is_form else None
        elif relative.endswith(".Module"):
            address = адрес_скомпилированного_модуля(relative)
            locator = ModuleLocator.compiled(relative)
            module_kind = "compiled"
        elif relative.endswith(".txt") or relative.endswith(".Form"):
            flat = разобрать_плоское_имя(relative)
            address = flat.address
            module_kind = flat.pattern
            is_form = flat.is_form
            if flat.compiled:
                locator = ModuleLocator.compiled(relative)
            elif flat.representation == "container":
                locator = ModuleLocator.container(relative, "module")
                evidence = "container"
            else:
                locator = ModuleLocator.file(relative)
                evidence = "module" if is_form else None
        elif relative.endswith("Form.xml") or relative.endswith(
            "Form.bin"
        ) or relative.endswith(".xml"):
            address, evidence = _tree_form(relative)
            module_kind = "form"
            is_form = True
            if relative.endswith("Form.bin"):
                locator = ModuleLocator.container(relative, "module")
        else:
            raise ValueError("неподдержанный кандидат")
    except (FlatNameError, ValueError) as error:
        return _Candidate(
            ordinal,
            None,
            "unknown",
            None,
            False,
            None,
            None,
            "unknown_address",
            _unknown_address_reason(relative, error),
        )

    if locator is None:
        return _Candidate(
            ordinal,
            address,
            module_kind,
            None,
            is_form,
            evidence,
            ModuleLocator.file(relative),
            "indexed",
        )
    if locator.kind == "compiled":
        return _Candidate(
            ordinal,
            address,
            module_kind,
            locator,
            is_form,
            evidence,
            ModuleLocator.file(relative) if evidence else None,
            "compiled",
        )
    category, digest, reason = _classify_readable(root, address, locator)
    return _Candidate(
        ordinal,
        address,
        module_kind,
        locator,
        is_form,
        evidence,
        (
            ModuleLocator.container(relative, "form")
            if evidence in {"container", "form_bin"}
            else locator if evidence else None
        ),
        category,
        reason,
        digest,
    )


def catalog_files(root: Path) -> tuple[Path, ...]:
    """Единственный детерминированный снимок файлов канонического корня."""
    return tuple(
        path
        for path in sorted(root.rglob("*"))
        if path.is_file() and path.name != CATALOG_FILE
    )


def build_catalog(
    root: Path,
    identity: LocatorIdentity,
    *,
    files: tuple[Path, ...] | None = None,
    progress: Callable[[int, int], None] | None = None,
) -> ModuleCatalog:
    """Перечислить источник один раз и вернуть неизменяемый снимок."""
    snapshot = catalog_files(root) if files is None else files
    if progress is not None:
        progress(0, len(snapshot))
    candidates = []
    for ordinal, path in enumerate(snapshot, 1):
        candidates.append(_candidate(root, path, ordinal))
        if progress is not None:
            progress(ordinal, len(snapshot))

    groups: dict[str, list[_Candidate]] = {}
    for candidate in candidates:
        if candidate.address is not None:
            groups.setdefault(ключ_адреса(candidate.address), []).append(candidate)

    entries: list[CatalogEntry] = []
    conflict_problems: list[CatalogProblem] = []
    for grouped in groups.values():
        addresses = {candidate.address for candidate in grouped}
        readable = [candidate for candidate in grouped if candidate.digest is not None]
        body_candidates = [
            candidate
            for candidate in readable
            if candidate.category == "indexed"
            or (
                candidate.category == "empty"
                and candidate.locator is not None
                and candidate.locator.kind == "file"
            )
        ]
        case_collision = len(addresses) != 1
        conflict = case_collision or len({c.digest for c in readable}) > 1
        address = sorted(addresses, key=lambda value: (value.casefold(), value))[0]
        if conflict:
            affected = grouped if len(addresses) != 1 else readable
            reason = (
                "канонические адреса различаются только регистром"
                if case_collision
                else "один канонический адрес содержит разные тексты"
            )
            for candidate in affected:
                candidate.category = "conflict"
                candidate.reason = reason
            conflict_problems.append(
                CatalogProblem(
                    "conflict",
                    address,
                    min(
                        candidate.ordinal
                        for candidate in grouped
                        if candidate.address == address
                    ),
                    reason,
                )
            )
        compiled_locators = [
            candidate.locator
            for candidate in grouped
            if candidate.category == "compiled" and candidate.locator is not None
        ]
        chosen = (
            None
            if conflict
            else body_candidates[0].locator
            if body_candidates
            else compiled_locators[0]
            if compiled_locators
            else None
        )
        form_sources = tuple(
            FormSource(kind, locator)
            for kind, locator in sorted(
                {
                    (candidate.evidence, candidate.evidence_locator)
                    for candidate in grouped
                    if candidate.evidence and candidate.evidence_locator
                },
                key=lambda item: (item[0], item[1].to_state()),
            )
        )
        if case_collision:
            # Ни одно физическое доказательство нельзя приписать выбранному
            # написанию адреса: другое отличается только регистром и на
            # case-sensitive FS может быть иным файлом.
            form_sources = ()
        diagnostics = tuple(
            sorted(
                {
                    candidate.category
                    for candidate in grouped
                    if candidate.category
                    not in {"indexed", "compiled", "empty"}
                }
            )
        )
        entries.append(
            CatalogEntry(
                address=address,
                module_kind=(
                    readable[0].module_kind
                    if readable
                    else next(
                        (
                            candidate.module_kind
                            for candidate in grouped
                            if candidate.locator is not None
                        ),
                        grouped[0].module_kind,
                    )
                ),
                locator=chosen,
                is_form=any(candidate.is_form for candidate in grouped),
                compiled=chosen is not None and chosen.kind == "compiled",
                form_sources=form_sources,
                diagnostics=diagnostics,
                conflict=conflict,
                address_collision=case_collision,
                sort_key=(address.casefold(), address),
            )
        )

    outcomes = tuple(
        CandidateOutcome(
            candidate.ordinal,
            candidate.category,
            candidate.address,
            candidate.reason,
        )
        for candidate in candidates
    )
    counts = {category: 0 for category in _CATEGORIES}
    for outcome in outcomes:
        counts[outcome.category] += 1
    coverage = CatalogCoverage(len(outcomes), **counts)

    ordinary_problems = [
        CatalogProblem(
            candidate.category,
            candidate.address,
            candidate.ordinal,
            candidate.reason,
        )
        for candidate in candidates
        if candidate.category
        in {"missing_body", "unknown_address", "broken_container", "unreadable_body"}
    ]
    problems = tuple(
        sorted(
            [*ordinary_problems, *conflict_problems],
            key=lambda problem: (
                problem.address.casefold() if problem.address else "",
                problem.address or "",
                problem.category,
                problem.ordinal,
            ),
        )
    )
    ordered_entries = {
        entry.address: entry for entry in sorted(entries, key=lambda entry: entry.sort_key)
    }
    return ModuleCatalog(identity, ordered_entries, outcomes, problems, coverage)
