"""RED materialization пяти слоёв из одного source-B collection."""

from __future__ import annotations

import importlib
import json
import shutil

import pytest

from mcp1c import tools
from mcp1c.intake_v2 import LayerKind, LayerState
from mcp1c.intake_v2_collector import collect_source_b
from mcp1c.intake_v2_converter import convert_collection
from mcp1c.intake_v2_probe import probe_export
from mcp1c.registry import Registry
from test_intake_v2_collector import MemoryTree, _configuration, _rights, _role
from test_intake_v2_converter import (
    _collection,
    _common_form_xml,
)


SUBJECT = "mcp1c.intake_v2_generation"


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
        registry.stage_generation(materialized.manifest, materialized.payloads)
    )
    pointer = registry.active_generation_pointer(materialized.manifest.identity)
    shutil.rmtree(collection.root)
    shutil.rmtree(materialized.root)

    restarted = Registry(registry.data_dir)
    assert restarted.restore() == []
    assert restarted.active_generation(materialized.manifest.identity) == materialized.manifest
    assert (registry.data_dir / pointer.root_path / "payload/code").is_dir()


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
        registry.stage_generation(materialized.manifest, materialized.payloads)
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
        registry.stage_generation(materialized.manifest, materialized.payloads)
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
        registry.stage_generation(materialized.manifest, materialized.payloads)
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
        registry.stage_generation(materialized.manifest, materialized.payloads)
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
        registry.stage_generation(materialized.manifest, materialized.payloads)
    )
    assert registry.active_generation(materialized.manifest.identity) == materialized.manifest


def test_tampered_member_блокирует_restore_даже_при_целом_layer_manifest(tmp_path):
    BundleStoreError = _symbol("BundleStoreError")
    collection = _collection(tmp_path / "source", common_forms=True)
    materialized = _materialized(tmp_path, "tamper", collection)
    registry = Registry(tmp_path / "data")
    registry.publish_generation(
        registry.stage_generation(materialized.manifest, materialized.payloads)
    )
    pointer = registry.active_generation_pointer(materialized.manifest.identity)
    code_manifest = json.loads(
        (registry.data_dir / pointer.root_path / "layers/code.json").read_text(
            encoding="utf-8"
        )
    )
    member = code_manifest["members"][0]["relative_path"]
    (registry.data_dir / pointer.root_path / member).write_bytes(b"changed")

    with pytest.raises(BundleStoreError, match="member.*контрольная сумма"):
        Registry(registry.data_dir).restore()
