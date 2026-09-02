"""RED-контракты очистки legacy после native commit."""

from __future__ import annotations

import json

import pytest

from mcp1c.intake_v2_converter import convert_collection
from mcp1c.intake_v2_generation import materialize_generation
from mcp1c.registry import KIND_EXTENSION, Registry, RegistryError
from conftest import build_configuration, write_export
from test_intake_v2_converter import _collection


def _native_generation(tmp_path, suffix: str):
    collection = _collection(tmp_path / f"source-{suffix}", common_forms=True)
    return materialize_generation(
        collection,
        convert_collection(collection),
        tmp_path / f"materialized-{suffix}",
        generation_id=f"generation-{suffix}",
    )


def _legacy_registry(tmp_path, архив_кода, корень_кода) -> Registry:
    incoming = tmp_path / "legacy-input"
    incoming.mkdir()
    registry = Registry(tmp_path / "data")
    registry.add_configuration(
        write_export(
            incoming,
            build_configuration(name="DemoConfiguration"),
        )
    )
    registry.add_modules(
        архив_кода(корень_кода),
        configuration="DemoConfiguration",
    )
    return registry


def test_native_commit_удаляет_только_legacy_структуру_и_модули(
    tmp_path,
    архив_кода,
    корень_кода,
):
    registry = _legacy_registry(tmp_path, архив_кода, корень_кода)
    extension = registry.add_modules(
        архив_кода(корень_кода, extension="SyntheticExtension"),
        configuration="DemoConfiguration",
    )
    legacy_configuration = registry.sources["DemoConfiguration"]
    legacy_modules = registry.sources["DemoConfiguration:modules"]
    configuration_path = registry._absolute(legacy_configuration.stored_path)
    modules_path = registry._absolute(legacy_modules.stored_path)
    extension_path = registry._absolute(extension.stored_path)
    generation = _native_generation(tmp_path, "001")

    registry.publish_generation(
        registry.stage_generation(generation.manifest, generation.payloads)
    )

    assert "DemoConfiguration" not in registry.sources
    assert "DemoConfiguration:modules" not in registry.sources
    assert registry.sources[extension.id].kind == KIND_EXTENSION
    assert not configuration_path.exists()
    assert not modules_path.exists()
    assert extension_path.is_dir()
    assert registry.resolve("DemoConfiguration").modules is not None
    stored = json.loads(registry.registry_path.read_text(encoding="utf-8"))
    assert {source["id"] for source in stored["sources"]} == {extension.id}

    restarted = Registry(registry.data_dir)
    assert restarted.startup() == []
    assert restarted.sources[extension.id].kind == KIND_EXTENSION
    restored = restarted.resolve(
        "DemoConfiguration",
        extension="SyntheticExtension",
    )
    assert restored.extension is not None and restored.extension.готов


def test_авария_очистки_завершается_после_restart(
    tmp_path,
    архив_кода,
    корень_кода,
    monkeypatch,
):
    registry = _legacy_registry(tmp_path, архив_кода, корень_кода)
    legacy_configuration = registry.sources["DemoConfiguration"]
    legacy_modules = registry.sources["DemoConfiguration:modules"]
    configuration_path = registry._absolute(legacy_configuration.stored_path)
    modules_path = registry._absolute(legacy_modules.stored_path)
    generation = _native_generation(tmp_path, "crash")

    def crash_after_files(*_args, **_kwargs):
        raise SystemExit("синтетическая авария очистки legacy")

    monkeypatch.setattr(
        registry,
        "_after_legacy_files_removed",
        crash_after_files,
    )
    with pytest.raises(SystemExit, match="авария очистки legacy"):
        registry.publish_generation(
            registry.stage_generation(generation.manifest, generation.payloads)
        )

    assert registry.active_generation(generation.manifest.identity) is not None
    assert not configuration_path.exists()
    assert not modules_path.exists()
    stored = json.loads(registry.registry_path.read_text(encoding="utf-8"))
    assert {source["id"] for source in stored["sources"]} >= {
        "DemoConfiguration",
        "DemoConfiguration:modules",
    }

    restarted = Registry(registry.data_dir)
    assert restarted.startup() == []
    assert "DemoConfiguration" not in restarted.sources
    assert "DemoConfiguration:modules" not in restarted.sources
    assert restarted.resolve("DemoConfiguration").modules is not None


def test_legacy_источник_вне_managed_data_не_удаляется(
    tmp_path,
):
    external = tmp_path / "external"
    external.mkdir()
    source_path = write_export(
        external,
        build_configuration(name="DemoConfiguration"),
    )
    registry = Registry(tmp_path / "data")
    registry.add_configuration(source_path, keep_source=False)
    registry.save()
    generation = _native_generation(tmp_path, "external")

    registry.publish_generation(
        registry.stage_generation(generation.manifest, generation.payloads)
    )

    assert source_path.is_file()
    assert "DemoConfiguration" not in registry.sources


def test_cleanup_понимает_legacy_строки_без_новых_полей(
    tmp_path,
    архив_кода,
    корень_кода,
):
    registry = _legacy_registry(tmp_path, архив_кода, корень_кода)
    payload = json.loads(registry.registry_path.read_text(encoding="utf-8"))
    for source in payload["sources"]:
        for field in (
            "selection_version",
            "locator_generation",
            "code_version",
            "incomplete",
        ):
            source.pop(field, None)
    registry._write_registry_payload(payload)
    generation = _native_generation(tmp_path, "old-registry")

    registry.publish_generation(
        registry.stage_generation(generation.manifest, generation.payloads)
    )

    stored = json.loads(registry.registry_path.read_text(encoding="utf-8"))
    assert stored["sources"] == []
    assert "DemoConfiguration" not in registry.sources
    assert "DemoConfiguration:modules" not in registry.sources


def test_отказ_до_native_commit_сохраняет_legacy_строки_и_файлы(
    tmp_path,
    архив_кода,
    корень_кода,
    monkeypatch,
):
    registry = _legacy_registry(tmp_path, архив_кода, корень_кода)
    configuration_path = registry._absolute(
        registry.sources["DemoConfiguration"].stored_path
    )
    modules_path = registry._absolute(
        registry.sources["DemoConfiguration:modules"].stored_path
    )
    generation = _native_generation(tmp_path, "failure")

    def fail_runtime(*_args, **_kwargs):
        raise RegistryError("синтетический отказ до commit")

    monkeypatch.setattr(registry, "_build_native_generation_runtime", fail_runtime)
    with pytest.raises(RegistryError, match="отказ до commit"):
        registry.publish_generation(
            registry.stage_generation(generation.manifest, generation.payloads)
        )

    assert registry.active_generation(generation.manifest.identity) is None
    assert configuration_path.is_file()
    assert modules_path.is_dir()
    assert "DemoConfiguration" in registry.sources
    assert "DemoConfiguration:modules" in registry.sources
    assert registry.resolve("DemoConfiguration").modules is not None
