"""Актуальный JSON-журнал покрытия одного корпуса кода.

Журнал относится к ``Source`` вида ``modules`` или ``extension``. Имя файла
выводится из полного sha256 идентификатора источника: пользовательское имя не
становится путём, а повторная загрузка атомарно заменяет тот же файл.
"""

from __future__ import annotations

import hashlib
import json
import os
import uuid
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .registry import LoadedModules, Source


SCHEMA_VERSION = 1
KIND = "module_coverage"
WRITE_WARNING = (
    "Журнал покрытия не записан; подробности доступны в журнале сервера."
)


def _filename(source_id: str) -> str:
    digest = hashlib.sha256(source_id.encode("utf-8")).hexdigest()
    return f"code-{digest}.json"


def relative_path(source_id: str) -> str:
    return f"logs/{_filename(source_id)}"


def log_path(data_dir: str | Path, source_id: str) -> Path:
    return Path(data_dir) / relative_path(source_id)


def _open_directory(data_dir: str | Path, *, create: bool) -> int | None:
    directory = Path(data_dir) / "logs"
    if create:
        directory.mkdir(parents=True, exist_ok=True)
    elif not directory.exists():
        return None
    flags = os.O_RDONLY
    flags |= getattr(os, "O_DIRECTORY", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    # O_NOFOLLOW запрещает подменить data/logs ссылкой наружу. Все дальнейшие
    # операции идут только по имени внутри закреплённого dir_fd.
    return os.open(directory, flags)


def _problem_sort_key(item) -> tuple:
    return (
        item.address is None,
        item.address.casefold() if item.address else "",
        item.address or "",
        item.category,
        item.ordinal,
        item.reason,
        -1 if item.marker is None else item.marker,
    )


def build_payload(loaded: "LoadedModules") -> dict[str, Any]:
    """Полный безопасный снимок того же поколения, что публичные агрегаты."""
    # Локальный импорт разрывает зависимость: registry вызывает запись
    # журнала, а tools уже импортирует типы registry для публичных ответов.
    from . import tools

    source = loaded.source
    coverage = tools._code_coverage(loaded, include_problem_rows=False)
    problems = sorted(tools._iter_code_problems(loaded), key=_problem_sort_key)
    return {
        "schema_version": SCHEMA_VERSION,
        "kind": KIND,
        "source": {
            "id": source.id,
            "kind": source.kind,
            "sha256": source.sha256,
            "loaded_at": source.loaded_at,
            "selection_version": source.selection_version,
            "locator_generation": source.locator_generation,
            "code_version": source.code_version,
        },
        "identity": {
            "source_id": loaded.каталог.identity.source_id,
            "source_sha256": loaded.каталог.identity.source_sha256,
            "generation": loaded.каталог.identity.generation,
        },
        "coverage": {
            "modules": {
                "total": coverage.modules_total,
                "source_available": coverage.modules_source_available,
                "empty": coverage.modules_empty,
                "partial": coverage.modules_partial,
                "unreadable": coverage.modules_unreadable,
                "conflict": coverage.modules_conflict,
                "compiled_without_source": (
                    coverage.modules_compiled_without_source
                ),
            },
            "procedures": {
                "total": coverage.procedures_total,
                "full": coverage.procedures_full,
                "partial": coverage.procedures_partial,
            },
            "form_structures": {
                "total": coverage.forms_total,
                "full": coverage.form_structures_full,
                "partial": coverage.form_structures_partial,
                "unavailable": coverage.form_structures_unread,
            },
            "form_modules": {
                "total": coverage.forms_total,
                "read": coverage.form_modules_read,
                "empty": coverage.form_modules_empty,
                "missing": coverage.form_modules_missing,
                "unreadable": coverage.form_modules_unread,
            },
            "limitations": {
                "categories": dict(coverage.problem_categories),
                "occurrences_total": coverage.problems_total,
                "problem_rows_total": len(problems),
                "unknown_markers": coverage.unknown_markers,
                "known_markers_incomplete": coverage.known_markers_incomplete,
                "unsupported_addresses": coverage.unsupported_addresses,
                "broken_containers": coverage.broken_containers,
                "unreadable_bodies": coverage.unreadable_bodies,
                "budget_exceeded": coverage.budget_exceeded,
                "body_conflicts": coverage.body_conflicts,
                "compiled_without_source": coverage.compiled_without_source,
            },
        },
        "problems": [
            {
                "category": item.category,
                "address": item.address,
                "ordinal": item.ordinal,
                "reason": item.reason,
                "marker": item.marker,
            }
            for item in problems
        ],
    }


def write(data_dir: str | Path, loaded: "LoadedModules") -> str:
    """Атомарно заменить журнал корпуса и вернуть относительный путь."""
    payload = build_payload(loaded)
    encoded = (
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    ).encode("utf-8")
    directory_fd = _open_directory(data_dir, create=True)
    assert directory_fd is not None
    target = _filename(loaded.source.id)
    temporary = f".{target}.{uuid.uuid4().hex}.tmp"
    file_fd: int | None = None
    try:
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        flags |= getattr(os, "O_NOFOLLOW", 0)
        file_fd = os.open(temporary, flags, 0o600, dir_fd=directory_fd)
        with os.fdopen(file_fd, "wb", closefd=True) as stream:
            file_fd = None
            stream.write(encoded)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(
            temporary,
            target,
            src_dir_fd=directory_fd,
            dst_dir_fd=directory_fd,
        )
        os.fsync(directory_fd)
    finally:
        if file_fd is not None:
            os.close(file_fd)
        try:
            os.unlink(temporary, dir_fd=directory_fd)
        except FileNotFoundError:
            pass
        os.close(directory_fd)
    return relative_path(loaded.source.id)


def _identity_matches(payload: object, source: "Source") -> bool:
    if not isinstance(payload, dict):
        return False
    if payload.get("schema_version") != SCHEMA_VERSION or payload.get("kind") != KIND:
        return False
    identity = payload.get("identity")
    source_row = payload.get("source")
    return (
        isinstance(identity, dict)
        and set(identity) == {"source_id", "source_sha256", "generation"}
        and isinstance(source_row, dict)
        and set(source_row) == {
            "id",
            "kind",
            "sha256",
            "loaded_at",
            "selection_version",
            "locator_generation",
            "code_version",
        }
        and type(identity.get("generation")) is int
        and type(source_row.get("selection_version")) is int
        and type(source_row.get("locator_generation")) is int
        and identity.get("source_id") == source.id
        and identity.get("source_sha256") == source.sha256
        and identity.get("generation") == source.locator_generation
        and source_row.get("id") == source.id
        and source_row.get("kind") == source.kind
        and source_row.get("sha256") == source.sha256
        and source_row.get("loaded_at") == source.loaded_at
        and source_row.get("selection_version") == source.selection_version
        and source_row.get("locator_generation") == source.locator_generation
        and source_row.get("code_version") == source.code_version
    )


def _valid_count_table(
    table: object,
    *,
    categories: tuple[str, ...],
) -> bool:
    if not isinstance(table, dict) or set(table) != {"total", *categories}:
        return False
    if any(type(value) is not int or value < 0 for value in table.values()):
        return False
    return table["total"] == sum(table[key] for key in categories)


def _valid_payload(payload: object, source: "Source") -> bool:
    if not _identity_matches(payload, source):
        return False
    assert isinstance(payload, dict)
    coverage = payload.get("coverage")
    problems = payload.get("problems")
    if not isinstance(coverage, dict) or set(coverage) != {
        "modules",
        "procedures",
        "form_structures",
        "form_modules",
        "limitations",
    }:
        return False
    if not _valid_count_table(
        coverage["modules"],
        categories=(
            "source_available",
            "empty",
            "partial",
            "unreadable",
            "conflict",
            "compiled_without_source",
        ),
    ):
        return False
    if not _valid_count_table(
        coverage["procedures"], categories=("full", "partial")
    ):
        return False
    if not _valid_count_table(
        coverage["form_structures"],
        categories=("full", "partial", "unavailable"),
    ):
        return False
    if not _valid_count_table(
        coverage["form_modules"],
        categories=("read", "empty", "missing", "unreadable"),
    ):
        return False
    if coverage["form_structures"]["total"] != coverage["form_modules"]["total"]:
        return False
    limitations = coverage["limitations"]
    limitation_counts = {
        "occurrences_total",
        "problem_rows_total",
        "unknown_markers",
        "known_markers_incomplete",
        "unsupported_addresses",
        "broken_containers",
        "unreadable_bodies",
        "budget_exceeded",
        "body_conflicts",
        "compiled_without_source",
    }
    if (
        not isinstance(limitations, dict)
        or set(limitations) != {"categories", *limitation_counts}
        or any(
            type(limitations[key]) is not int or limitations[key] < 0
            for key in limitation_counts
        )
    ):
        return False
    categories = limitations.get("categories")
    rows_total = limitations.get("problem_rows_total")
    if (
        not isinstance(categories, dict)
        or any(
            not isinstance(key, str) or type(value) is not int or value < 0
            for key, value in categories.items()
        )
        or type(rows_total) is not int
        or rows_total < 0
        or not isinstance(problems, list)
        or rows_total != len(problems)
        or limitations["occurrences_total"] != sum(categories.values())
    ):
        return False
    for row in problems:
        if (
            not isinstance(row, dict)
            or set(row) != {
                "category",
                "address",
                "ordinal",
                "reason",
                "marker",
            }
            or not isinstance(row["category"], str)
            or (row["address"] is not None and not isinstance(row["address"], str))
            or type(row["ordinal"]) is not int
            or row["ordinal"] < 0
            or not isinstance(row["reason"], str)
            or (row["marker"] is not None and type(row["marker"]) is not int)
        ):
            return False
    return True


def load_current(data_dir: str | Path, source: "Source") -> dict[str, Any] | None:
    """Прочитать только журнал текущего поколения; битый или старый — None."""
    try:
        directory_fd = _open_directory(data_dir, create=False)
        if directory_fd is None:
            return None
        try:
            flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
            file_fd = os.open(_filename(source.id), flags, dir_fd=directory_fd)
            with os.fdopen(file_fd, "r", encoding="utf-8") as stream:
                payload = json.load(stream)
        finally:
            os.close(directory_fd)
    except (OSError, UnicodeError, json.JSONDecodeError, ValueError):
        return None
    return payload if _valid_payload(payload, source) else None


def remove(data_dir: str | Path, source_id: str) -> None:
    """Удалить журнал источника; отсутствие файла уже является успехом."""
    directory_fd = _open_directory(data_dir, create=False)
    if directory_fd is None:
        return
    try:
        try:
            os.unlink(_filename(source_id), dir_fd=directory_fd)
        except FileNotFoundError:
            return
        os.fsync(directory_fd)
    finally:
        os.close(directory_fd)
