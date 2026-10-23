#!/usr/bin/env python3
"""Найти активные поколения старого формата, ничего не меняя в data/."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

# Скрипт работает и из checkout без установки пакета.
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from mcp1c.intake_v2 import LayerKind, LayerManifest, LayerState, SourceKind
from mcp1c.intake_v2_registry import (
    BundleStoreError,
    GenerationBundleStore,
    GenerationPointer,
    hash_layer_payload,
    hash_layer_semantic,
    load_layer_payload,
)
from mcp1c.intake_v2_runtime import _BALANCE_FIELD_KEYS, _FIELD_KEYS


# Это только диагностические признаки прежних строк, не runtime fallback.
_UNSUPPORTED_SOURCE_KINDS = frozenset({"modules", "extension", "extension-runtime"})
_SUPPORTED_SOURCE_KINDS = frozenset({"configuration", "syntax"})
# Текущий дисковый формат registry.REGISTRY_VERSION; Registry не запускаем.
_REGISTRY_FORMAT_VERSION = 1
_OLD_FIELD_SHAPES = {
    frozenset(_FIELD_KEYS - {"string_allowed_length"}),
    frozenset(_BALANCE_FIELD_KEYS - {"string_allowed_length"}),
}
_CURRENT_FIELD_SHAPES = {frozenset(_FIELD_KEYS), frozenset(_BALANCE_FIELD_KEYS)}


def _report() -> dict:
    return {
        "schema_version": 1,
        "complete": False,
        "total_generations": 0,
        "checked_generations": 0,
        "field_records": 0,
        "legacy_field_records": 0,
        "legacy_generations": [],
        "retired_sources": [],
        "errors": [],
    }


def _error(report: dict, relative_path: str, reason: str, **context: object) -> None:
    report["errors"].append({"relative_path": relative_path, "reason": reason, **context})


def _field_counts(semantic: dict) -> tuple[int, int]:
    total = legacy = 0
    pending: list[object] = [semantic]
    while pending:
        value = pending.pop()
        if isinstance(value, dict):
            shape = frozenset(value)
            if shape in _OLD_FIELD_SHAPES:
                total += 1
                legacy += 1
            elif shape in _CURRENT_FIELD_SHAPES:
                total += 1
            pending.extend(value.values())
        elif isinstance(value, list):
            pending.extend(value)
    return total, legacy


def _read_structure(store: GenerationBundleStore, root: Path, layer: LayerManifest) -> dict:
    path = root / layer.relative_path
    store._validate_path_chain(path)
    payload = load_layer_payload(path)
    if payload.kind is not layer.kind:
        raise BundleStoreError("Неверный вид структурного слоя")
    expected = layer.payload_sha256 or layer.content_sha256
    if hash_layer_payload(layer.kind, path) != expected:
        raise BundleStoreError("Не совпала контрольная сумма структурного слоя")
    if layer.payload_sha256 and hash_layer_semantic(layer.kind, payload.semantic) != layer.content_sha256:
        raise BundleStoreError("Не совпала смысловая контрольная сумма структурного слоя")
    return dict(payload.semantic)


def _scan_generation(
    store: GenerationBundleStore, pointer: GenerationPointer,
) -> tuple[list[str], int, int, list[str]]:
    root = store.data_dir / pointer.root_path
    manifest = store._load_manifest(root, pointer)
    base = next(
        (layer for layer in manifest.layers if layer.kind is LayerKind.BASE_STRUCTURE),
        None,
    )
    is_configuration = pointer.identity.source_kind is SourceKind.CONFIGURATION
    if is_configuration and (base is None or base.state is not LayerState.READY):
        raise BundleStoreError("Нет доступного слоя base_structure")
    structure_kinds = {LayerKind.EXTENDED_STRUCTURE}
    if is_configuration:
        structure_kinds.add(LayerKind.BASE_STRUCTURE)
    reasons: list[str] = []
    affected_layers: list[str] = []
    total = legacy = 0
    for layer in manifest.layers:
        if (
            layer.kind not in structure_kinds
            or layer.state is not LayerState.READY
        ):
            continue
        semantic = _read_structure(store, root, layer)
        layer_total, layer_legacy = _field_counts(semantic)
        total += layer_total
        legacy += layer_legacy
        missing_compatibility = (
            layer.kind is LayerKind.BASE_STRUCTURE
            and "compatibility_mode" not in semantic
        )
        if missing_compatibility:
            reasons.append("missing_compatibility_mode")
        if layer_legacy and "legacy_field_shape" not in reasons:
            reasons.append("legacy_field_shape")
        if missing_compatibility or layer_legacy:
            affected_layers.append(layer.kind.value)
    return reasons, total, legacy, affected_layers


def scan(data_dir: Path) -> dict:
    """Проверить только active pointers registry.json; ошибки не скрываются."""
    report = _report()
    try:
        store = GenerationBundleStore(data_dir)
        registry_path = store.data_dir / "registry.json"
        store._validate_path_chain(registry_path)
        registry = json.loads(registry_path.read_bytes())
    except (OSError, ValueError, RuntimeError):
        _error(report, "registry.json", "Реестр недоступен или не является корректным JSON")
        return report
    if (
        not isinstance(registry, dict)
        or type(registry.get("registry_version")) is not int
        or registry["registry_version"] != _REGISTRY_FORMAT_VERSION
    ):
        _error(report, "registry.json", "Неизвестный формат реестра")
        return report

    sources = registry.get("sources", [])
    if not isinstance(sources, list):
        _error(report, "registry.json", "sources должен быть массивом")
        sources = []
    seen_sources: set[str] = set()
    for index, source in enumerate(sources):
        if (
            not isinstance(source, dict)
            or not isinstance(source.get("id"), str)
            or not source["id"]
            or not isinstance(source.get("kind"), str)
            or source["kind"] not in _SUPPORTED_SOURCE_KINDS | _UNSUPPORTED_SOURCE_KINDS
        ):
            _error(report, "registry.json", "Некорректная строка источника", row_index=index)
            continue
        if source["id"] in seen_sources:
            _error(report, "registry.json", "Повторяющийся идентификатор источника", row_index=index)
        seen_sources.add(source["id"])
        # Индекс справки может лежать вне data/. Его содержимое здесь не
        # читается, а путь не выводится, чтобы не раскрывать локальные имена.
        if not isinstance(source.get("stored_path", ""), str):
            _error(report, "registry.json", "Некорректный путь источника", row_index=index)
            continue
        if source["kind"] in _UNSUPPORTED_SOURCE_KINDS:
            report["retired_sources"].append({
                "identity": {"source_id": source["id"], "source_kind": source["kind"]},
                "generation_id": None,
                "relative_path": "registry.json",
                "row_index": index,
                "reasons": ["unsupported_source_kind"],
            })

    pointers = registry.get("generation_manifests", [])
    if not isinstance(pointers, list):
        _error(report, "registry.json", "generation_manifests должен быть массивом")
        pointers = []
    report["total_generations"] = len(pointers)
    seen_identities: set[tuple[str, ...]] = set()
    for index, raw in enumerate(pointers):
        try:
            pointer = GenerationPointer.from_dict(raw)
        except (ValueError, TypeError, RecursionError, BundleStoreError):
            _error(report, "registry.json", "Некорректный указатель поколения", row_index=index)
            continue
        context = {"identity": pointer.identity.to_dict(), "generation_id": pointer.generation_id}
        if pointer.identity.grouping_key in seen_identities:
            _error(report, pointer.root_path, "Повторяющаяся identity поколения", **context)
            continue
        seen_identities.add(pointer.identity.grouping_key)
        try:
            reasons, total, legacy, affected_layers = _scan_generation(store, pointer)
        except (OSError, ValueError, RecursionError, BundleStoreError):
            _error(report, pointer.root_path, "Manifest или структурный слой недоступен либо повреждён", **context)
            continue
        report["checked_generations"] += 1
        report["field_records"] += total
        report["legacy_field_records"] += legacy
        if reasons:
            report["legacy_generations"].append({
                **context,
                "relative_path": pointer.root_path,
                "reasons": reasons,
                "affected_layers": affected_layers,
                "field_records": total,
                "legacy_field_records": legacy,
            })
    report["complete"] = not report["errors"]
    return report


class _ArgumentError(ValueError):
    pass


class _Parser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        raise _ArgumentError("Нужен --data PATH без дополнительных аргументов")

    def print_help(self, file=None) -> None:
        super().print_help(file=sys.stderr if file is None else file)


def main(argv: list[str] | None = None) -> int:
    parser = _Parser(description=__doc__)
    parser.add_argument("--data", type=Path, required=True, help="Проверяемый каталог данных (только чтение)")
    try:
        args = parser.parse_args(argv)
    except _ArgumentError as error:
        report = _report()
        _error(report, "", str(error))
    else:
        report = scan(args.data)
    print(json.dumps(report, ensure_ascii=False, sort_keys=True))
    if not report["complete"]:
        return 2
    return 1 if report["legacy_generations"] or report["retired_sources"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
