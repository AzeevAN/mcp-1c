"""Цена повторного startup (`POST /admin/reload`) на native-поколениях.

Reload не должен переписывать файлы кэша, из которых только что всё поднял.
"""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

from mcp1c.intake_v2_registry import ExportIdentity
from mcp1c.registry import Registry
from test_intake_v2_extensions import _materialized, _stage_active_base


def _два_поколения(tmp_path) -> Path:
    data = tmp_path / "data"
    registry = Registry(data)
    for number, name in enumerate(("Альфа", "Бета"), 1):
        _collection, generation = _materialized(
            tmp_path, f"generation-{number}", configuration_name=name
        )
        generation = type(generation)(
            generation.root,
            replace(
                generation.manifest, identity=ExportIdentity.configuration(name)
            ),
            generation.payloads,
        )
        registry.publish_generation(_stage_active_base(registry, generation))
    assert set(registry.configurations) == {"Альфа", "Бета"}
    return data


def _кэш(registry: Registry) -> dict[str, tuple[int, int]]:
    return {
        str(path.relative_to(registry.cache_dir)): (
            path.stat().st_ino,
            path.stat().st_mtime_ns,
        )
        for path in registry.cache_dir.rglob("*")
        if path.is_file()
    }


def test_reload_не_переписывает_кэш_после_попадания(tmp_path):
    registry = Registry(_два_поколения(tmp_path))
    registry.startup()
    до = _кэш(registry)
    assert any(name.endswith(".objects") for name in до), до

    registry.startup()

    после = _кэш(registry)
    assert {name: после[name] for name in до} == до



def test_reload_дописывает_только_пропавший_кэш(tmp_path):
    registry = Registry(_два_поколения(tmp_path))
    registry.startup()
    до = _кэш(registry)
    пропавшие = [
        name for name in до if name.startswith("Альфа.") and name.endswith(".objects")
    ] + [name for name in до if name.startswith("Бета_modules.")]
    assert len(пропавшие) >= 2, до
    for name in пропавшие:
        (registry.cache_dir / name).unlink()

    registry.startup()

    после = _кэш(registry)
    assert all(name in после for name in пропавшие)
    assert {
        name: после[name] for name in до if name not in пропавшие
    } == {name: value for name, value in до.items() if name not in пропавшие}
