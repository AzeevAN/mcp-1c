"""RED-контракты подключения native generation к runtime Registry."""

from __future__ import annotations

import shutil

import pytest

from mcp1c.intake_v2_converter import convert_collection
from mcp1c.intake_v2_generation import materialize_generation
from mcp1c.registry import Registry, RegistryError
from mcp1c.tools import get_procedure
from test_intake_v2_converter import _collection


def _materialized(tmp_path, name: str, **collection_options):
    collection = _collection(
        tmp_path / f"source-{name}",
        **collection_options,
    )
    generation = materialize_generation(
        collection,
        convert_collection(collection),
        tmp_path / f"materialized-{name}",
        generation_id=f"generation-{name}",
    )
    return collection, generation


def test_native_commit_атомарно_подключает_структуру_код_и_формы(
    tmp_path,
    monkeypatch,
):
    collection, generation = _materialized(
        tmp_path,
        "001",
        common_forms=True,
    )
    registry = Registry(tmp_path / "data")

    registry.publish_generation(
        registry.stage_generation(generation.manifest, generation.payloads)
    )

    context = registry.resolve("DemoConfiguration")
    catalog = context.configuration.config.get("Справочник.Items")
    assert catalog is not None
    assert {field.name: field.types for field in catalog.attributes} == {
        "Title": ["Строка"],
        "Owner": ["Справочник.Items"],
    }
    assert catalog.tabular_parts[0].attributes[0].digits == 15
    assert context.modules is not None and context.modules.готов
    assert "Справочник.Items.МодульОбъекта" in context.modules.оглавление.модули
    assert "ОбщаяФорма.Workspace" in context.modules.формы.модули
    assert "procedure Demo()" in get_procedure(
        registry,
        "Справочник.Items.МодульОбъекта::Demo",
        config="DemoConfiguration",
    )

    pointer = registry.active_generation_pointer(generation.manifest.identity)
    assert pointer is not None
    shutil.rmtree(collection.root)
    shutil.rmtree(generation.root)

    restarted = Registry(registry.data_dir)

    def reject_cold_rebuild(*_args, **_kwargs):
        pytest.fail("неизменённый native runtime обязан подняться из кэша")

    monkeypatch.setattr(
        restarted,
        "_построить_индекс_кода",
        reject_cold_rebuild,
    )
    monkeypatch.setattr("mcp1c.registry.index_configuration", reject_cold_rebuild)
    monkeypatch.setattr("mcp1c.registry.index_fields", reject_cold_rebuild)
    assert restarted.restore() == []
    restored = restarted.resolve("DemoConfiguration")
    assert restored.configuration.config.get("Справочник.Items") is not None
    assert restored.modules is not None and restored.modules.готов
    assert "procedure Demo()" in get_procedure(
        restarted,
        "Справочник.Items.МодульОбъекта::Demo",
        config="DemoConfiguration",
    )
    assert (registry.data_dir / pointer.root_path).is_dir()


def test_native_compiled_модуль_поднимается_из_warm_кэша(
    tmp_path,
    monkeypatch,
):
    _collection_value, generation = _materialized(
        tmp_path,
        "compiled-cache",
        compiled_module=True,
    )
    registry = Registry(tmp_path / "data")
    registry.publish_generation(
        registry.stage_generation(generation.manifest, generation.payloads)
    )
    published = registry.modules["DemoConfiguration:modules"]
    assert published.каталог.entries["ОбщийМодуль.Sealed"].compiled is True

    restarted = Registry(registry.data_dir)

    def reject_cold_rebuild(*_args, **_kwargs):
        pytest.fail("compiled member native generation обязан подняться из кэша")

    monkeypatch.setattr(
        restarted,
        "_построить_индекс_кода",
        reject_cold_rebuild,
    )

    assert restarted.restore() == []
    restored = restarted.modules["DemoConfiguration:modules"]
    assert restored.каталог.entries["ОбщийМодуль.Sealed"].compiled is True


def test_runtime_failure_до_commit_сохраняет_прежний_pointer_и_runtime(
    tmp_path,
    monkeypatch,
):
    _first_collection, first = _materialized(tmp_path, "001")
    _second_collection, second = _materialized(
        tmp_path,
        "002",
        common_forms=True,
    )
    registry = Registry(tmp_path / "data")
    registry.publish_generation(
        registry.stage_generation(first.manifest, first.payloads)
    )
    old_pointer = registry.active_generation_pointer(first.manifest.identity)
    old_configuration = registry.resolve("DemoConfiguration").configuration
    staged = registry.stage_generation(second.manifest, second.payloads)

    def fail_runtime(*_args, **_kwargs):
        raise RegistryError("синтетический отказ runtime")

    monkeypatch.setattr(registry, "_build_native_generation_runtime", fail_runtime)
    with pytest.raises(RegistryError, match="отказ runtime"):
        registry.publish_generation(staged, expected_previous=old_pointer)

    assert registry.active_generation_pointer(first.manifest.identity) == old_pointer
    assert registry.resolve("DemoConfiguration").configuration is old_configuration
    assert not staged.root.exists()


def test_авария_сборки_runtime_оставляет_wal_и_откатывается_после_restart(
    tmp_path,
    monkeypatch,
):
    _first_collection, first = _materialized(tmp_path, "001")
    _second_collection, second = _materialized(
        tmp_path,
        "002",
        common_forms=True,
    )
    registry = Registry(tmp_path / "data")
    registry.publish_generation(
        registry.stage_generation(first.manifest, first.payloads)
    )
    old_pointer = registry.active_generation_pointer(first.manifest.identity)
    staged = registry.stage_generation(second.manifest, second.payloads)

    def crash_runtime(*_args, **_kwargs):
        raise SystemExit("синтетическая авария runtime")

    monkeypatch.setattr(registry, "_build_native_generation_runtime", crash_runtime)
    with pytest.raises(SystemExit, match="авария runtime"):
        registry.publish_generation(staged, expected_previous=old_pointer)

    assert registry.generation_recovery_path.is_file()
    restarted = Registry(registry.data_dir)
    assert restarted.recover_generation_publish() == [
        "generation generation-002: staging откачен"
    ]
    assert restarted.restore() == []
    assert restarted.active_generation_pointer(first.manifest.identity) == old_pointer
    assert restarted.resolve("DemoConfiguration").configuration.config.name == (
        "DemoConfiguration"
    )
    assert not staged.root.exists()


def test_repacked_generation_сохраняет_identity_кэша_структуры_и_кода(
    tmp_path,
    monkeypatch,
):
    _collection_value, first = _materialized(tmp_path, "001", common_forms=True)
    registry = Registry(tmp_path / "data")
    registry.publish_generation(
        registry.stage_generation(first.manifest, first.payloads)
    )
    first_context = registry.resolve("DemoConfiguration")

    second_manifest = type(first.manifest)(
        format_version=first.manifest.format_version,
        generation_id="generation-roles",
        identity=first.manifest.identity,
        parser_version=first.manifest.parser_version,
        selection_version=first.manifest.selection_version,
        source_transport=first.manifest.source_transport,
        origin_name=first.manifest.origin_name,
        raw_sha256="e" * 64,
        layers=first.manifest.layers,
    )
    pointer = registry.active_generation_pointer(first.manifest.identity)
    assert pointer is not None
    payloads = registry.generation_payload_sources(pointer)

    def reject_rebuild(*_args, **_kwargs):
        pytest.fail("перепаковка без semantic change не должна строить код заново")

    monkeypatch.setattr(registry, "_построить_индекс_кода", reject_rebuild)

    registry.publish_generation(
        registry.stage_generation(second_manifest, payloads),
        expected_previous=pointer,
    )
    second_context = registry.resolve("DemoConfiguration")

    assert (
        second_context.configuration.source.sha256
        == first_context.configuration.source.sha256
    )
    assert second_context.modules is not None and first_context.modules is not None
    assert second_context.modules.source.sha256 == first_context.modules.source.sha256
    assert (
        second_context.modules.source.locator_generation
        == first_context.modules.source.locator_generation
    )
