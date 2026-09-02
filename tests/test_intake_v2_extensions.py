"""RED-контракты extension layers и смены native-базы.

Все фикстуры синтетические. Проверки работают только с generation snapshot и
не требуют исходного ZIP после публикации.
"""

from __future__ import annotations

import importlib
import shutil
from dataclasses import replace

import pytest

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
from mcp1c.registry import Registry, RegistryError
from mcp1c.tools import list_extensions
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
        registry.stage_generation(base.manifest, base.payloads)
    )
    registry.publish_generation(
        registry.stage_generation(extension.manifest, extension.payloads)
    )
    extension_pointer = registry.active_generation_pointer(extension.manifest.identity)

    before = registry.resolve("DemoConfiguration", extension="DemoExtension")
    assert before.extension is not None and before.extension.готов
    assert before.extension_roles is not None and before.extension_roles.ready
    assert before.extension_resolution is not None
    assert before.extension_resolution.relations[0].state.value == "resolved"
    extension_status = list_extensions(registry, "DemoConfiguration")
    assert "DemoExtension" in extension_status
    assert "отключ" not in extension_status.casefold()

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
        registry.stage_generation(new_base.manifest, new_base.payloads),
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


def test_extension_publish_требует_существующего_родителя(tmp_path):
    _collection_value, extension = _materialized(
        tmp_path,
        "extension-orphan",
        configuration_name="DemoExtension",
        extension=True,
    )
    registry = Registry(tmp_path / "data")

    with pytest.raises(RegistryError, match="родител"):
        registry.publish_generation(
            registry.stage_generation(extension.manifest, extension.payloads)
        )
    assert registry.active_generation_pointer(extension.manifest.identity) is None


def test_restore_поднимает_native_расширение_после_legacy_родителя(tmp_path):
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
    registry.publish_generation(
        registry.stage_generation(extension.manifest, extension.payloads)
    )

    restarted = Registry(registry.data_dir)
    assert restarted.restore() == []
    restored = restarted.resolve(
        "DemoConfiguration",
        extension="DemoExtension",
    )

    assert restored.extension is not None and restored.extension.готов
    assert restored.extension_roles is not None and restored.extension_roles.ready
    assert restored.extension_resolution is not None
    assert all(
        relation.state.value == "resolved"
        for relation in restored.extension_resolution.relations
    )


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
        registry.stage_generation(base.manifest, base.payloads)
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
