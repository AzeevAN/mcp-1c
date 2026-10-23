"""RED-контракты extension layers и смены native-базы.

Все фикстуры синтетические. Проверки работают только с generation snapshot и
не требуют исходного ZIP после публикации.
"""

from __future__ import annotations

import importlib
import shutil
from dataclasses import replace
from hashlib import sha256

import pytest

from mcp1c import coverage_log, index_cache
from mcp1c.intake_v2 import ExportIdentity, LayerKind
from mcp1c.intake_v2_collector import collect_source_b
from mcp1c.intake_v2_converter import (
    ExtendedStructure,
    StructureConversion,
    base_layer_data,
    convert_collection,
)
from mcp1c.intake_v2_generation import MaterializedGeneration, materialize_generation
from mcp1c.intake_v2_registry import hash_layer_semantic
from mcp1c.intake_v2_planner import IntakeAction, plan_intake
from mcp1c.intake_v2_probe import probe_export
from mcp1c.model import Configuration, Field, MetadataObject
from mcp1c.registry import KIND_EXTENSION, Registry, RegistryError
from mcp1c.source_modes import ActivationComponent, ActivationManifest, ActivationMode
from mcp1c.tools import sources_snapshot
from conftest import write_export
from test_intake_v2_converter import (
    MemoryTree,
    _catalog,
    _collection,
    _configuration as _configuration_xml,
)


SUBJECT = "mcp1c.intake_v2_extensions"


def _symbol(name: str):
    try:
        module = importlib.import_module(SUBJECT)
    except ModuleNotFoundError as error:
        if error.name != SUBJECT:
            raise
        pytest.fail(f"RED: отсутствует модуль {SUBJECT} для контракта {name}")
    if not hasattr(module, name):
        pytest.fail(f"RED: в {SUBJECT} отсутствует контракт {name}")
    return getattr(module, name)


def _object(name: str, *fields: str) -> MetadataObject:
    return MetadataObject(
        full_name=f"Справочник.{name}",
        kind="Справочник",
        name=name,
        attributes=[Field(field_name, types=["Строка"]) for field_name in fields],
    )


def _configuration(name: str, *objects: MetadataObject) -> Configuration:
    return Configuration(
        name=name,
        version="1.0",
        platform="Version8_3_24",
        source_format="source-b",
        objects={item.full_name: item for item in objects},
    )


def test_resolver_объединяет_собственные_объекты_и_доказанный_overlay():
    ExtensionStructure = _symbol("ExtensionStructure")
    resolve_extension_structure = _symbol("resolve_extension_structure")
    base = _configuration("DemoConfiguration", _object("Items", "BaseField"))
    extension = ExtensionStructure(
        name="DemoExtension",
        parent_configuration="DemoConfiguration",
        own_objects={"Справочник.Own": _object("Own", "OwnField")},
        borrowed_overlays={
            "Справочник.Items": _object("Items", "BaseField", "ExtensionField")
        },
    )

    resolved = resolve_extension_structure(base, extension)

    assert set(resolved.configuration.objects) == {
        "Справочник.Items",
        "Справочник.Own",
    }
    assert [
        item.name
        for item in resolved.configuration.objects["Справочник.Items"].attributes
        if not item.standard
    ] == ["BaseField", "ExtensionField"]
    assert [(item.target, item.state.value) for item in resolved.relations] == [
        ("Справочник.Items", "resolved")
    ]
    # Resolver не изменяет опубликованные слои на месте.
    assert [item.name for item in base.objects["Справочник.Items"].attributes] == [
        "BaseField"
    ]


def test_target_missing_исключает_orphan_overlay_но_сохраняет_свой_объект():
    ExtensionStructure = _symbol("ExtensionStructure")
    resolve_extension_structure = _symbol("resolve_extension_structure")
    extension = ExtensionStructure(
        name="DemoExtension",
        parent_configuration="DemoConfiguration",
        own_objects={"Справочник.Own": _object("Own", "OwnField")},
        borrowed_overlays={
            "Справочник.Removed": _object("Removed", "ExtensionField")
        },
    )

    resolved = resolve_extension_structure(
        _configuration("DemoConfiguration"), extension
    )

    assert set(resolved.configuration.objects) == {"Справочник.Own"}
    assert [(item.extension, item.target, item.state.value) for item in resolved.relations] == [
        ("DemoExtension", "Справочник.Removed", "target_missing")
    ]
    assert not hasattr(resolved, "active")
    assert not hasattr(resolved, "disabled")


def test_resolver_заменяет_проекцию_собственного_объекта_из_schema_v1():
    ExtensionStructure = _symbol("ExtensionStructure")
    resolve_extension_structure = _symbol("resolve_extension_structure")
    projected = _object("Own", "Field")
    projected.props["periodicity"] = "В пределах дня"
    base = _configuration("DemoConfiguration", projected)
    base.source_format = "json"
    native = _object("Own", "Field")
    native.props["periodicity"] = "WithinDay"
    extension = ExtensionStructure(
        name="DemoExtension",
        parent_configuration="DemoConfiguration",
        own_objects={native.full_name: native},
        borrowed_overlays={},
    )

    resolved = resolve_extension_structure(base, extension)

    assert resolved.configuration.objects[native.full_name].props == {
        "periodicity": "WithinDay"
    }
    assert base.objects[projected.full_name].props == {
        "periodicity": "В пределах дня"
    }


def test_resolver_отклоняет_рассинхрон_полей_собственного_объекта_schema_v1():
    ExtensionStructure = _symbol("ExtensionStructure")
    ExtensionResolutionError = _symbol("ExtensionResolutionError")
    resolve_extension_structure = _symbol("resolve_extension_structure")
    base = _configuration("DemoConfiguration", _object("Own", "OldField"))
    base.source_format = "xml"
    native = _object("Own", "NewField")
    extension = ExtensionStructure(
        name="DemoExtension",
        parent_configuration="DemoConfiguration",
        own_objects={native.full_name: native},
        borrowed_overlays={},
    )

    with pytest.raises(ExtensionResolutionError, match="набор полей"):
        resolve_extension_structure(base, extension)


def test_resolver_не_маскирует_дубль_собственного_объекта_native_базы():
    ExtensionStructure = _symbol("ExtensionStructure")
    ExtensionResolutionError = _symbol("ExtensionResolutionError")
    resolve_extension_structure = _symbol("resolve_extension_structure")
    own = _object("Own", "Field")
    extension = ExtensionStructure(
        name="DemoExtension",
        parent_configuration="DemoConfiguration",
        own_objects={own.full_name: own},
        borrowed_overlays={},
    )

    with pytest.raises(ExtensionResolutionError, match="конфликтует с базой"):
        resolve_extension_structure(
            _configuration("DemoConfiguration", _object("Own", "Field")),
            extension,
        )


def test_исчезнувшее_заимствованное_поле_не_становится_собственным_overlay():
    ExtensionStructure = _symbol("ExtensionStructure")
    resolve_extension_structure = _symbol("resolve_extension_structure")
    extension = ExtensionStructure(
        name="DemoExtension",
        parent_configuration="DemoConfiguration",
        own_objects={},
        borrowed_overlays={
            "Справочник.Items": _object(
                "Items",
                "RemovedBaseField",
                "ExtensionField",
            )
        },
        borrowed_field_targets=("Справочник.Items.RemovedBaseField",),
    )

    resolved = resolve_extension_structure(
        _configuration("DemoConfiguration", _object("Items")),
        extension,
    )

    assert [
        item.name
        for item in resolved.configuration.objects["Справочник.Items"].attributes
        if not item.standard
    ] == ["ExtensionField"]
    assert [(item.target, item.state.value) for item in resolved.relations] == [
        ("Справочник.Items", "resolved"),
        ("Справочник.Items.RemovedBaseField", "target_missing"),
    ]


def test_resolver_отклоняет_чужого_родителя_до_объединения():
    ExtensionResolutionError = _symbol("ExtensionResolutionError")
    ExtensionStructure = _symbol("ExtensionStructure")
    resolve_extension_structure = _symbol("resolve_extension_structure")
    extension = ExtensionStructure(
        name="DemoExtension",
        parent_configuration="OtherConfiguration",
        own_objects={},
        borrowed_overlays={},
    )

    with pytest.raises(ExtensionResolutionError, match="родител"):
        resolve_extension_structure(
            _configuration("DemoConfiguration"), extension
        )


def _real_extension(tmp_path):
    descriptor = _configuration_xml().replace(
        b"<Name>DemoConfiguration</Name>",
        b"<Name>DemoExtension</Name>",
    ).replace(
        b"<CompatibilityMode>Version8_3_24</CompatibilityMode>",
        b"<NamePrefix>Demo_</NamePrefix>"
        b"<ObjectBelonging>Adopted</ObjectBelonging>"
        b"<ConfigurationExtensionPurpose>AddOn</ConfigurationExtensionPurpose>",
    ).replace(
        b"<Catalog>Items</Catalog>",
        b"<Catalog>Items</Catalog><Catalog>Own</Catalog>",
    )
    adopted = _catalog().replace(
        b"<Name>Items</Name>",
        b"<Name>Items</Name><ObjectBelonging>Adopted</ObjectBelonging>",
        1,
    ).replace(
        b"<Name>Title</Name>",
        b"<Name>Title</Name><ObjectBelonging>Adopted</ObjectBelonging>",
        1,
    )
    own = _catalog().replace(b"Items", b"Own")
    tree = MemoryTree(
        {
            "Configuration.xml": descriptor,
            "Catalogs/Items.xml": adopted,
            "Catalogs/Items/Ext/ObjectModule.bsl": b"procedure Overlay() endprocedure",
            "Catalogs/Own.xml": own,
            "Catalogs/Own/Ext/ObjectModule.bsl": b"procedure Own() endprocedure",
        }
    )
    return collect_source_b(
        tree,
        probe_export(tree),
        tmp_path / "real-extension-collection",
    )


def test_converter_сохраняет_borrowed_edges_и_собственные_объекты_из_xml(
    tmp_path,
):
    collection = _real_extension(tmp_path)

    converted = convert_collection(collection)
    structure = converted.extended.extension_structure

    assert structure is not None
    assert tuple(structure.borrowed_overlays) == ("Справочник.Items",)
    assert structure.borrowed_field_targets == ("Справочник.Items.Title",)
    assert tuple(structure.own_objects) == ("Справочник.Own",)
    materialized = materialize_generation(
        collection,
        converted,
        tmp_path / "real-extension-generation",
        generation_id="extension-real",
        parent_configuration="DemoConfiguration",
    )
    assert materialized.manifest.identity == ExportIdentity.extension(
        "DemoExtension",
        parent_configuration="DemoConfiguration",
    )


def test_первое_полное_расширение_разрешено_без_active_generation(tmp_path):
    collection = _real_extension(tmp_path)
    converted = convert_collection(collection)
    materialized = materialize_generation(
        collection,
        converted,
        tmp_path / "extension-for-plan",
        generation_id="extension-plan",
        parent_configuration="DemoConfiguration",
    )

    plan = plan_intake(
        IntakeAction.UPDATE_FULL,
        materialized.manifest,
        active=None,
    )

    assert plan.identity.source_kind.value == "extension"
    assert plan.applied_layers == set(LayerKind)
    assert plan.no_op is False


def _materialized(
    tmp_path,
    generation_id: str,
    *,
    configuration_name: str = "DemoConfiguration",
    remove_items: bool = False,
    extension: bool = False,
) -> tuple[object, MaterializedGeneration]:
    collection = _collection(tmp_path / f"source-{generation_id}")
    converted = convert_collection(collection)
    converted.base.name = configuration_name
    if remove_items:
        converted.base.objects.pop("Справочник.Items")
    if extension:
        ExtensionStructure = _symbol("ExtensionStructure")
        own = _object("Own", "OwnField")
        converted.base.objects[own.full_name] = own
        extension_structure = ExtensionStructure(
            name=configuration_name,
            parent_configuration="DemoConfiguration",
            own_objects={own.full_name: own},
            borrowed_overlays={
                "Справочник.Items": converted.base.objects["Справочник.Items"]
            },
        )
        extended = ExtendedStructure(
            converted.extended.objects,
            extension_structure=extension_structure,
        )
    else:
        extended = converted.extended
    converted = StructureConversion(
        base=converted.base,
        extended=extended,
        base_content_sha256=hash_layer_semantic(
            LayerKind.BASE_STRUCTURE,
            base_layer_data(converted.base),
        ),
        extended_content_sha256=hash_layer_semantic(
            LayerKind.EXTENDED_STRUCTURE,
            extended.to_layer_data(),
        ),
        diagnostics=converted.diagnostics,
    )
    materialized = materialize_generation(
        collection,
        converted,
        tmp_path / f"materialized-{generation_id}",
        generation_id=generation_id,
    )
    if extension:
        materialized = MaterializedGeneration(
            materialized.root,
            replace(
                materialized.manifest,
                identity=ExportIdentity.extension(
                    configuration_name,
                    parent_configuration="DemoConfiguration",
                ),
            ),
            materialized.payloads,
        )
    return collection, materialized


def _stage_active_base(registry: Registry, generation: MaterializedGeneration):
    """Явная B_FULL-активация синтетического родителя для runtime-сценариев."""
    manifest = generation.manifest
    digest = sha256(manifest.generation_id.encode()).hexdigest()
    activation = ActivationManifest(
        mode=ActivationMode.B_FULL,
        identity_incarnation=f"inc-{manifest.generation_id}",
        physical_generation_root_id=f"root-{manifest.generation_id}",
        configuration_version="1.0",
        main=ActivationComponent(
            source="source-b",
            origin=manifest.origin_name,
            raw_sha256=manifest.raw_sha256,
            payload_sha256=digest,
        ),
        extensions=(),
        expected_previous_activation=None,
        transaction_id=f"tx-{manifest.generation_id}",
        recovery_id=f"recovery-{manifest.generation_id}",
    )
    return registry.stage_generation(
        manifest, generation.payloads, activation=activation
    )


def test_registry_сохраняет_extension_generation_при_строгой_замене_базы(
    tmp_path,
):
    _base_collection, base = _materialized(tmp_path, "base-001")
    extension_collection, extension = _materialized(
        tmp_path,
        "extension-001",
        configuration_name="DemoExtension",
        extension=True,
    )
    _new_collection, new_base = _materialized(
        tmp_path,
        "base-002",
        remove_items=True,
    )
    registry = Registry(tmp_path / "data")
    registry.publish_generation(
        _stage_active_base(registry, base)
    )
    registry.publish_generation(
        registry.stage_generation(extension.manifest, extension.payloads)
    )
    extension_pointer = registry.active_generation_pointer(extension.manifest.identity)
    extension_loaded = registry.modules["DemoConfiguration:ext:DemoExtension"]
    assert coverage_log.load_current(
        registry.data_dir,
        extension_loaded.source,
        expected=coverage_log.build_payload(extension_loaded),
    ) is not None

    before = registry.resolve("DemoConfiguration", extension="DemoExtension")
    assert before.extension is not None and before.extension.готов
    assert before.extension_roles is not None and before.extension_roles.ready
    assert before.extension_resolution is not None
    assert before.extension_resolution.relations[0].state.value == "resolved"
    assert registry.snapshot().extension_names("DemoConfiguration") == (
        "DemoExtension",
    )

    shutil.rmtree(extension_collection.root)
    impacts = registry.preview_extension_relations(
        new_base.root,
        new_base.manifest,
    )
    assert [
        (item.extension, item.target, item.state.value) for item in impacts
    ] == [
        ("DemoExtension", "Справочник.Items", "target_missing")
    ]

    registry.publish_generation(
        _stage_active_base(registry, new_base),
        expected_previous=registry.active_generation_pointer(base.manifest.identity),
    )

    after = registry.resolve("DemoConfiguration", extension="DemoExtension")
    assert registry.active_generation_pointer(extension.manifest.identity) == (
        extension_pointer
    )
    assert after.configuration.config.get("Справочник.Items") is None
    assert after.extension is not None and after.extension.готов
    assert after.extension_roles is not None and after.extension_roles.ready
    assert after.extension_resolution is not None
    assert [
        (item.extension, item.target, item.state.value)
        for item in after.extension_resolution.relations
    ] == [
        ("DemoExtension", "Справочник.Items", "target_missing")
    ]

    restarted = Registry(registry.data_dir)
    assert restarted.restore() == []
    restored = restarted.resolve(
        "DemoConfiguration",
        extension="DemoExtension",
    )
    assert restored.extension is not None and restored.extension.готов
    assert restored.extension_roles is not None and restored.extension_roles.ready
    assert restored.extension_resolution is not None
    assert restored.extension_resolution.relations[0].state.value == "target_missing"
    assert coverage_log.load_current(
        restarted.data_dir,
        restored.extension.source,
        expected=coverage_log.build_payload(restored.extension),
    ) is not None


def test_дата_принятия_расширения_не_меняет_дату_базы(tmp_path):
    _base_collection, base = _materialized(tmp_path, "dated-base")
    _extension_collection, extension = _materialized(
        tmp_path, "dated-extension",
        configuration_name="DemoExtension",
        extension=True,
    )
    registry = Registry(tmp_path / "data")
    base_pointer = registry.publish_generation(
        _stage_active_base(registry, base), accepted_job_id="base-job"
    )
    registry.acknowledge_generation_load(base.manifest.identity, "base-job")
    extension_pointer = registry.publish_generation(
        registry.stage_generation(extension.manifest, extension.payloads),
        accepted_job_id="extension-job",
    )
    registry.acknowledge_generation_load(extension.manifest.identity, "extension-job")
    assert registry.active_generation_pointer(base.manifest.identity).last_successful_load_at == base_pointer.last_successful_load_at
    assert extension_pointer.last_successful_load_at is not None

    snapshot = sources_snapshot(registry)
    row = next(row for row in snapshot.configurations if row.name == "DemoConfiguration")
    corpus = next(code for code in row.code if code.corpus == "Расширение DemoExtension")
    assert row.last_successful_load_at == base_pointer.last_successful_load_at
    assert corpus.last_successful_load_at == extension_pointer.last_successful_load_at

    restarted = Registry(registry.data_dir)
    assert restarted.restore() == []
    assert restarted.active_generation_pointer(base.manifest.identity).last_successful_load_at == base_pointer.last_successful_load_at
    assert restarted.active_generation_pointer(extension.manifest.identity).last_successful_load_at == extension_pointer.last_successful_load_at

    _next_collection, next_extension = _materialized(
        tmp_path, "dated-extension-next",
        configuration_name="DemoExtension", extension=True,
    )
    next_pointer = restarted.publish_generation(
        restarted.stage_generation(next_extension.manifest, next_extension.payloads),
        expected_previous=restarted.active_generation_pointer(extension.manifest.identity),
        accepted_job_id="extension-next-job",
    )
    restarted.acknowledge_generation_load(extension.manifest.identity, "extension-next-job")
    assert next_pointer.last_successful_load_at > extension_pointer.last_successful_load_at
    assert restarted.active_generation_pointer(base.manifest.identity).last_successful_load_at == base_pointer.last_successful_load_at

    _next_base_collection, next_base = _materialized(tmp_path, "dated-base-next")
    updated_base = restarted.publish_generation(
        _stage_active_base(restarted, next_base),
        expected_previous=restarted.active_generation_pointer(base.manifest.identity),
        accepted_job_id="base-next-job",
    )
    restarted.acknowledge_generation_load(base.manifest.identity, "base-next-job")
    assert updated_base.last_successful_load_at > base_pointer.last_successful_load_at
    assert restarted.active_generation_pointer(extension.manifest.identity).last_successful_load_at == next_pointer.last_successful_load_at


def test_noop_даты_не_пересобирает_разрешение_расширения(tmp_path, monkeypatch):
    import mcp1c.registry as registry_module

    _base_collection, base = _materialized(tmp_path, "cached-base")
    _extension_collection, extension = _materialized(
        tmp_path, "cached-extension",
        configuration_name="DemoExtension",
        extension=True,
    )
    registry = Registry(tmp_path / "data")
    registry.publish_generation(_stage_active_base(registry, base))
    registry.publish_generation(
        registry.stage_generation(extension.manifest, extension.payloads)
    )
    registry.resolve("DemoConfiguration", extension="DemoExtension")
    previous = registry.active_generation_pointer(extension.manifest.identity)

    def unexpected_resolution(*_args, **_kwargs):
        pytest.fail("Дата загрузки пересобрала разрешение расширения")

    monkeypatch.setattr(registry_module, "resolve_extension_structure", unexpected_resolution)
    registry.accept_generation_noop(previous, "extension-noop")
    registry.acknowledge_generation_load(extension.manifest.identity, "extension-noop")
    registry.resolve("DemoConfiguration", extension="DemoExtension")


def test_schema_v1_поверх_native_отключает_расширение_и_не_воскрешает_его_после_restart(
    tmp_path,
):
    _base_collection, base = _materialized(tmp_path, "base-source-a")
    _extension_collection, extension = _materialized(
        tmp_path,
        "extension-source-a",
        configuration_name="DemoExtension",
        extension=True,
    )
    registry = Registry(tmp_path / "data")
    registry.publish_generation(
        _stage_active_base(registry, base)
    )
    registry.publish_generation(
        registry.stage_generation(extension.manifest, extension.payloads)
    )
    extension_pointer = registry.active_generation_pointer(extension.manifest.identity)
    assert extension_pointer is not None
    source = tmp_path / "source-a"
    source.mkdir()
    projected = _object("Own", "OwnField")
    projected.props["periodicity"] = "В пределах дня"

    registry.add_configuration(
        write_export(
            source,
            _configuration(
                "DemoConfiguration",
                _object("Items", "Title"),
                projected,
            ),
        )
    )

    assert registry.active_generation_pointer(extension.manifest.identity) is None
    assert "DemoExtension" not in registry.snapshot().extension_names(
        "DemoConfiguration"
    )
    assert registry.resolve("DemoConfiguration", extension="DemoExtension").extension is None
    assert not (registry.data_dir / extension_pointer.root_path).exists()

    restarted = Registry(registry.data_dir)
    assert restarted.restore() == []
    restored = restarted.resolve("DemoConfiguration", extension="DemoExtension")
    assert restored.configuration.config.source_format == "schema-v1"
    assert restored.extension is None
    assert restarted.active_generation_pointer(extension.manifest.identity) is None


def test_extension_publish_требует_существующего_родителя(tmp_path):
    _collection_value, extension = _materialized(
        tmp_path,
        "extension-orphan",
        configuration_name="DemoExtension",
        extension=True,
    )
    registry = Registry(tmp_path / "data")

    with pytest.raises(RegistryError, match="active B_FULL"):
        registry.publish_generation(
            registry.stage_generation(extension.manifest, extension.payloads)
        )
    assert registry.active_generation_pointer(extension.manifest.identity) is None


def test_native_расширение_отклоняется_после_legacy_родителя(tmp_path):
    incoming = tmp_path / "legacy-parent"
    incoming.mkdir()
    registry = Registry(tmp_path / "data")
    registry.add_configuration(
        write_export(
            incoming,
            _configuration(
                "DemoConfiguration",
                _object("Items", "BaseField"),
            ),
        )
    )
    _collection_value, extension = _materialized(
        tmp_path,
        "extension-with-legacy-parent",
        configuration_name="DemoExtension",
        extension=True,
    )
    with pytest.raises(RegistryError, match="active B_FULL"):
        registry.publish_generation(
            registry.stage_generation(extension.manifest, extension.payloads)
        )
    assert registry.active_generation_pointer(extension.manifest.identity) is None


def test_remove_снимает_native_generation_расширения_без_legacy_source(
    tmp_path,
):
    registry = Registry(tmp_path / "data")
    _base_collection, base = _materialized(tmp_path, "base-parent-remove")
    registry.publish_generation(_stage_active_base(registry, base))
    _collection_value, extension = _materialized(
        tmp_path,
        "extension-remove",
        configuration_name="DemoExtension",
        extension=True,
    )
    registry.publish_generation(
        registry.stage_generation(extension.manifest, extension.payloads)
    )
    pointer = registry.active_generation_pointer(extension.manifest.identity)
    assert pointer is not None
    assert "DemoConfiguration:ext:DemoExtension" not in registry.snapshot().sources
    corpus = next(
        row
        for row in sources_snapshot(registry).code
        if row.source_id == "DemoConfiguration:ext:DemoExtension"
    )
    assert corpus.kind == KIND_EXTENSION
    assert corpus.native_generation is True
    role_cache = registry._cache_path(
        "DemoConfiguration:ext:DemoExtension",
        "roles.sqlite",
    )
    assert role_cache.is_file()

    registry.remove("DemoConfiguration:ext:DemoExtension")

    snapshot = registry.snapshot()
    assert registry.active_generation_pointer(extension.manifest.identity) is None
    assert "DemoExtension" not in snapshot.extension_names("DemoConfiguration")
    assert "DemoConfiguration:ext:DemoExtension" not in snapshot.modules
    assert not (registry.data_dir / pointer.root_path).exists()
    assert not (registry.data_dir / pointer.root_path).parent.exists()
    assert not role_cache.exists()

    restarted = Registry(registry.data_dir)
    assert restarted.restore() == []
    assert "DemoExtension" not in restarted.snapshot().extension_names(
        "DemoConfiguration"
    )


@pytest.mark.parametrize(
    "source_id",
    ("DemoConfiguration", "DemoConfiguration:modules"),
    ids=("configuration", "main-source-b"),
)
def test_remove_native_конфигурации_каскадно_снимает_её_расширения(
    tmp_path,
    source_id,
):
    _base_collection, base = _materialized(tmp_path, "base-remove-cascade")
    _extension_collection, extension = _materialized(
        tmp_path,
        "extension-remove-cascade",
        configuration_name="DemoExtension",
        extension=True,
    )
    registry = Registry(tmp_path / "data")
    base_pointer = registry.publish_generation(
        _stage_active_base(registry, base)
    )
    extension_pointer = registry.publish_generation(
        registry.stage_generation(extension.manifest, extension.payloads)
    )

    registry.remove(source_id)

    snapshot = registry.snapshot()
    assert registry.active_generation_pointer(base.manifest.identity) is None
    assert registry.active_generation_pointer(extension.manifest.identity) is None
    assert "DemoConfiguration" not in snapshot.configurations
    assert "DemoConfiguration:modules" not in snapshot.modules
    assert "DemoConfiguration:ext:DemoExtension" not in snapshot.modules
    assert not (registry.data_dir / base_pointer.root_path).exists()
    assert not (registry.data_dir / extension_pointer.root_path).exists()
    assert not (registry.data_dir / base_pointer.root_path).parent.exists()
    assert not (registry.data_dir / extension_pointer.root_path).parent.exists()

    restarted = Registry(registry.data_dir)
    assert restarted.restore() == []
    assert not restarted.snapshot().configurations
    assert not restarted.snapshot().generations


def test_restore_поднимает_код_native_расширения_из_warm_кэша(
    tmp_path,
    monkeypatch,
):
    _base_collection, base = _materialized(tmp_path, "base-cache")
    _extension_collection, extension = _materialized(
        tmp_path,
        "extension-cache",
        configuration_name="DemoExtension",
        extension=True,
    )
    registry = Registry(tmp_path / "data")
    registry.publish_generation(
        _stage_active_base(registry, base)
    )
    registry.publish_generation(
        registry.stage_generation(extension.manifest, extension.payloads)
    )

    restarted = Registry(registry.data_dir)

    def reject_cold_rebuild(*_args, **_kwargs):
        pytest.fail("код native-расширения обязан подняться из warm-кэша")

    monkeypatch.setattr(
        restarted,
        "_построить_индекс_кода",
        reject_cold_rebuild,
    )

    assert restarted.restore() == []
    restored = restarted.resolve(
        "DemoConfiguration",
        extension="DemoExtension",
    )
    assert restored.extension is not None and restored.extension.готов


def test_startup_sweep_сохраняет_кэши_active_native_расширения(tmp_path):
    _base_collection, base = _materialized(tmp_path, "base-cache-owner")
    _extension_collection, extension = _materialized(
        tmp_path,
        "extension-cache-owner",
        configuration_name="DemoExtension",
        extension=True,
    )
    registry = Registry(tmp_path / "data")
    registry.publish_generation(
        _stage_active_base(registry, base)
    )
    registry.publish_generation(
        registry.stage_generation(extension.manifest, extension.payloads)
    )
    source_id = "DemoConfiguration:ext:DemoExtension"
    expected = {
        registry._cache_path(source_id, kind).name
        for kind in Registry.CACHE_KINDS[KIND_EXTENSION]
    }
    expected.add(registry._cache_path(source_id, "roles.sqlite").name)
    assert all((registry.cache_dir / name).is_file() for name in expected)

    assert expected <= registry._cached_names()
    assert index_cache.sweep(registry.cache_dir, registry._cached_names()) == []


def test_extension_publish_CAS_отклоняет_смену_родителя(tmp_path, monkeypatch):
    _base_collection, base = _materialized(tmp_path, "base-parent")
    _extension_collection, extension = _materialized(
        tmp_path,
        "extension-parent-cas",
        configuration_name="DemoExtension",
        extension=True,
    )
    registry = Registry(tmp_path / "data")
    registry.publish_generation(
        _stage_active_base(registry, base)
    )
    real_build = registry._build_native_generation_runtime

    def change_parent(root, manifest):
        prepared = real_build(root, manifest)
        parent = registry.configurations["DemoConfiguration"]
        registry.configurations["DemoConfiguration"] = replace(parent)
        return prepared

    monkeypatch.setattr(registry, "_build_native_generation_runtime", change_parent)
    with pytest.raises(RegistryError, match="родитель расширения изменился"):
        registry.publish_generation(
            registry.stage_generation(extension.manifest, extension.payloads)
        )
    assert registry.active_generation_pointer(extension.manifest.identity) is None
