"""RED materialization пяти слоёв из одного source-B collection."""

from __future__ import annotations

import importlib
import hashlib
import json
import shutil

import pytest

from test_module_content import _m3_bsl, _m3_capture_decoder_output, _m3_form_collection

from mcp1c import tools
from mcp1c.intake_v2 import LayerKind, LayerState
from mcp1c.intake_v2_collector import collect_source_b
from mcp1c.intake_v2_converter import convert_collection
from mcp1c.intake_v2_probe import probe_export
from mcp1c.member_pack import INDEX_NAME, PACK_NAME
from mcp1c.intake_v2_registry import load_layer_payload
from mcp1c.registry import Registry
from mcp1c.source_modes import ActivationComponent, ActivationManifest, ActivationMode
from test_intake_v2_collector import MemoryTree, _configuration, _rights, _role
from test_intake_v2_converter import (
    _collection,
    _common_form_xml,
)


SUBJECT = "mcp1c.intake_v2_generation"


def _stage_b_generation(registry, manifest, payloads):
    """Подготовить синтетический Source B bundle с явной активацией."""
    base = payloads[LayerKind.BASE_STRUCTURE]
    base_path = base.manifest_path if hasattr(base, "manifest_path") else base
    version = load_layer_payload(base_path).semantic["version"]
    digest = hashlib.sha256(
        "|".join(
            f"{layer.kind.value}:{layer.payload_sha256}"
            for layer in manifest.layers if layer.state is LayerState.READY
        ).encode("utf-8")
    ).hexdigest()
    previous = registry.active_generation_pointer(manifest.identity)
    activation = ActivationManifest(
        mode=ActivationMode.B_FULL,
        identity_incarnation=manifest.generation_id,
        physical_generation_root_id=manifest.generation_id,
        configuration_version=version,
        main=ActivationComponent(
            source="source-b",
            origin=manifest.origin_name,
            raw_sha256=manifest.raw_sha256,
            payload_sha256=digest,
        ),
        extensions=(),
        expected_previous_activation=(
            previous.activation.sha256 if previous and previous.activation else None
        ),
        transaction_id=f"test-{manifest.generation_id}",
        recovery_id=f"test-recovery-{manifest.generation_id}",
    )
    return registry.stage_generation(manifest, payloads, activation=activation)


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


def _materialized(tmp_path, name: str, collection):
    materialize_generation = _symbol("materialize_generation")
    conversion = convert_collection(collection)
    return materialize_generation(
        collection,
        conversion,
        tmp_path / f"materialized-{name}",
        generation_id=f"generation-{name}",
    )


def _hashes(materialized) -> dict[LayerKind, str]:
    return {
        layer.kind: layer.content_sha256
        for layer in materialized.manifest.layers
    }


def _payload_hashes(materialized) -> dict[LayerKind, str]:
    return {
        layer.kind: layer.payload_sha256
        for layer in materialized.manifest.layers
    }


def test_materializer_строит_пять_слоёв_и_generation_переживает_source(tmp_path):
    load_layer_payload = _symbol("load_layer_payload")
    collection = _collection(
        tmp_path / "source",
        common_forms=True,
        bots=True,
    )
    materialized = _materialized(tmp_path, "001", collection)

    assert {layer.kind for layer in materialized.manifest.layers} == set(LayerKind)
    assert {
        layer.kind: layer.state for layer in materialized.manifest.layers
    } == {kind: LayerState.READY for kind in LayerKind}
    assert all(
        layer.content_sha256 and layer.payload_sha256 and layer.relative_path
        for layer in materialized.manifest.layers
    )
    assert all(
        layer.provenance is not None
        and layer.provenance.raw_sha256 == collection.probe.raw_sha256
        and layer.provenance.origin_name == collection.probe.origin_name
        for layer in materialized.manifest.layers
    )
    code = load_layer_payload(materialized.payloads[LayerKind.CODE].manifest_path)
    forms = load_layer_payload(materialized.payloads[LayerKind.FORMS].manifest_path)
    assert {item["address"] for item in code.semantic["modules"]} >= {
        "Справочник.Items.МодульОбъекта",
        "ОбщаяФорма.Workspace",
        "ОбщаяФорма.Container",
        "ОбщаяФорма.Flat",
        "Бот.Assistant",
    }
    assert any(item["address"] == "ОбщаяФорма.Workspace" for item in forms.semantic["forms"])

    registry = Registry(tmp_path / "data")
    registry.publish_generation(
        _stage_b_generation(registry, materialized.manifest, materialized.payloads)
    )
    pointer = registry.active_generation_pointer(materialized.manifest.identity)
    shutil.rmtree(collection.root)
    shutil.rmtree(materialized.root)

    restarted = Registry(registry.data_dir)
    assert restarted.restore() == []
    assert restarted.active_generation(materialized.manifest.identity) == materialized.manifest
    generation_root = registry.data_dir / pointer.root_path
    assert (generation_root / "members.pack").is_file()
    assert (generation_root / "members.index.json").is_file()
    assert not (generation_root / "payload").exists()


def test_materializer_сохраняет_xdto_как_ленивые_extended_members(
    tmp_path, monkeypatch
):
    load_layer_payload = _symbol("load_layer_payload")
    collection = _collection(tmp_path / "source-xdto", xdto_packages=True)
    materialized = _materialized(tmp_path, "xdto", collection)
    extended = load_layer_payload(
        materialized.payloads[LayerKind.EXTENDED_STRUCTURE].manifest_path
    )

    assert {member.key for member in extended.members} == {
        "ПакетXDTO.Common",
        "ПакетXDTO.Main",
    }
    assert all(
        member.relative_path.startswith("payload/extended_structure/")
        and member.relative_path.endswith(".xml")
        for member in extended.members
    )
    root_payload = next(
        item["payload"]
        for item in extended.semantic["objects"]
        if item["full_name"] == "ПакетXDTO.Main"
    )
    assert "object_types" in root_payload
    assert "property" not in root_payload

    registry = Registry(tmp_path / "data-xdto")
    registry.publish_generation(
        _stage_b_generation(registry, materialized.manifest, materialized.payloads)
    )
    pointer = registry.active_generation_pointer(materialized.manifest.identity)
    shutil.rmtree(collection.root)
    shutil.rmtree(materialized.root)

    restarted = Registry(registry.data_dir)
    assert restarted.restore() == []
    reader = tools.read_package_member

    def reject_eager_read(*_args, **_kwargs):
        pytest.fail("search_objects не должен читать полный XDTO member")

    monkeypatch.setattr(tools, "read_package_member", reject_eager_read)
    found = tools.search_objects(
        restarted,
        "Item",
        config="DemoConfiguration",
    )
    monkeypatch.setattr(tools, "read_package_member", reader)
    card = tools.get_object(
        restarted,
        "ПакетXDTO.Main.ТипОбъекта.Item",
        config="DemoConfiguration",
        detail="full",
    )

    assert "ПакетXDTO.Main.ТипОбъекта.Item" in found
    assert "ПакетXDTO.Main.ТипОбъекта.Item" in card
    assert "Свойства (2)" in card
    assert "`c2:Code` → `ПакетXDTO.Common.ТипЗначения.Code` — межпакетная" in card
    assert "typeDef=anonymous" in card
    assert pointer is not None
    generation_root = registry.data_dir / pointer.root_path
    assert (generation_root / "members.pack").is_file()
    assert (generation_root / "members.index.json").is_file()
    assert not (generation_root / "payload").exists()


def test_generation_pack_с_пустым_member_публикуется_и_читается_после_restart(
    tmp_path,
):
    tree = MemoryTree(
        {
            "Configuration.xml": _configuration(),
            "CommonModules/a/Ext/Module.bsl": (
                b"procedure Ready() export\nendprocedure"
            ),
            "CommonModules/B/Ext/Module.bsl": b"",
        }
    )
    collection = collect_source_b(
        tree,
        probe_export(tree),
        tmp_path / "collection-empty-member",
    )
    materialized = _materialized(tmp_path, "empty-member", collection)
    registry = Registry(tmp_path / "data-empty-member")
    registry.publish_generation(
        _stage_b_generation(registry, materialized.manifest, materialized.payloads)
    )

    restarted = Registry(registry.data_dir)
    assert restarted.restore() == []
    assert "procedure Ready() export" in tools.get_procedure(
        restarted,
        "ОбщийМодуль.a::Ready",
        config="DemoConfiguration",
    )
    assert restarted.modules["DemoConfiguration:modules"].готов


def test_materializer_сохраняет_compiled_модуль_не_выдавая_его_за_bsl(tmp_path):
    load_layer_payload = _symbol("load_layer_payload")
    collection = _collection(
        tmp_path / "source-compiled",
        compiled_module=True,
    )
    materialized = _materialized(tmp_path, "compiled", collection)
    payload = load_layer_payload(
        materialized.payloads[LayerKind.CODE].manifest_path
    )
    semantic = next(
        item
        for item in payload.semantic["modules"]
        if item["address"] == "ОбщийМодуль.Sealed"
    )
    member = next(
        item for item in payload.members if item.key == "ОбщийМодуль.Sealed"
    )

    assert semantic["compiled"] is True
    assert member.relative_path.endswith(".Module")

    registry = Registry(tmp_path / "data-compiled")
    registry.publish_generation(
        _stage_b_generation(registry, materialized.manifest, materialized.payloads)
    )
    loaded = registry.modules["DemoConfiguration:modules"]
    entry = loaded.каталог.entries["ОбщийМодуль.Sealed"]
    assert entry.compiled is True
    assert entry.locator is not None and entry.locator.kind == "compiled"


def test_общий_модуль_без_тела_виден_как_opaque_но_не_как_ошибка(tmp_path):
    load_layer_payload = _symbol("load_layer_payload")
    collection = _collection(
        tmp_path / "source-opaque",
        opaque_common_module=True,
        item_form_xml=_common_form_xml(),
    )
    materialized = _materialized(tmp_path, "opaque", collection)
    payload = load_layer_payload(
        materialized.payloads[LayerKind.CODE].manifest_path
    )

    assert payload.semantic["opaque_modules"] == ["ОбщийМодуль.Sealed"]
    assert all(member.key != "ОбщийМодуль.Sealed" for member in payload.members)

    registry = Registry(tmp_path / "data-opaque")
    registry.publish_generation(
        _stage_b_generation(registry, materialized.manifest, materialized.payloads)
    )
    loaded = registry.modules["DemoConfiguration:modules"]
    entry = loaded.каталог.entries["ОбщийМодуль.Sealed"]
    coverage = tools.sources_snapshot(registry).code[0].coverage

    assert entry.opaque is True
    assert entry.compiled is True
    assert entry.locator is None
    assert coverage is not None
    assert coverage.compiled_without_source == 1
    assert coverage.problems_total == 0
    assert not coverage.has_limitations
    sources = tools.list_configurations(registry)
    object_card = tools.get_object(
        registry,
        "ОбщийМодуль.Sealed",
        config="DemoConfiguration",
        detail="full",
    )
    module = tools.get_procedure(
        registry,
        "ОбщийМодуль.Sealed",
        config="DemoConfiguration",
    )
    state = tools.sources_snapshot(registry).code[0].state
    assert f"модулей {coverage.modules_total}" in state
    assert state in sources
    assert "недоступным исходным текстом 1" in sources
    assert "ОбщийМодуль.Sealed" in object_card
    assert "исходный текст недоступен" in object_card
    assert "Модулей нет" not in object_card
    assert "исходный текст недоступен" in module
    assert "в загруженном коде не найден" not in module
    answer = tools.search_procedures(
        registry,
        "НесуществующаяПроцедура",
        config="DemoConfiguration",
    )
    assert "исходный текст недоступен у 1 модулей" in answer
    assert "пустой результат не доказывает отсутствие кода" in answer
    assert "Покрытие кода неполно" not in answer

    restarted = Registry(registry.data_dir)
    assert restarted.restore() == []
    restored = restarted.modules["DemoConfiguration:modules"]
    assert restored.каталог.entries["ОбщийМодуль.Sealed"].opaque is True


def test_native_generation_считает_пустое_тело_прочитанным(tmp_path):
    collection = _collection(
        tmp_path / "source-empty",
        object_module=b" \r\n\t",
        item_form_xml=_common_form_xml(),
    )
    materialized = _materialized(tmp_path, "empty", collection)
    registry = Registry(tmp_path / "data-empty")
    registry.publish_generation(
        _stage_b_generation(registry, materialized.manifest, materialized.payloads)
    )
    coverage = tools.sources_snapshot(registry).code[0].coverage

    assert coverage is not None
    assert coverage.modules_empty == 1
    assert coverage.modules_unreadable == 0
    assert coverage.problems_total == 0
    assert not coverage.has_limitations


def test_native_generation_оставляет_нечитаемое_тело_ошибкой(tmp_path):
    collection = _collection(
        tmp_path / "source-unreadable",
        object_module=b"\xff",
        item_form_xml=_common_form_xml(),
    )
    materialized = _materialized(tmp_path, "unreadable", collection)
    registry = Registry(tmp_path / "data-unreadable")
    registry.publish_generation(
        _stage_b_generation(registry, materialized.manifest, materialized.payloads)
    )
    coverage = tools.sources_snapshot(registry).code[0].coverage

    assert coverage is not None
    assert coverage.modules_source_available == 0
    assert coverage.modules_unreadable == 1
    assert dict(coverage.problem_categories) == {"unreadable_body": 1}
    assert coverage.has_limitations


def test_hashes_разделяют_изменение_кода_и_декларации_формы(tmp_path):
    first = _materialized(
        tmp_path,
        "base",
        _collection(tmp_path / "base", common_forms=True),
    )
    changed_code = _materialized(
        tmp_path,
        "code",
        _collection(
            tmp_path / "code",
            common_forms=True,
            common_form_module=(
                "Процедура Other()\nКонецПроцедуры\n"
            ).encode(),
        ),
    )
    xml = _common_form_xml().replace(b'name="Filter"', b'name="ChangedFilter"')
    changed_form = _materialized(
        tmp_path,
        "form",
        _collection(
            tmp_path / "form",
            common_forms=True,
            common_form_xml=xml,
        ),
    )

    base_hashes = _hashes(first)
    code_changed = {
        kind for kind in LayerKind if _hashes(changed_code)[kind] != base_hashes[kind]
    }
    form_changed = {
        kind for kind in LayerKind if _hashes(changed_form)[kind] != base_hashes[kind]
    }
    assert code_changed == {LayerKind.CODE}
    assert form_changed == {LayerKind.FORMS}


def test_form_semantic_hash_не_зависит_от_xml_whitespace_а_payload_hash_видит_байты(
    tmp_path,
):
    plain_xml = _common_form_xml()
    spaced_xml = plain_xml.replace(b"><", b">\n  <")
    plain = _materialized(
        tmp_path,
        "plain",
        _collection(
            tmp_path / "plain",
            common_forms=True,
            common_form_xml=plain_xml,
        ),
    )
    spaced = _materialized(
        tmp_path,
        "spaced",
        _collection(
            tmp_path / "spaced",
            common_forms=True,
            common_form_xml=spaced_xml,
        ),
    )

    assert _hashes(plain)[LayerKind.FORMS] == _hashes(spaced)[LayerKind.FORMS]
    assert (
        _payload_hashes(plain)[LayerKind.FORMS]
        != _payload_hashes(spaced)[LayerKind.FORMS]
    )


def _role_collection(tmp_path, value: str):
    tree = MemoryTree(
        {
            "Configuration.xml": _configuration(),
            "Roles/Reader.xml": _role("Reader"),
            "Roles/Reader/Ext/Rights.xml": _rights(value=value),
        }
    )
    return collect_source_b(tree, probe_export(tree), tmp_path)


def test_role_change_меняет_только_roles_semantic_hash(tmp_path):
    allowed = _materialized(
        tmp_path,
        "role-true",
        _role_collection(tmp_path / "role-true", "true"),
    )
    denied = _materialized(
        tmp_path,
        "role-false",
        _role_collection(tmp_path / "role-false", "false"),
    )

    changed = {
        kind
        for kind in LayerKind
        if _hashes(allowed)[kind] != _hashes(denied)[kind]
    }
    assert changed == {LayerKind.ROLES}


def test_role_error_не_отменяет_остальные_готовые_слои(tmp_path):
    tree = MemoryTree(
        {
            "Configuration.xml": _configuration(),
            "Roles/Reader/Ext/Rights.xml": _rights(),
        }
    )
    collection = collect_source_b(tree, probe_export(tree), tmp_path / "source")
    materialized = _materialized(tmp_path, "role-error", collection)

    assert {
        layer.kind: layer.state for layer in materialized.manifest.layers
    } == {
        LayerKind.BASE_STRUCTURE: LayerState.READY,
        LayerKind.EXTENDED_STRUCTURE: LayerState.READY,
        LayerKind.FORMS: LayerState.READY,
        LayerKind.CODE: LayerState.READY,
        LayerKind.ROLES: LayerState.ERROR,
    }
    assert LayerKind.ROLES not in materialized.payloads
    roles = next(
        layer
        for layer in materialized.manifest.layers
        if layer.kind is LayerKind.ROLES
    )
    assert roles.provenance is not None
    assert roles.provenance.raw_sha256 == collection.probe.raw_sha256

    registry = Registry(tmp_path / "data-error")
    registry.publish_generation(
        _stage_b_generation(registry, materialized.manifest, materialized.payloads)
    )
    assert registry.active_generation(materialized.manifest.identity) == materialized.manifest


def test_tampered_member_изолируется_при_restore_даже_при_целом_layer_manifest(tmp_path):
    collection = _collection(tmp_path / "source", common_forms=True)
    materialized = _materialized(tmp_path, "tamper", collection)
    registry = Registry(tmp_path / "data")
    registry.publish_generation(
        _stage_b_generation(registry, materialized.manifest, materialized.payloads)
    )
    pointer = registry.active_generation_pointer(materialized.manifest.identity)
    raw_pointer = json.loads(registry.registry_path.read_text(encoding="utf-8"))[
        "generation_manifests"
    ][0]
    code_manifest = json.loads(
        (registry.data_dir / pointer.root_path / "layers/code.json").read_text(
            encoding="utf-8"
        )
    )
    member = code_manifest["members"][0]["relative_path"]
    root = registry.data_dir / pointer.root_path
    index = json.loads((root / INDEX_NAME).read_text(encoding="utf-8"))
    entry = next(
        item for item in index["entries"] if item["relative_path"] == member
    )
    with (root / PACK_NAME).open("r+b") as stream:
        stream.seek(entry["offset"])
        original = stream.read(1)
        stream.seek(entry["offset"])
        stream.write(bytes((original[0] ^ 0xFF,)))

    restarted = Registry(registry.data_dir)
    messages = restarted.restore()

    assert any("member" in message and "контрольная сумма" in message
               for message in messages), messages
    identity = materialized.manifest.identity
    assert restarted.active_generation_pointer(identity) is None
    assert restarted.active_generation(identity) is None
    snapshot = restarted.snapshot()
    assert identity.configuration_name not in snapshot.configurations
    assert not any(key.startswith(identity.configuration_name + ":") for key in snapshot.modules)
    restarted.save()
    assert raw_pointer in json.loads(registry.registry_path.read_text(encoding="utf-8"))[
        "generation_manifests"
    ]




@pytest.mark.parametrize("size", [883_773, 883_774, 883_773 + 65_536])
def test_generation_не_публикует_слишком_большой_embedded_module(tmp_path, monkeypatch, size):
    collection = _m3_form_collection(tmp_path / "source", form=b"{19}", module=_m3_bsl(size))
    decoders = _m3_capture_decoder_output(monkeypatch)

    materialized = _materialized(tmp_path, "form-module-budget", collection)

    code = load_layer_payload(materialized.payloads[LayerKind.CODE].manifest_path)
    forms = load_layer_payload(materialized.payloads[LayerKind.FORMS].manifest_path)
    form = next(item for item in forms.semantic["forms"] if item["address"] == "ОбщаяФорма.Budget")
    if size <= 883_773:
        member = next(member for member in code.members if member.key == "ОбщаяФорма.Budget")
        assert member.size == size
        assert form["structure_state"] == "deferred"
    else:
        assert all(member.key != "ОбщаяФорма.Budget" for member in code.members)
        assert all(item["address"] != "ОбщаяФорма.Budget" for item in code.semantic["modules"])
        assert form["structure_state"] == "unreadable"

    assert decoders
    assert all(decoder.produced <= 883_774 for decoder in decoders)


def _m7_generation(tmp_path):
    collection = _collection(tmp_path / 'source-m7')
    materialized = _materialized(tmp_path, 'm7-core', collection)
    registry = Registry(tmp_path / 'data')
    return registry, materialized


def _m7_bytes(monkeypatch):
    from threading import local
    from mcp1c import member_pack
    from mcp1c.intake_v2_registry import GenerationBundleStore

    counts = {'copied': 0, 'verify_packed': 0}
    scope = local()
    real_verify = GenerationBundleStore.verify
    real_read = member_pack._PackedReader.read
    real_copy = member_pack.MemberPackWriter.copy

    def verify(store, *args, **kwargs):
        scope.depth = getattr(scope, 'depth', 0) + 1
        try:
            return real_verify(store, *args, **kwargs)
        finally:
            scope.depth -= 1

    def packed_read(reader, *args, **kwargs):
        block = real_read(reader, *args, **kwargs)
        if getattr(scope, 'depth', 0):
            counts['verify_packed'] += len(block)
        return block

    class CountedSource:
        def __init__(self, stream):
            self.stream = stream

        def read(self, *args, **kwargs):
            block = self.stream.read(*args, **kwargs)
            counts['copied'] += len(block)
            return block

        def __getattr__(self, name):
            return getattr(self.stream, name)

    def copy(writer, relative_path, source, **kwargs):
        return real_copy(writer, relative_path, CountedSource(source), **kwargs)

    monkeypatch.setattr(GenerationBundleStore, 'verify', verify)
    monkeypatch.setattr(member_pack._PackedReader, 'read', packed_read)
    monkeypatch.setattr(member_pack.MemberPackWriter, 'copy', copy)
    return counts


def _m7_published(tmp_path):
    registry, materialized = _m7_generation(tmp_path)
    pointer = registry.publish_generation(_stage_b_generation(
        registry, materialized.manifest, materialized.payloads,
    ))
    root = registry.data_dir / pointer.root_path
    assert (root / PACK_NAME).stat().st_size > 0
    assert (root / '.verified').is_file(), 'Успешная публикация на обычной ФС должна сохранить verification cache'
    assert (root / '.cache-key').is_file()
    return registry, materialized, pointer, root


def _m7_remove_marker(root):
    (root / '.verified').unlink(missing_ok=True)
    (root / '.cache-key').unlink(missing_ok=True)


def test_m7_stage_proof_копирует_members_один_раз_и_переживает_promote(tmp_path, monkeypatch):
    registry, materialized = _m7_generation(tmp_path)
    counts = _m7_bytes(monkeypatch)
    staged = _stage_b_generation(registry, materialized.manifest, materialized.payloads)
    pack_bytes = (staged.root / PACK_NAME).stat().st_size
    assert pack_bytes > 0
    assert getattr(staged, 'verified', False) is True
    assert counts['copied'] == pack_bytes
    assert counts['verify_packed'] == 0

    pointer = registry.publish_generation(staged)
    root = registry.data_dir / pointer.root_path
    assert (root / '.verified').is_file()
    assert (root / '.cache-key').is_file()
    assert registry._generation_store.verify(pointer) == materialized.manifest
    assert counts['copied'] == pack_bytes
    assert counts['verify_packed'] == 0


def test_m7_warm_restore_поднимает_active_pointer_без_повторного_hash_pack(tmp_path, monkeypatch):
    registry, materialized, pointer, root = _m7_published(tmp_path)
    assert (root / '.verified').is_file()
    counts = _m7_bytes(monkeypatch)
    restarted = Registry(registry.data_dir)

    assert restarted.restore() == []

    active = restarted.active_generation_pointer(materialized.manifest.identity)
    assert active is not None and active.generation_id == pointer.generation_id
    assert materialized.manifest.identity.configuration_name in restarted.snapshot().configurations
    assert counts['verify_packed'] == 0


@pytest.mark.parametrize('damage', ['pack', 'pack-reset-mtime', 'index', 'ready-envelope', 'manifest'])
def test_m7_старый_marker_не_разрешает_повреждённое_поколение(tmp_path, monkeypatch, damage):
    import os
    from mcp1c.intake_v2_registry import BundleStoreError

    registry, materialized, pointer, root = _m7_published(tmp_path)
    marker = root / '.verified'
    marker_before = marker.read_bytes()
    raw_pointer = json.loads(registry.registry_path.read_text(encoding='utf-8'))['generation_manifests'][0]
    index_path = root / INDEX_NAME
    index = json.loads(index_path.read_text(encoding='utf-8'))
    entry = next(item for item in index['entries'] if item['size'] > 0)
    if damage.startswith('pack'):
        pack = root / PACK_NAME
        before = pack.stat()
        with pack.open('r+b') as stream:
            stream.seek(entry['offset'])
            original = stream.read(1)
            assert len(original) == 1
            stream.seek(entry['offset'])
            stream.write(bytes([original[0] ^ 0xFF]))
        if damage == 'pack-reset-mtime':
            os.utime(pack, ns=(before.st_atime_ns, before.st_mtime_ns))
            after = pack.stat()
            assert after.st_size == before.st_size
            assert after.st_dev == before.st_dev and after.st_ino == before.st_ino
            assert after.st_mtime_ns == before.st_mtime_ns
            assert after.st_ctime_ns != before.st_ctime_ns
    elif damage == 'index':
        entry['offset'] = (root / PACK_NAME).stat().st_size + 1
        index_path.write_text(json.dumps(index, sort_keys=True, separators=(',', ':')) + '\n', encoding='utf-8')
    elif damage == 'ready-envelope':
        layer = next(layer for layer in materialized.manifest.layers
                     if layer.kind is LayerKind.CODE and layer.state is LayerState.READY)
        envelope = root / layer.relative_path
        payload = json.loads(envelope.read_text(encoding='utf-8'))
        payload['m7_tampered'] = True
        envelope.write_text(json.dumps(payload), encoding='utf-8')
    else:
        manifest_path = root / 'manifest.json'
        manifest_path.write_bytes(manifest_path.read_bytes() + b'\n')
    counts = _m7_bytes(monkeypatch)

    with pytest.raises(BundleStoreError):
        registry._generation_store.verify(pointer)
    if damage.startswith('pack'):
        assert counts['verify_packed'] > 0
    assert marker.read_bytes() == marker_before
    restarted = Registry(registry.data_dir)
    messages = restarted.restore()
    assert messages
    identity = materialized.manifest.identity
    assert restarted.active_generation_pointer(identity) is None
    assert restarted.active_generation(identity) is None
    assert identity.configuration_name not in restarted.snapshot().configurations
    restarted.save()
    assert raw_pointer in json.loads(registry.registry_path.read_text(encoding='utf-8'))['generation_manifests']


@pytest.mark.parametrize('damage', ['missing', 'broken', 'hmac', 'version', 'directory', 'symlink', 'oversized', 'deep-header'])
def test_m7_негодный_marker_даёт_cache_miss_с_настоящей_проверкой_pack(tmp_path, monkeypatch, damage):
    registry, materialized, pointer, root = _m7_published(tmp_path)
    marker = root / '.verified'
    raw = marker.read_bytes()
    marker.unlink()
    outside = tmp_path / 'outside-marker'
    if damage == 'broken':
        marker.write_bytes(b'broken marker')
    elif damage in {'hmac', 'version'}:
        header, mac, blob = raw.split(b'\n', 2)
        if damage == 'hmac':
            mac = (b'0' if mac[:1] != b'0' else b'1') + mac[1:]
        else:
            stamp = json.loads(header)
            stamp['cache_version'] = -1
            header = json.dumps(stamp).encode()
        marker.write_bytes(header + b'\n' + mac + b'\n' + blob)
    elif damage == 'directory':
        marker.mkdir()
    elif damage == 'symlink':
        outside.write_bytes(raw)
        marker.symlink_to(outside)
    elif damage == 'oversized':
        marker.write_bytes(b'x' * (1024 * 1024 + 1))
    elif damage == 'deep-header':
        marker.write_bytes(b'[' * 2000 + b'0' + b']' * 2000 + b'\n' + b'0' * 64 + b'\nx')
    counts = _m7_bytes(monkeypatch)

    assert registry._generation_store.verify(pointer) == materialized.manifest

    assert counts['verify_packed'] > 0
    if damage == 'symlink':
        assert marker.is_symlink()
        assert outside.read_bytes() == raw
    if damage == 'directory':
        assert marker.is_dir()
    if damage == 'missing':
        assert not marker.exists()


def test_m7_verify_и_direct_restore_не_создают_marker_или_key(tmp_path, monkeypatch):
    registry, materialized, pointer, root = _m7_published(tmp_path)
    _m7_remove_marker(root)
    counts = _m7_bytes(monkeypatch)

    assert registry._generation_store.verify(pointer) == materialized.manifest
    assert not (root / '.verified').exists() and not (root / '.cache-key').exists()
    restarted = Registry(registry.data_dir)
    assert restarted.restore() == []
    assert restarted.active_generation_pointer(materialized.manifest.identity) is not None
    assert counts['verify_packed'] > 0
    assert not (root / '.verified').exists() and not (root / '.cache-key').exists()


def test_m7_use_cache_false_принудительно_проверяет_pack_без_перезаписи_marker(tmp_path, monkeypatch):
    registry, materialized, pointer, root = _m7_published(tmp_path)
    marker_before = (root / '.verified').read_bytes()
    counts = _m7_bytes(monkeypatch)

    assert registry._generation_store.verify(pointer, use_cache=False) == materialized.manifest

    assert counts['verify_packed'] > 0
    assert (root / '.verified').read_bytes() == marker_before


def test_m7_stage_proof_не_публикует_изменённый_после_copy_pack(tmp_path):
    from mcp1c.intake_v2_registry import BundleStoreError

    registry, materialized = _m7_generation(tmp_path)
    staged = _stage_b_generation(registry, materialized.manifest, materialized.payloads)
    assert getattr(staged, 'verified', False) is True
    index = json.loads((staged.root / INDEX_NAME).read_text(encoding='utf-8'))
    entry = next(item for item in index['entries'] if item['size'] > 0)
    with (staged.root / PACK_NAME).open('r+b') as stream:
        stream.seek(entry['offset'])
        original = stream.read(1)
        assert len(original) == 1
        stream.seek(entry['offset'])
        stream.write(bytes([original[0] ^ 0xFF]))

    with pytest.raises(BundleStoreError):
        registry.publish_generation(staged)
    assert registry.active_generation_pointer(materialized.manifest.identity) is None


def test_m7_owner_startup_создаёт_marker_после_полной_проверки(tmp_path, monkeypatch):
    registry, materialized, _pointer, root = _m7_published(tmp_path)
    _m7_remove_marker(root)
    counts = _m7_bytes(monkeypatch)
    restarted = Registry(registry.data_dir)

    restarted.startup()

    assert restarted.role == 'owner'
    assert restarted.active_generation_pointer(materialized.manifest.identity) is not None
    assert counts['verify_packed'] > 0
    assert (root / '.verified').is_file() and (root / '.cache-key').is_file()


def test_m7_guest_startup_проверяет_pack_но_не_создаёт_marker_key(tmp_path, monkeypatch):
    from test_registry_interprocess import _start_server, _stop

    registry, materialized, _pointer, root = _m7_published(tmp_path)
    owner = _start_server(registry.data_dir)
    try:
        _m7_remove_marker(root)
        counts = _m7_bytes(monkeypatch)
        guest = Registry(registry.data_dir)
        guest.startup()
        assert guest.role == 'guest'
        assert guest.active_generation_pointer(materialized.manifest.identity) is not None
        assert counts['verify_packed'] > 0
        assert not (root / '.verified').exists() and not (root / '.cache-key').exists()
    finally:
        _stop(owner)


def test_m7_невозможность_записи_marker_не_ломает_stage_publish(tmp_path, monkeypatch):
    from mcp1c import index_cache

    registry, materialized = _m7_generation(tmp_path)
    real_save = index_cache.save_blob

    def unavailable(payload, path, **kwargs):
        if kwargs.get('kind') == 'generation-verification-v1':
            return None
        return real_save(payload, path, **kwargs)

    monkeypatch.setattr(index_cache, 'save_blob', unavailable)
    staged = _stage_b_generation(registry, materialized.manifest, materialized.payloads)
    assert getattr(staged, 'verified', False) is True
    pointer = registry.publish_generation(staged)
    root = registry.data_dir / pointer.root_path
    assert not (root / '.verified').exists()
    assert registry._generation_store.verify(pointer) == materialized.manifest
