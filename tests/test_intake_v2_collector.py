"""RED-контракты единого потокового source-B collector этапа 4."""

from __future__ import annotations

import hashlib
import importlib
import io
import zipfile
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

from mcp1c.intake_v2 import CandidateTransport, LayerState, MetadataKindPolicy
from mcp1c.intake_v2_probe import probe_export
from mcp1c.intake_v2_transport import ZipExportTree


SUBJECT = "mcp1c.intake_v2_collector"
NS = "http://v8.1c.ru/8.3/MDClasses"


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


def _configuration(name: str = "DemoConfiguration") -> bytes:
    return (
        f'<MetaDataObject xmlns="{NS}"><Configuration><Properties>'
        f"<Name>{name}</Name><Version>1.0</Version>"
        "<CompatibilityMode>Version8_3_21</CompatibilityMode>"
        "</Properties></Configuration></MetaDataObject>"
    ).encode()


def _role(name: str) -> bytes:
    return (
        f'<MetaDataObject xmlns="{NS}" version="2.20"><Role><Properties>'
        f"<Name>{name}</Name><setForNewObjects>false</setForNewObjects>"
        "<setForAttributesByDefault>true</setForAttributesByDefault>"
        "<independentRightsOfChildObjects>false</independentRightsOfChildObjects>"
        "</Properties></Role></MetaDataObject>"
    ).encode()


def _rights(*, value: str = "true") -> bytes:
    return (
        '<Rights xmlns="http://v8.1c.ru/8.2/roles" version="2.20">'
        "<object><name>Catalog.Demo</name><right><name>Read</name>"
        f"<value>{value}</value><restrictionByCondition>"
        "<condition>Allowed = true</condition>"
        "<field>Catalog.Demo.Attribute.Code</field>"
        "</restrictionByCondition></right><right><name>Delete</name>"
        "<value>false</value></right></object>"
        "<template><name>DemoTemplate</name><condition>Allowed</condition>"
        "</template></Rights>"
    ).encode()


class MemoryTree:
    transport = CandidateTransport.INCOMING
    origin_name = "demo-export.zip"

    def __init__(
        self,
        payloads: dict[str, bytes],
        *,
        fail_on: str = "",
        unstable: bool = False,
    ):
        self.payloads = dict(payloads)
        self.fail_on = fail_on
        self.unstable = unstable
        self.open_count: dict[str, int] = {}
        self._fingerprint = self._digest()

    def _digest(self) -> str:
        digest = hashlib.sha256()
        for path in sorted(self.payloads):
            digest.update(path.encode())
            digest.update(self.payloads[path])
        return digest.hexdigest()

    def paths(self) -> tuple[str, ...]:
        return tuple(sorted(self.payloads))

    def open(self, path: str):
        self.open_count[path] = self.open_count.get(path, 0) + 1
        if path == self.fail_on:
            raise RuntimeError("synthetic transport failure")
        return io.BytesIO(self.payloads[path])

    def size(self, path: str) -> int:
        return len(self.payloads[path])

    def fingerprint(self) -> str:
        return self._fingerprint

    def source_sha256(self) -> str:
        return self._digest()

    def verify_stable(self, expected: str) -> bool:
        return not self.unstable and expected == self._fingerprint == self._digest()


def _tree(**extra: bytes) -> MemoryTree:
    payloads = {
        "Configuration.xml": _configuration(),
        "Catalogs/Demo.xml": b"<catalog/>",
        "Catalogs/Demo/Ext/ObjectModule.bsl": b"procedure Demo() endprocedure",
        "Catalogs/Demo/Forms/Main.xml": b"<form-descriptor/>",
        "Catalogs/Demo/Forms/Main/Ext/Form.xml": b"<form/>",
        "Catalogs/Demo/Forms/Main/Ext/Form/Module.bsl": b"procedure Form() endprocedure",
        "Roles/Reader.xml": _role("Reader"),
        "Roles/Reader/Ext/Rights.xml": _rights(),
        "Roles/Empty.xml": _role("Empty"),
        "Roles/Empty/Ext/Rights.xml": (
            b'<Rights xmlns="http://v8.1c.ru/8.2/roles" version="2.20"/>'
        ),
        "Subsystems/Deferred.xml": b"<deferred/>",
        "SettingsStorages/Ignored/Ext/ManagerModule.bsl": b"ignored",
        "ScheduledJobs/Job/Ext/Schedule.xml": b"<ignored-schedule/>",
        "UnknownThings/One.xml": b"<unknown/>",
        "UnknownThings/Two.xml": b"<unknown/>",
        "UnknownThings/Three.xml": b"<unknown/>",
        "UnknownThings/Four.xml": b"<unknown/>",
        "UnknownThings/One/Ext/Module.bsl": b"unknown",
        **extra,
    }
    return MemoryTree(payloads)


def _collect(tree: MemoryTree, target: Path):
    collect_source_b = _symbol("collect_source_b")
    probe = probe_export(tree)
    tree.open_count.clear()
    return collect_source_b(tree, probe, target)


def test_collector_одним_проходом_сохраняет_metadata_code_forms_и_roles(tmp_path):
    ArtifactKind = _symbol("ArtifactKind")
    read_role_member = _symbol("read_role_member")
    target = tmp_path / "collected"
    tree = _tree()

    result = _collect(tree, target)

    selected = {
        "Configuration.xml",
        "Catalogs/Demo.xml",
        "Catalogs/Demo/Ext/ObjectModule.bsl",
        "Catalogs/Demo/Forms/Main.xml",
        "Catalogs/Demo/Forms/Main/Ext/Form.xml",
        "Catalogs/Demo/Forms/Main/Ext/Form/Module.bsl",
        "Roles/Reader.xml",
        "Roles/Reader/Ext/Rights.xml",
        "Roles/Empty.xml",
        "Roles/Empty/Ext/Rights.xml",
    }
    assert tree.open_count == {path: 1 for path in selected}
    assert {item.kind for item in result.artifacts} == {
        ArtifactKind.METADATA,
        ArtifactKind.CODE,
        ArtifactKind.FORMS,
    }
    assert {item.address for item in result.code} == {
        "Справочник.Demo.МодульОбъекта",
        "Справочник.Demo.Форма.Main",
    }
    assert result.roles.state is LayerState.READY
    assert result.roles.roles_total == 2
    assert b"setForNewObjects" in read_role_member(
        result, "Roles/Reader.xml"
    )
    rights = read_role_member(
        result, "Roles/Reader/Ext/Rights.xml"
    )
    assert all(
        marker in rights
        for marker in (
            b"Catalog.Demo",
            b">false<",
            b"restrictionByCondition",
            b"field",
            b"template",
        )
    )
    assert b"Rights" in read_role_member(
        result, "Roles/Empty/Ext/Rights.xml"
    )


def test_collector_сохраняет_mixed_tree_и_flat_без_глобальной_ветки(tmp_path):
    tree = MemoryTree(
        {
            "Configuration.xml": _configuration(),
            "Catalog.Flat.ObjectModule.txt": b"flat",
            "Catalog.Flat.Form.Main.Form": b"container",
            "Catalogs/Tree/Ext/ObjectModule.bsl": b"tree",
            "Catalogs/Tree/Forms/Main/Ext/Form.bin": b"binary-form",
        }
    )

    result = _collect(tree, tmp_path / "mixed")

    assert {item.address for item in result.code} == {
        "Справочник.Flat.МодульОбъекта",
        "Справочник.Tree.МодульОбъекта",
    }
    assert {item.source_path for item in result.forms} == {
        "Catalog.Flat.Form.Main.Form",
        "Catalogs/Tree/Forms/Main/Ext/Form.bin",
    }


def test_collector_нормализует_роли_плоской_выгрузки(tmp_path):
    read_role_member = _symbol("read_role_member")
    tree = MemoryTree(
        {
            "Configuration.xml": _configuration(),
            "Role.Reader.xml": _role("Reader"),
            "Role.Reader.Rights.xml": _rights(),
            "Role.Empty.xml": _role("Empty"),
            "Role.Empty.Rights.xml": (
                b'<Rights xmlns="http://v8.1c.ru/8.2/roles" version="2.20"/>'
            ),
        }
    )

    result = _collect(tree, tmp_path / "flat-roles")

    assert result.roles.state is LayerState.READY
    assert result.roles.roles_total == 2
    assert {item.source_path for item in result.roles.artifacts} == {
        "Roles/Reader.xml",
        "Roles/Reader/Ext/Rights.xml",
        "Roles/Empty.xml",
        "Roles/Empty/Ext/Rights.xml",
    }
    assert b"setForNewObjects" in read_role_member(
        result, "Roles/Reader.xml"
    )
    assert b"Catalog.Demo" in read_role_member(
        result, "Roles/Reader/Ext/Rights.xml"
    )
    ET.fromstring(read_role_member(result, "Roles/Reader.xml"))


def test_collector_принимает_доказанные_flat_виды_без_unknown(tmp_path):
    ArtifactKind = _symbol("ArtifactKind")
    tree = MemoryTree(
        {
            "Configuration.xml": _configuration(),
            "DefinedType.Value.xml": b"<defined-type/>",
            "CommonAttribute.Tenant.xml": b"<common-attribute/>",
            "SessionParameter.Tenant.xml": b"<session-parameter/>",
            "EventSubscription.Write.xml": b"<event-subscription/>",
            "ScheduledJob.Refresh.xml": b"<scheduled-job/>",
            "ScheduledJob.Refresh.Schedule.xml": b"<schedule/>",
            "Sequence.Documents.RecordSetModule.txt": (
                b"procedure Register() endprocedure"
            ),
            "AccumulationRegister.Balance.Help.xml": b"<help/>",
            "Catalog.Products.Predefined.xml": b"<predefined/>",
            "Catalog.Products.Form.List.xml": b"<form-descriptor/>",
            "Catalog.Products.Template.Layout.xml": b"<template/>",
            "Subsystem.Future.xml": b"<deferred/>",
            "XDTOPackage.Future.xml": b"<deferred/>",
            "WSReference.Future.xml": b"<deferred/>",
            "CommonPicture.Logo.xml": b"<ignored/>",
            "CommonTemplate.Layout.xml": b"<ignored/>",
            "CommandGroup.Tools.xml": b"<ignored/>",
            "FunctionalOption.Feature.xml": b"<ignored/>",
            "FunctionalOptionsParameter.Value.xml": b"<ignored/>",
            "Language.Russian.xml": b"<ignored/>",
            "Style.Default.xml": b"<ignored/>",
            "StyleItem.Color.xml": b"<ignored/>",
            "Interface.Main.xml": b"<ignored/>",
        }
    )

    result = _collect(tree, tmp_path / "flat-kinds")

    assert {
        (item.source_name, item.kind, item.source_path)
        for item in result.artifacts
    } == {
        ("Configuration", ArtifactKind.METADATA, "Configuration.xml"),
        ("DefinedTypes", ArtifactKind.METADATA, "DefinedType.Value.xml"),
        (
            "CommonAttributes",
            ArtifactKind.METADATA,
            "CommonAttribute.Tenant.xml",
        ),
        (
            "SessionParameters",
            ArtifactKind.METADATA,
            "SessionParameter.Tenant.xml",
        ),
        (
            "EventSubscriptions",
            ArtifactKind.METADATA,
            "EventSubscription.Write.xml",
        ),
        (
            "ScheduledJobs",
            ArtifactKind.METADATA,
            "ScheduledJob.Refresh.xml",
        ),
        (
            "Sequences",
            ArtifactKind.CODE,
            "Sequence.Documents.RecordSetModule.txt",
        ),
    }
    assert not any(
        item.code in {"unsupported_metadata", "unsupported_layout"}
        for item in result.diagnostics
    )
    assert not any(
        item.source_path.endswith(
            (".Help.xml", ".Predefined.xml", ".Form.List.xml", ".Template.Layout.xml")
        )
        for item in result.artifacts
    )


def test_collector_бота_принимает_только_доказанный_tree_layout(tmp_path):
    tree = MemoryTree(
        {
            "Configuration.xml": _configuration(),
            "Bots/Assistant.xml": b"<bot/>",
            "Bots/Assistant/Ext/Module.bsl": b"procedure Reply() endprocedure",
            "Bots/Assistant/Ext/ObjectModule.bsl": b"unsupported-tree",
            "Bot.Flat.Module.txt": b"unsupported-flat",
        }
    )

    result = _collect(tree, tmp_path / "bot")

    assert {
        (item.source_path, item.address)
        for item in result.artifacts
        if item.source_name == "Bots"
    } == {
        ("Bots/Assistant.xml", ""),
        ("Bots/Assistant/Ext/Module.bsl", "Бот.Assistant"),
    }
    assert "Bots/Assistant/Ext/ObjectModule.bsl" not in tree.open_count
    assert "Bot.Flat.Module.txt" not in tree.open_count
    assert {
        (item.code, item.signature)
        for item in result.diagnostics
    } >= {
        ("unsupported_layout", "Bots"),
        ("unsupported_layout", "Bot"),
    }


def test_collector_применяет_supported_deferred_ignored_без_чтения(tmp_path):
    DEFAULT_KIND_SPECS = _symbol("DEFAULT_KIND_SPECS")
    by_name = {spec.source_name: spec for spec in DEFAULT_KIND_SPECS}

    result = _collect(_tree(), tmp_path / "policies")

    assert by_name["Catalogs"].policy is MetadataKindPolicy.SUPPORTED
    assert by_name["Subsystems"].policy is MetadataKindPolicy.DEFERRED
    assert by_name["SettingsStorages"].policy is MetadataKindPolicy.IGNORED
    assert not any("Subsystems" in item.source_path for item in result.artifacts)
    assert not any("SettingsStorages" in item.source_path for item in result.artifacts)


def test_collector_агрегирует_unknown_и_не_считает_schedule_потерей(tmp_path):
    tree = _tree()

    result = _collect(tree, tmp_path / "diagnostics")

    metadata = [
        item for item in result.diagnostics if item.code == "unsupported_metadata"
    ]
    layout = [item for item in result.diagnostics if item.code == "unsupported_layout"]
    assert len(metadata) == 1
    assert metadata[0].count == 4
    assert len(metadata[0].examples) == 3
    assert len(layout) == 1 and layout[0].count == 1
    assert not any(
        "Schedule" in example
        for item in result.diagnostics
        for example in item.examples
    )
    assert not any(path.startswith("UnknownThings/") for path in tree.open_count)


def test_collector_считает_неизвестный_layout_поддержанного_вида_один_раз(
    tmp_path,
):
    tree = MemoryTree(
        {
            "Configuration.xml": _configuration(),
            "Catalogs/Demo/Ext/UnknownModule.bsl": b"unknown",
        }
    )

    result = _collect(tree, tmp_path / "known-kind-unknown-layout")

    diagnostics = [
        item
        for item in result.diagnostics
        if item.code == "unsupported_layout" and item.signature == "Catalogs"
    ]
    assert len(diagnostics) == 1
    assert diagnostics[0].count == 1
    assert "Catalogs/Demo/Ext/UnknownModule.bsl" not in tree.open_count


def test_collector_нулевые_роли_ready_и_переживают_удаление_источника(tmp_path):
    load_collection = _symbol("load_collection")
    tree = MemoryTree(
        {
            "Configuration.xml": _configuration(),
            "Catalogs/Demo/Ext/ObjectModule.bsl": b"code",
        }
    )
    target = tmp_path / "durable"

    result = _collect(tree, target)
    tree.payloads.clear()
    restored = load_collection(target)

    assert restored.probe == result.probe
    assert restored.probe.raw_sha256 == result.probe.raw_sha256
    assert result.roles.state is restored.roles.state is LayerState.READY
    assert restored.roles.roles_total == 0
    assert (target / restored.code[0].relative_path).read_bytes() == b"code"


@pytest.mark.parametrize("extra", [{"Roles/Broken.xml": b"<broken"}])
def test_collector_локальная_ошибка_roles_не_ломает_code_и_не_оставляет_payload(
    tmp_path, extra
):
    tree = MemoryTree(
        {
            "Configuration.xml": _configuration(),
            "Catalogs/Demo/Ext/ObjectModule.bsl": b"code",
            **extra,
        }
    )

    result = _collect(tree, tmp_path / "role-error")

    assert result.roles.state is LayerState.ERROR
    assert result.roles.error
    assert result.roles.artifacts == ()
    assert len(result.code) == 1
    assert (tmp_path / "role-error" / result.code[0].relative_path).is_file()


def test_collector_сохраняет_descriptor_без_rights_как_пустую_роль(tmp_path):
    load_collection = _symbol("load_collection")
    tree = MemoryTree(
        {
            "Configuration.xml": _configuration(),
            "Roles/Empty.xml": _role("Empty"),
        }
    )
    target = tmp_path / "descriptor-only-role"

    result = _collect(tree, target)
    tree.payloads.clear()
    restored = load_collection(target)

    assert result.roles.state is restored.roles.state is LayerState.READY
    assert restored.roles.roles_total == 1
    assert [item.source_path for item in restored.roles.artifacts] == [
        "Roles/Empty.xml"
    ]


def test_collector_rights_без_descriptor_оставляет_ролевой_слой_error(tmp_path):
    result = _collect(
        MemoryTree(
            {
                "Configuration.xml": _configuration(),
                "Roles/Orphan/Ext/Rights.xml": _rights(),
            }
        ),
        tmp_path / "rights-without-descriptor",
    )

    assert result.roles.state is LayerState.ERROR
    assert "descriptor" in result.roles.error
    assert result.roles.artifacts == ()


@pytest.mark.parametrize("failure", ["transport", "unstable"])
def test_collector_общая_ошибка_отменяет_весь_staging(tmp_path, failure):
    CollectorError = _symbol("CollectorError")
    collect_source_b = _symbol("collect_source_b")
    target = tmp_path / "failed"
    tree = MemoryTree(
        {
            "Configuration.xml": _configuration(),
            "Catalogs/Demo/Ext/ObjectModule.bsl": b"code",
        },
        fail_on=(
            "Catalogs/Demo/Ext/ObjectModule.bsl" if failure == "transport" else ""
        ),
    )
    probe = probe_export(tree)
    tree.open_count.clear()
    tree.unstable = failure == "unstable"

    with pytest.raises(CollectorError):
        collect_source_b(tree, probe, target)

    assert not target.exists()
    assert list(tmp_path.glob(".failed.*.tmp")) == []


def test_collection_manifest_fail_closed_на_подмене_и_symlink(tmp_path):
    CollectionError = _symbol("CollectionError")
    load_collection = _symbol("load_collection")
    target = tmp_path / "verified"
    result = _collect(
        MemoryTree(
            {
                "Configuration.xml": _configuration(),
                "Catalogs/Demo/Ext/ObjectModule.bsl": b"code",
            }
        ),
        target,
    )
    code = target / result.code[0].relative_path
    code.write_bytes(b"evil")

    with pytest.raises(CollectionError, match="хеш|размер|измен"):
        load_collection(target)

    code.unlink()
    outside = tmp_path / "outside"
    outside.write_bytes(b"code")
    code.symlink_to(outside)
    with pytest.raises(CollectionError, match="символическ|обычным файлом"):
        load_collection(target)


def test_collector_не_перезаписывает_готовый_target(tmp_path):
    CollectorError = _symbol("CollectorError")
    target = tmp_path / "existing"
    target.mkdir()
    marker = target / "marker"
    marker.write_text("keep", encoding="utf-8")

    with pytest.raises(CollectorError, match="существ"):
        _collect(
            MemoryTree({"Configuration.xml": _configuration()}),
            target,
        )

    assert marker.read_text(encoding="utf-8") == "keep"


def test_collector_работает_через_zip_wrapper_после_удаления_архива(tmp_path):
    load_collection = _symbol("load_collection")
    read_role_member = _symbol("read_role_member")
    archive = tmp_path / "wrapped.zip"
    with zipfile.ZipFile(archive, "w") as output:
        output.writestr("Wrap/Configuration.xml", _configuration())
        output.writestr("Wrap/Catalogs/Demo/Ext/ObjectModule.bsl", b"code")
        output.writestr("Wrap/Roles/Reader.xml", _role("Reader"))
        output.writestr("Wrap/Roles/Reader/Ext/Rights.xml", _rights())
    tree = ZipExportTree(archive, transport=CandidateTransport.INCOMING)
    probe = probe_export(tree)

    result = _symbol("collect_source_b")(tree, probe, tmp_path / "from-zip")
    tree.close()
    archive.unlink()
    restored = load_collection(result.root)

    assert restored.probe.wrapper == "Wrap"
    assert restored.code[0].source_path == "Catalogs/Demo/Ext/ObjectModule.bsl"
    assert b"Catalog.Demo" in read_role_member(
        restored, "Roles/Reader/Ext/Rights.xml"
    )
