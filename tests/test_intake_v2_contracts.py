"""RED-контракты первого этапа единого приёма конфигураций.

Здесь нет проверки нынешнего ``intake``: сначала фиксируются новые общие
границы, и только после наблюдаемого RED появляется production-реализация.
Все примеры синтетические и не зависят от ``data/``.
"""

from __future__ import annotations

import importlib
import io
import json
from dataclasses import replace

import pytest


SUBJECT = "mcp1c.intake_v2"


def _symbol(name: str):
    """Один RED на контракт, а не один collection error на весь файл."""
    try:
        module = importlib.import_module(SUBJECT)
    except ModuleNotFoundError as error:
        if error.name != SUBJECT:
            raise
        pytest.fail(f"RED: отсутствует модуль {SUBJECT} для контракта {name}")
    if not hasattr(module, name):
        pytest.fail(f"RED: в {SUBJECT} отсутствует контракт {name}")
    return getattr(module, name)


def test_identity_группирует_конфигурацию_и_расширение_без_имени_файла():
    ExportIdentity = _symbol("ExportIdentity")

    configuration = ExportIdentity.configuration("DemoConfiguration")
    extension = ExportIdentity.extension(
        "DemoExtension", parent_configuration="DemoConfiguration"
    )

    assert configuration.grouping_key == ("configuration", "DemoConfiguration")
    assert extension.grouping_key == (
        "extension",
        "DemoConfiguration",
        "DemoExtension",
    )
    with pytest.raises(ValueError, match="родител"):
        ExportIdentity.extension("OrphanExtension", parent_configuration="")


def test_export_candidate_несёт_транспорт_raw_hash_и_устойчивый_снимок():
    CandidateTransport = _symbol("CandidateTransport")
    ExportCandidate = _symbol("ExportCandidate")
    ExportIdentity = _symbol("ExportIdentity")

    candidate = ExportCandidate(
        candidate_id="candidate-001",
        transport=CandidateTransport.BROWSER,
        origin_name="demo-export.zip",
        raw_sha256="a" * 64,
        snapshot_fingerprint="b" * 64,
        identity=ExportIdentity.configuration("DemoConfiguration"),
    )

    assert candidate.origin_name == "demo-export.zip"
    assert candidate.transport.value == "browser"
    assert candidate.identity.configuration_name == "DemoConfiguration"
    with pytest.raises(ValueError, match="sha256"):
        replace(candidate, raw_sha256="not-a-sha")


def test_virtual_tree_задаёт_один_потоковый_контракт_для_всех_транспортов():
    CandidateTransport = _symbol("CandidateTransport")
    VirtualExportTree = _symbol("VirtualExportTree")

    class SyntheticTree:
        transport = CandidateTransport.INCOMING
        origin_name = "demo-export.zip"

        def paths(self) -> tuple[str, ...]:
            return ("Catalogs/Demo/Ext/ObjectModule.bsl", "Configuration.xml")

        def open(self, path: str):
            if path not in self.paths():
                raise KeyError(path)
            return io.BytesIO(path.encode("utf-8"))

        def size(self, path: str) -> int:
            with self.open(path) as stream:
                return len(stream.read())

        def fingerprint(self) -> str:
            return "c" * 64

        def source_sha256(self) -> str:
            return "d" * 64

        def verify_stable(self, expected: str) -> bool:
            return expected == self.fingerprint()

    tree = SyntheticTree()

    assert isinstance(tree, VirtualExportTree)
    assert tree.paths() == tuple(sorted(tree.paths()))
    with tree.open("Configuration.xml") as stream:
        assert stream.read() == b"Configuration.xml"
    assert tree.verify_stable(tree.fingerprint()) is True


def test_durable_store_восстанавливает_candidate_и_job_после_нового_экземпляра(
    tmp_path,
):
    CandidateJob = _symbol("CandidateJob")
    CandidateJobState = _symbol("CandidateJobState")
    CandidateTransport = _symbol("CandidateTransport")
    DurableCandidateStore = _symbol("DurableCandidateStore")
    ExportCandidate = _symbol("ExportCandidate")
    ExportIdentity = _symbol("ExportIdentity")

    candidate = ExportCandidate(
        candidate_id="candidate-001",
        transport=CandidateTransport.BROWSER,
        origin_name="demo-export.zip",
        raw_sha256="a" * 64,
        snapshot_fingerprint="b" * 64,
        identity=ExportIdentity.configuration("DemoConfiguration"),
    )
    job = CandidateJob(
        job_id="job-001",
        candidate_id=candidate.candidate_id,
        state=CandidateJobState.ACCEPTED,
    )
    first = DurableCandidateStore(tmp_path / "candidate-store")
    first.save_candidate(candidate)
    first.save_job(job)

    after_restart = DurableCandidateStore(tmp_path / "candidate-store")

    assert after_restart.load_candidate(candidate.candidate_id) == candidate
    assert after_restart.load_job(job.job_id) == job
    ready = job.transition(CandidateJobState.PROBING).transition(
        CandidateJobState.READY
    )
    parsing = ready.transition(CandidateJobState.PARSING)
    assert parsing.state is CandidateJobState.PARSING
    with pytest.raises(ValueError, match="переход"):
        parsing.transition(CandidateJobState.ACCEPTED)
    with pytest.raises(ValueError, match="переход"):
        job.transition(CandidateJobState.PARSING)


def test_durable_store_не_теряет_прежнюю_запись_при_отказе_atomic_replace(
    tmp_path, monkeypatch
):
    CandidateStoreError = _symbol("CandidateStoreError")
    CandidateTransport = _symbol("CandidateTransport")
    DurableCandidateStore = _symbol("DurableCandidateStore")
    ExportCandidate = _symbol("ExportCandidate")
    ExportIdentity = _symbol("ExportIdentity")

    store = DurableCandidateStore(tmp_path / "candidate-store")
    previous = ExportCandidate(
        candidate_id="candidate-001",
        transport=CandidateTransport.BROWSER,
        origin_name="demo-export.zip",
        raw_sha256="a" * 64,
        snapshot_fingerprint="b" * 64,
        identity=ExportIdentity.configuration("DemoConfiguration"),
    )
    store.save_candidate(previous)
    changed = replace(previous, raw_sha256="c" * 64)

    def fail_replace(_source, _target):
        raise OSError("synthetic replace failure")

    monkeypatch.setattr("mcp1c.intake_v2.os.replace", fail_replace)

    with pytest.raises(CandidateStoreError, match="атомарно"):
        store.save_candidate(changed)

    assert store.load_candidate(previous.candidate_id) == previous
    assert list(store.candidates_dir.glob("*.tmp")) == []


def test_durable_store_fail_closed_на_повреждённой_записи_и_symlink(
    tmp_path,
):
    CandidateStoreError = _symbol("CandidateStoreError")
    CandidateTransport = _symbol("CandidateTransport")
    DurableCandidateStore = _symbol("DurableCandidateStore")
    ExportCandidate = _symbol("ExportCandidate")
    ExportIdentity = _symbol("ExportIdentity")

    store = DurableCandidateStore(tmp_path / "candidate-store")
    candidate = ExportCandidate(
        candidate_id="candidate-001",
        transport=CandidateTransport.INCOMING,
        origin_name="demo-export.zip",
        raw_sha256="a" * 64,
        snapshot_fingerprint="b" * 64,
        identity=ExportIdentity.configuration("DemoConfiguration"),
    )
    store.save_candidate(candidate)
    record = store.candidates_dir / "candidate-001.json"
    record.write_text(json.dumps({"format_version": 999}), encoding="utf-8")

    with pytest.raises(CandidateStoreError, match="несовместим"):
        store.load_candidate(candidate.candidate_id)

    linked_root = tmp_path / "linked-store"
    linked_root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (linked_root / "candidates").symlink_to(outside, target_is_directory=True)
    with pytest.raises(CandidateStoreError, match="символическ"):
        DurableCandidateStore(linked_root)

    root_target = tmp_path / "root-target"
    root_target.mkdir()
    root_link = tmp_path / "root-link"
    root_link.symlink_to(root_target, target_is_directory=True)
    with pytest.raises(CandidateStoreError, match="символическ"):
        DurableCandidateStore(root_link)

    regular_store = DurableCandidateStore(tmp_path / "regular-store")
    outside_record = tmp_path / "outside-record.json"
    outside_record.write_text("{}", encoding="utf-8")
    (regular_store.candidates_dir / "linked.json").symlink_to(outside_record)
    with pytest.raises(CandidateStoreError, match="символическ"):
        regular_store.load_candidate("linked")


def test_metadata_kind_spec_различает_supported_deferred_ignored():
    LayerKind = _symbol("LayerKind")
    MetadataKindPolicy = _symbol("MetadataKindPolicy")
    MetadataKindSpec = _symbol("MetadataKindSpec")

    supported = MetadataKindSpec(
        source_name="DocumentJournals",
        canonical_kind="ЖурналДокументов",
        policy=MetadataKindPolicy.SUPPORTED,
        layers=frozenset({LayerKind.EXTENDED_STRUCTURE, LayerKind.FORMS, LayerKind.CODE}),
        layouts=frozenset({"tree"}),
    )
    deferred = MetadataKindSpec(
        source_name="Subsystems",
        canonical_kind="Подсистема",
        policy=MetadataKindPolicy.DEFERRED,
    )
    ignored = MetadataKindSpec(
        source_name="SettingsStorages",
        canonical_kind="ХранилищеНастроек",
        policy=MetadataKindPolicy.IGNORED,
    )

    assert supported.supports(LayerKind.EXTENDED_STRUCTURE)
    assert not deferred.layers and not ignored.layers
    assert deferred.policy.value == "deferred"
    assert ignored.policy.value == "ignored"


def test_layer_manifest_не_смешивает_структуру_формы_код_и_роли():
    LayerKind = _symbol("LayerKind")
    LayerManifest = _symbol("LayerManifest")
    LayerState = _symbol("LayerState")

    layers = {
        kind: LayerManifest(
            kind=kind,
            state=LayerState.READY,
            content_sha256=f"{number:064x}",
            relative_path=f"layers/{kind.value}.json",
        )
        for number, kind in enumerate(
            (
                LayerKind.BASE_STRUCTURE,
                LayerKind.EXTENDED_STRUCTURE,
                LayerKind.FORMS,
                LayerKind.CODE,
                LayerKind.ROLES,
            ),
            1,
        )
    }

    assert len({layer.content_sha256 for layer in layers.values()}) == 5
    assert layers[LayerKind.FORMS].relative_path != layers[LayerKind.CODE].relative_path
    assert LayerState.UNAVAILABLE.value == "unavailable"
    assert LayerState.ERROR.value == "error"
    with pytest.raises(ValueError, match="относитель"):
        replace(layers[LayerKind.CODE], relative_path="../outside.json")
    with pytest.raises(ValueError, match="относитель"):
        replace(layers[LayerKind.CODE], relative_path=".")


def test_layer_provenance_переживает_merge_слоёв_разных_источников():
    CandidateTransport = _symbol("CandidateTransport")
    LayerKind = _symbol("LayerKind")
    LayerManifest = _symbol("LayerManifest")
    LayerProvenance = _symbol("LayerProvenance")
    LayerSourceProfile = _symbol("LayerSourceProfile")
    LayerState = _symbol("LayerState")

    provenance = LayerProvenance(
        profile=LayerSourceProfile.SOURCE_B,
        transport=CandidateTransport.INCOMING,
        origin_name="demo-export.zip",
        raw_sha256="a" * 64,
        parser_version=7,
        selection_version=8,
    )
    ready = LayerManifest(
        kind=LayerKind.CODE,
        state=LayerState.READY,
        content_sha256="b" * 64,
        relative_path="layers/code.json",
        provenance=provenance,
    )
    broken = LayerManifest(
        kind=LayerKind.ROLES,
        state=LayerState.ERROR,
        error="непрочитан синтетический Rights.xml",
        provenance=provenance,
    )

    assert LayerManifest.from_dict(ready.to_dict()) == ready
    assert LayerManifest.from_dict(broken.to_dict()) == broken
    assert ready.provenance.profile.value == "source-b"
    with pytest.raises(ValueError, match="parser_version"):
        replace(provenance, parser_version=0)


def test_role_layer_различает_нулевой_ready_error_и_unavailable():
    LayerKind = _symbol("LayerKind")
    LayerManifest = _symbol("LayerManifest")
    LayerState = _symbol("LayerState")

    empty_ready = LayerManifest(
        kind=LayerKind.ROLES,
        state=LayerState.READY,
        content_sha256="d" * 64,
        relative_path="layers/roles.json",
        items_total=0,
    )
    broken = LayerManifest(
        kind=LayerKind.ROLES,
        state=LayerState.ERROR,
        error="непрочитан синтетический Rights.xml",
    )
    unavailable = LayerManifest(
        kind=LayerKind.ROLES,
        state=LayerState.UNAVAILABLE,
    )

    assert empty_ready.items_total == 0 and not empty_ready.error
    assert broken.error and broken.content_sha256 == ""
    assert unavailable.content_sha256 == "" and unavailable.relative_path == ""
    with pytest.raises(ValueError, match="error"):
        replace(broken, error="")


def test_generation_manifest_каноничен_проверяем_и_не_хранит_payload_внутри():
    CandidateTransport = _symbol("CandidateTransport")
    ExportIdentity = _symbol("ExportIdentity")
    GenerationManifest = _symbol("GenerationManifest")
    LayerKind = _symbol("LayerKind")
    LayerManifest = _symbol("LayerManifest")
    LayerState = _symbol("LayerState")

    manifest = GenerationManifest(
        format_version=1,
        generation_id="generation-001",
        identity=ExportIdentity.configuration("DemoConfiguration"),
        parser_version=1,
        selection_version=1,
        source_transport=CandidateTransport.BROWSER,
        origin_name="demo-export.zip",
        raw_sha256="a" * 64,
        layers=(
            LayerManifest(
                kind=LayerKind.CODE,
                state=LayerState.READY,
                content_sha256="e" * 64,
                relative_path="layers/code.json",
            ),
            LayerManifest(
                kind=LayerKind.ROLES,
                state=LayerState.UNAVAILABLE,
            ),
        ),
    )

    encoded = manifest.to_json_bytes()
    restored = GenerationManifest.from_json_bytes(encoded)

    assert restored == manifest
    assert restored.to_json_bytes() == encoded
    assert restored.sha256 == manifest.sha256
    assert b"module body" not in encoded and b"role payload" not in encoded
    assert encoded.endswith(b"\n")

    reverse_order = replace(manifest, layers=tuple(reversed(manifest.layers)))
    assert reverse_order == manifest
    assert reverse_order.to_json_bytes() == encoded


def test_recovery_выбирает_старое_или_новое_поколение_и_блокирует_третье():
    RecoveryAction = _symbol("RecoveryAction")
    RecoveryPhase = _symbol("RecoveryPhase")
    RecoveryRecord = _symbol("RecoveryRecord")
    decide_recovery = _symbol("decide_recovery")

    record = RecoveryRecord(
        previous_generation="generation-old",
        staged_generation="generation-new",
        phase=RecoveryPhase.PREPARED,
    )

    assert decide_recovery("generation-old", record) is RecoveryAction.ROLLBACK_STAGING
    assert decide_recovery("generation-new", record) is RecoveryAction.FINALIZE_NEW
    assert decide_recovery("generation-foreign", record) is RecoveryAction.BLOCK

    switched = replace(record, phase=RecoveryPhase.POINTER_SWITCHED)
    assert decide_recovery("generation-old", switched) is RecoveryAction.BLOCK


def test_recovery_первичного_создания_допускает_отсутствующий_active_pointer():
    RecoveryAction = _symbol("RecoveryAction")
    RecoveryPhase = _symbol("RecoveryPhase")
    RecoveryRecord = _symbol("RecoveryRecord")
    decide_recovery = _symbol("decide_recovery")

    record = RecoveryRecord(
        previous_generation=None,
        staged_generation="generation-new",
        phase=RecoveryPhase.PREPARED,
    )

    assert decide_recovery(None, record) is RecoveryAction.ROLLBACK_STAGING
    assert decide_recovery("generation-new", record) is RecoveryAction.FINALIZE_NEW
