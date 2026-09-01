"""RED-контракты probe, identity и active-only grouping этапа 3."""

from __future__ import annotations

import hashlib
import importlib
import io

import pytest

from mcp1c.intake_v2 import CandidateTransport, ExportIdentity, SourceKind


SUBJECT = "mcp1c.intake_v2_probe"
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


def _xml(
    name: str,
    *,
    version: str = "1.2.3",
    compatibility: str = "Version8_3_21",
    belonging: str = "",
    purpose: str = "",
    prefix: str = "",
) -> bytes:
    return (
        f'<MetaDataObject xmlns="{NS}"><Configuration><Properties>'
        f"<Name>{name}</Name><Version>{version}</Version>"
        f"<NamePrefix>{prefix}</NamePrefix>"
        f"<ObjectBelonging>{belonging}</ObjectBelonging>"
        f"<ConfigurationExtensionPurpose>{purpose}</ConfigurationExtensionPurpose>"
        f"<CompatibilityMode>{compatibility}</CompatibilityMode>"
        "</Properties></Configuration></MetaDataObject>"
    ).encode()


class MemoryTree:
    transport = CandidateTransport.INCOMING
    origin_name = "demo-export.zip"

    def __init__(self, payloads: dict[str, bytes]):
        self.payloads = dict(payloads)
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
        try:
            return io.BytesIO(self.payloads[path])
        except KeyError:
            raise KeyError(path) from None

    def size(self, path: str) -> int:
        return len(self.payloads[path])

    def fingerprint(self) -> str:
        return self._fingerprint

    def source_sha256(self) -> str:
        return self._digest()

    def verify_stable(self, expected: str) -> bool:
        return expected == self._fingerprint == self._digest()


def test_probe_основной_конфигурации_возвращает_internal_name_и_provenance():
    ExportLayout = _symbol("ExportLayout")
    probe_export = _symbol("probe_export")
    tree = MemoryTree(
        {
            "Configuration.xml": _xml("DemoConfiguration", version="4.5.6"),
            "Catalogs/Demo/Ext/ObjectModule.bsl": b"procedure Demo() endprocedure",
        }
    )

    probe = probe_export(tree)

    assert probe.source_kind is SourceKind.CONFIGURATION
    assert probe.internal_name == "DemoConfiguration"
    assert probe.configuration_version == "4.5.6"
    assert probe.layout is ExportLayout.TREE
    assert probe.wrapper == ""
    assert probe.raw_sha256 == tree.source_sha256()
    assert probe.transport is CandidateTransport.INCOMING


def test_probe_расширения_требует_родителя_до_grouping():
    ProbeError = _symbol("ProbeError")
    probe_export = _symbol("probe_export")
    tree = MemoryTree(
        {
            "Configuration.xml": _xml(
                "DemoExtension",
                compatibility="",
                belonging="Adopted",
                purpose="AddOn",
                prefix="Demo_",
            ),
            "Catalogs/Demo/Ext/ObjectModule.bsl": b"procedure Demo() endprocedure",
        }
    )

    probe = probe_export(tree)

    assert probe.source_kind is SourceKind.EXTENSION
    assert probe.internal_name == "DemoExtension"
    with pytest.raises(ProbeError, match="родител"):
        probe.bind("candidate-extension")
    bound = probe.bind(
        "candidate-extension", parent_configuration="DemoConfiguration"
    )
    assert bound.identity == ExportIdentity.extension(
        "DemoExtension", parent_configuration="DemoConfiguration"
    )


@pytest.mark.parametrize(
    "properties",
    [
        {"belonging": "Adopted", "purpose": "", "prefix": "Demo_"},
        {"belonging": "", "purpose": "AddOn", "prefix": "Demo_"},
        {
            "belonging": "Adopted",
            "purpose": "AddOn",
            "prefix": "Demo_",
            "compatibility": "Version8_3_21",
        },
    ],
)
def test_probe_fail_closed_на_противоречивых_признаках_расширения(properties):
    ProbeError = _symbol("ProbeError")
    probe_export = _symbol("probe_export")
    tree = MemoryTree({"Configuration.xml": _xml("DemoExtension", **properties)})

    with pytest.raises(ProbeError, match="расширен|признак"):
        probe_export(tree)


@pytest.mark.parametrize(
    ("members", "expected_layout", "expected_wrapper"),
    [
        (
            {"Wrap/Configuration.xml": _xml("Demo"), "Wrap/Catalog.Demo.Form": b"x"},
            "flat",
            "Wrap",
        ),
        (
            {"Configuration.xml": _xml("Demo"), "Catalogs/Demo/Ext/Form.xml": b"x"},
            "tree",
            "",
        ),
        (
            {
                "Configuration.xml": _xml("Demo"),
                "Catalogs/Demo/Ext/ObjectModule.bsl": b"x",
                "Catalog.Demo.Form": b"x",
            },
            "mixed",
            "",
        ),
    ],
)
def test_probe_определяет_flat_tree_mixed_и_wrapper_по_путям(
    members, expected_layout, expected_wrapper
):
    probe_export = _symbol("probe_export")
    probe = probe_export(MemoryTree(members))

    assert probe.layout.value == expected_layout
    assert probe.wrapper == expected_wrapper


def test_probe_отвергает_missing_ambiguous_и_слишком_большой_descriptor():
    ProbeError = _symbol("ProbeError")
    probe_export = _symbol("probe_export")

    with pytest.raises(ProbeError, match="Configuration.xml"):
        probe_export(MemoryTree({"Catalogs/Demo.xml": b"x"}))
    with pytest.raises(ProbeError, match="несколь|неоднознач"):
        probe_export(
            MemoryTree(
                {
                    "Configuration.xml": _xml("Demo"),
                    "Wrap/Configuration.xml": _xml("Other"),
                }
            )
        )
    with pytest.raises(ProbeError, match="размер|предел"):
        probe_export(
            MemoryTree({"Configuration.xml": b" " * (8 * 1024 * 1024 + 1)})
        )


def test_probe_не_выбирает_parser_по_версии_конфигурации():
    probe_export = _symbol("probe_export")
    old = probe_export(
        MemoryTree(
            {
                "Configuration.xml": _xml("Demo", version="1.0.0"),
                "Catalogs/Demo/Ext/ObjectModule.bsl": b"x",
            }
        )
    )
    new = probe_export(
        MemoryTree(
            {
                "Configuration.xml": _xml("Demo", version="99.0.0"),
                "Catalogs/Demo/Ext/ObjectModule.bsl": b"x",
            }
        )
    )

    assert old.layout == new.layout
    assert old.source_kind == new.source_kind


def test_grouping_использует_internal_identity_а_не_origin_или_version():
    group_candidates = _symbol("group_candidates")
    probe_export = _symbol("probe_export")
    first = probe_export(MemoryTree({"Configuration.xml": _xml("Demo", version="1")}))
    second_tree = MemoryTree({"Configuration.xml": _xml("Demo", version="2")})
    second_tree.origin_name = "renamed.zip"
    second = probe_export(second_tree)
    other = probe_export(MemoryTree({"Configuration.xml": _xml("Other")}))

    groups = group_candidates(
        (
            first.bind("first"),
            second.bind("second"),
            other.bind("other"),
        )
    )

    assert groups[("configuration", "Demo")] == ("first", "second")
    assert groups[("configuration", "Other")] == ("other",)


def test_active_only_compare_различает_current_duplicate_reparse_foreign():
    ActiveSnapshot = _symbol("ActiveSnapshot")
    CandidateState = _symbol("CandidateState")
    compare_with_active = _symbol("compare_with_active")
    probe_export = _symbol("probe_export")
    probe = probe_export(MemoryTree({"Configuration.xml": _xml("Demo")}))
    bound = probe.bind("candidate")
    hashes = {"base_structure": "a" * 64, "code": "b" * 64}
    active = ActiveSnapshot(
        identity=bound.identity,
        raw_sha256=probe.raw_sha256,
        semantic_hashes=hashes,
        parser_version=2,
        selection_version=3,
    )

    assert compare_with_active(
        bound,
        hashes,
        active,
        parser_version=2,
        selection_version=3,
    ) is CandidateState.CURRENT
    assert compare_with_active(
        bound,
        hashes,
        active,
        parser_version=3,
        selection_version=3,
    ) is CandidateState.REPARSE
    repacked = bound.with_raw_sha256("f" * 64)
    assert compare_with_active(
        repacked,
        hashes,
        active,
        parser_version=2,
        selection_version=3,
    ) is CandidateState.DUPLICATE
    assert compare_with_active(
        repacked,
        hashes,
        active,
        parser_version=3,
        selection_version=3,
    ) is CandidateState.REPARSE
    assert compare_with_active(
        repacked,
        {**hashes, "code": "c" * 64},
        active,
        parser_version=2,
        selection_version=3,
    ) is CandidateState.DIFFERENT_SNAPSHOT

    foreign_probe = probe_export(MemoryTree({"Configuration.xml": _xml("Other")}))
    assert compare_with_active(
        foreign_probe.bind("foreign"),
        hashes,
        active,
        parser_version=2,
        selection_version=3,
    ) is CandidateState.FOREIGN_IDENTITY
