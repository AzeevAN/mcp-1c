"""RED-контракты durable backend для preview единого intake."""

from __future__ import annotations

import importlib
import io
import zipfile
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from threading import Event

import pytest

from conftest import build_configuration, write_export
from mcp1c.intake_v2 import (
    CandidateJobState,
    DurableCandidateStore,
    LayerKind,
)
from mcp1c.intake_v2_planner import IntakeAction
from mcp1c.intake_v2_transport import BrowserStagingStore
from mcp1c.registry import Registry, RegistryError
from test_intake_v2_collector import _configuration


SUBJECT = "mcp1c.intake_v2_operations"


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


def _intake_symbol(name: str):
    module = importlib.import_module("mcp1c.intake_v2")
    if not hasattr(module, name):
        pytest.fail(f"RED: в mcp1c.intake_v2 отсутствует контракт {name}")
    return getattr(module, name)


def _archive() -> bytes:
    payload = io.BytesIO()
    with zipfile.ZipFile(payload, "w") as archive:
        archive.writestr("Configuration.xml", _configuration())
    return payload.getvalue()


def _stores(tmp_path):
    root = tmp_path / "managed-intake"
    return (
        root,
        BrowserStagingStore(root / "uploads"),
        DurableCandidateStore(root / "records"),
    )


def _accepted(upload_store, coordinator, candidate_id="candidate-001", job_id="job-001"):
    raw = _archive()
    upload_store.accept(
        candidate_id,
        "demo-export.zip",
        io.BytesIO(raw),
        expected_size=len(raw),
    )
    job = coordinator.create_job(job_id, candidate_id)
    assert job.state is CandidateJobState.ACCEPTED
    with upload_store.open_tree(candidate_id) as tree:
        candidate = coordinator.probe(job_id, tree)
    return candidate


def test_preview_переживает_рестарт_и_не_публикует_registry(tmp_path):
    IntakeCoordinator = _symbol("IntakeCoordinator")
    CandidateJobStage = _intake_symbol("CandidateJobStage")

    root, uploads, records = _stores(tmp_path)
    first = IntakeCoordinator(root / "operations", records)
    candidate = _accepted(uploads, first)
    assert candidate.identity.configuration_name == "DemoConfiguration"
    assert records.load_job("job-001").state is CandidateJobState.READY

    restarted_uploads = BrowserStagingStore(root / "uploads")
    restarted_records = DurableCandidateStore(root / "records")
    restarted = IntakeCoordinator(root / "operations", restarted_records)
    with restarted_uploads.open_tree("candidate-001") as tree:
        preview = restarted.prepare(
            "job-001",
            tree,
            action=IntakeAction.CREATE,
            active=None,
            generation_id="generation-001",
        )

    assert preview.plan.action is IntakeAction.CREATE
    assert preview.plan.applied_layers == set(LayerKind)
    assert preview.plan.no_op is False
    assert preview.materialized.root.is_dir()
    assert not (tmp_path / "data").exists()
    done = restarted_records.load_job("job-001")
    assert done.state is CandidateJobState.DONE
    assert done.stage is CandidateJobStage.DONE
    assert done.result == "job-001"

    after_restart = IntakeCoordinator(
        root / "operations",
        DurableCandidateStore(root / "records"),
    ).load_preview("job-001")
    assert after_restart.plan == preview.plan
    assert after_restart.materialized.manifest == preview.materialized.manifest
    assert set(after_restart.materialized.payloads) == set(LayerKind)


def test_обычная_ошибка_сохраняется_и_удаляет_частичный_preview(
    tmp_path, monkeypatch
):
    IntakeCoordinator = _symbol("IntakeCoordinator")
    OperationError = _symbol("OperationError")
    CandidateJobStage = _intake_symbol("CandidateJobStage")

    root, uploads, records = _stores(tmp_path)
    coordinator = IntakeCoordinator(root / "operations", records)
    _accepted(uploads, coordinator)

    def fail_conversion(_collection):
        raise RuntimeError("синтетический отказ conversion")

    monkeypatch.setattr(f"{SUBJECT}.convert_collection", fail_conversion)
    with uploads.open_tree("candidate-001") as tree:
        with pytest.raises(OperationError, match="conversion"):
            coordinator.prepare(
                "job-001",
                tree,
                action=IntakeAction.CREATE,
                active=None,
                generation_id="generation-001",
            )

    restarted = DurableCandidateStore(root / "records").load_job("job-001")
    assert restarted.state is CandidateJobState.FAILED
    assert restarted.stage is CandidateJobStage.FAILED
    assert restarted.error == "синтетический отказ conversion"
    assert not (root / "operations" / "previews" / "job-001.json").exists()
    assert not (root / "operations" / "work" / "job-001").exists()


def test_аварийный_checkpoint_переживает_рестарт_и_операция_возобновляется(
    tmp_path, monkeypatch
):
    IntakeCoordinator = _symbol("IntakeCoordinator")
    OperationError = _symbol("OperationError")
    CandidateJobStage = _intake_symbol("CandidateJobStage")
    operations = importlib.import_module(SUBJECT)

    root, uploads, records = _stores(tmp_path)
    coordinator = IntakeCoordinator(root / "operations", records)
    _accepted(uploads, coordinator)
    real_convert = operations.convert_collection

    def interrupt_conversion(_collection):
        raise KeyboardInterrupt("синтетическое аварийное завершение")

    monkeypatch.setattr(operations, "convert_collection", interrupt_conversion)
    with uploads.open_tree("candidate-001") as tree:
        with pytest.raises(KeyboardInterrupt):
            coordinator.prepare(
                "job-001",
                tree,
                action=IntakeAction.CREATE,
                active=None,
                generation_id="generation-001",
            )

    interrupted = DurableCandidateStore(root / "records").load_job("job-001")
    assert interrupted.state is CandidateJobState.PARSING
    assert interrupted.stage is CandidateJobStage.CONVERTING
    assert (root / "operations" / "work" / "job-001" / "collection").is_dir()

    monkeypatch.setattr(operations, "convert_collection", real_convert)
    restarted = IntakeCoordinator(
        root / "operations",
        DurableCandidateStore(root / "records"),
    )
    with BrowserStagingStore(root / "uploads").open_tree("candidate-001") as tree:
        with pytest.raises(OperationError, match="параметры операции"):
            restarted.prepare(
                "job-001",
                tree,
                action=IntakeAction.CREATE,
                active=None,
                generation_id="generation-other",
            )
    unchanged = DurableCandidateStore(root / "records").load_job("job-001")
    assert unchanged.state is CandidateJobState.PARSING
    assert unchanged.stage is CandidateJobStage.CONVERTING

    with BrowserStagingStore(root / "uploads").open_tree("candidate-001") as tree:
        preview = restarted.prepare(
            "job-001",
            tree,
            action=IntakeAction.CREATE,
            active=None,
            generation_id="generation-001",
        )

    assert preview.plan.no_op is False
    assert DurableCandidateStore(root / "records").load_job(
        "job-001"
    ).state is CandidateJobState.DONE


def test_commit_после_аварии_распознаёт_уже_опубликованный_target(
    tmp_path, monkeypatch
):
    IntakeCommitResult = _symbol("IntakeCommitResult")
    IntakeCoordinator = _symbol("IntakeCoordinator")

    root, uploads, records = _stores(tmp_path)
    coordinator = IntakeCoordinator(root / "operations", records)
    _accepted(uploads, coordinator)
    with uploads.open_tree("candidate-001") as tree:
        preview = coordinator.prepare(
            "job-001",
            tree,
            action=IntakeAction.CREATE,
            active=None,
            generation_id="generation-001",
        )
    registry = Registry(tmp_path / "data")

    def crash_after_publish(_pointer):
        raise SystemExit("синтетическая авария после Registry commit")

    monkeypatch.setattr(coordinator, "_after_publish", crash_after_publish)
    with pytest.raises(SystemExit, match="после Registry commit"):
        coordinator.confirm("job-001", registry)
    assert (
        registry.active_generation(preview.plan.identity)
        == preview.materialized.manifest
    )

    restarted = IntakeCoordinator(
        root / "operations",
        DurableCandidateStore(root / "records"),
    )
    result = restarted.confirm("job-001", registry)

    assert isinstance(result, IntakeCommitResult)
    assert result.no_op is False
    assert result.pointer == registry.active_generation_pointer(
        preview.plan.identity
    )
    assert result.applied_layers == set(LayerKind)
    assert restarted.load_commit("job-001") == result
    assert restarted.confirm("job-001", registry) == result


def test_commit_переживает_отказ_очистки_и_повтор_завершает_её(
    tmp_path,
    monkeypatch,
):
    IntakeCoordinator = _symbol("IntakeCoordinator")
    OperationError = _symbol("OperationError")

    root, uploads, records = _stores(tmp_path)
    coordinator = IntakeCoordinator(root / "operations", records)
    _accepted(uploads, coordinator)
    with uploads.open_tree("candidate-001") as tree:
        preview = coordinator.prepare(
            "job-001",
            tree,
            action=IntakeAction.CREATE,
            active=None,
            generation_id="generation-001",
        )
    registry = Registry(tmp_path / "data")

    def fail_cleanup(*_args, **_kwargs):
        raise OperationError("синтетический отказ очистки")

    monkeypatch.setattr(coordinator, "_discard_payload", fail_cleanup)
    with pytest.raises(OperationError, match="отказ очистки"):
        coordinator.confirm("job-001", registry)

    assert (
        registry.active_generation(preview.plan.identity)
        == preview.materialized.manifest
    )
    assert coordinator.load_commit("job-001").pointer == (
        registry.active_generation_pointer(preview.plan.identity)
    )
    assert (coordinator.work_dir / "job-001").is_dir()

    restarted = IntakeCoordinator(
        root / "operations",
        DurableCandidateStore(root / "records"),
    )
    result = restarted.confirm("job-001", registry)

    assert result.pointer == registry.active_generation_pointer(
        preview.plan.identity
    )
    assert not (restarted.work_dir / "job-001").exists()
    assert not (restarted.requests_dir / "job-001.json").exists()
    assert not (restarted.previews_dir / "job-001.json").exists()


def test_commit_распознаёт_тот_же_target_при_гонке_до_staging(
    tmp_path, monkeypatch
):
    IntakeCoordinator = _symbol("IntakeCoordinator")

    root, uploads, records = _stores(tmp_path)
    coordinator = IntakeCoordinator(root / "operations", records)
    _accepted(uploads, coordinator)
    with uploads.open_tree("candidate-001") as tree:
        preview = coordinator.prepare(
            "job-001",
            tree,
            action=IntakeAction.CREATE,
            active=None,
            generation_id="generation-001",
        )
    registry = Registry(tmp_path / "data")
    real_stage = registry.stage_generation

    def win_same_target(manifest, payloads):
        staged = real_stage(manifest, payloads)
        registry.publish_generation(staged, expected_previous=None)
        raise RuntimeError("синтетически проигранная гонка staging")

    monkeypatch.setattr(registry, "stage_generation", win_same_target)
    result = coordinator.confirm("job-001", registry)

    assert result.no_op is False
    assert result.pointer == registry.active_generation_pointer(preview.plan.identity)
    assert registry.active_generation(preview.plan.identity) == preview.materialized.manifest


def test_noop_commit_не_создаёт_staging_а_устаревший_preview_отклоняется(
    tmp_path,
):
    IntakeCoordinator = _symbol("IntakeCoordinator")
    OperationConflict = _symbol("OperationConflict")

    root, uploads, records = _stores(tmp_path)
    coordinator = IntakeCoordinator(root / "operations", records)
    _accepted(uploads, coordinator)
    with uploads.open_tree("candidate-001") as tree:
        coordinator.prepare(
            "job-001",
            tree,
            action=IntakeAction.CREATE,
            active=None,
            generation_id="generation-001",
        )
    registry = Registry(tmp_path / "data")
    created = coordinator.confirm("job-001", registry)

    _accepted(uploads, coordinator, "candidate-002", "job-002")
    active = registry.generation_view("DemoConfiguration")
    with uploads.open_tree("candidate-002") as tree:
        no_op_preview = coordinator.prepare(
            "job-002",
            tree,
            action=IntakeAction.UPDATE_FULL,
            active=active,
            generation_id="generation-002",
        )
    assert no_op_preview.plan.no_op
    staging_before = tuple((registry.data_dir / "generations").glob(".staging-*"))
    no_op = coordinator.confirm("job-002", registry)
    assert no_op.no_op is True
    assert no_op.pointer == created.pointer
    assert tuple((registry.data_dir / "generations").glob(".staging-*")) == staging_before

    _accepted(uploads, coordinator, "candidate-003", "job-003")
    with uploads.open_tree("candidate-003") as tree:
        stale = coordinator.prepare(
            "job-003",
            tree,
            action=IntakeAction.UPDATE_FULL,
            active=active,
            generation_id="generation-003",
        )
    competitor_manifest = replace(
        stale.materialized.manifest,
        generation_id="generation-competitor",
    )
    registry.publish_generation(
        registry.stage_generation(competitor_manifest, stale.materialized.payloads),
        expected_previous=created.pointer,
    )

    with pytest.raises(OperationConflict, match="active generation изменился"):
        coordinator.confirm("job-003", registry)
    with pytest.raises(KeyError):
        coordinator.load_commit("job-003")
    assert registry.active_generation(stale.plan.identity) == competitor_manifest


def test_commit_атомарно_отклоняет_смену_legacy_после_preview(tmp_path):
    IntakeCoordinator = _symbol("IntakeCoordinator")
    OperationConflict = _symbol("OperationConflict")

    root, uploads, records = _stores(tmp_path)
    coordinator = IntakeCoordinator(root / "operations", records)
    _accepted(uploads, coordinator)
    registry = Registry(tmp_path / "data")
    legacy_input = tmp_path / "legacy-input"
    legacy_input.mkdir()
    registry.add_configuration(
        write_export(
            legacy_input,
            build_configuration(name="DemoConfiguration", version="1.0"),
        ),
        keep_source=False,
    )
    active = registry.generation_view("DemoConfiguration")
    with uploads.open_tree("candidate-001") as tree:
        preview = coordinator.prepare(
            "job-001",
            tree,
            action=IntakeAction.UPDATE_FULL,
            active=active,
            generation_id="generation-001",
        )

    registry.add_configuration(
        write_export(
            legacy_input,
            build_configuration(name="DemoConfiguration", version="2.0"),
        ),
        keep_source=False,
    )
    changed = registry.generation_view("DemoConfiguration")
    assert changed != active

    with pytest.raises(OperationConflict, match="active generation изменился"):
        coordinator.confirm("job-001", registry)

    assert registry.active_generation(preview.plan.identity) is None
    assert registry.generation_view("DemoConfiguration") == changed
    assert not tuple((registry.data_dir / "generations").glob(".staging-*"))


def test_commit_сериализует_публикацию_с_конкурентной_legacy(
    tmp_path, monkeypatch
):
    IntakeCoordinator = _symbol("IntakeCoordinator")

    root, uploads, records = _stores(tmp_path)
    coordinator = IntakeCoordinator(root / "operations", records)
    _accepted(uploads, coordinator)
    registry = Registry(tmp_path / "data")
    legacy_input = tmp_path / "legacy-input"
    legacy_input.mkdir()
    registry.add_configuration(
        write_export(
            legacy_input,
            build_configuration(name="DemoConfiguration", version="1.0"),
        ),
        keep_source=False,
    )
    active = registry.generation_view("DemoConfiguration")
    with uploads.open_tree("candidate-001") as tree:
        preview = coordinator.prepare(
            "job-001",
            tree,
            action=IntakeAction.UPDATE_FULL,
            active=active,
            generation_id="generation-001",
        )

    real_build = registry._build_native_generation_runtime
    real_index = registry._configuration_index
    indexed = Event()
    competitor = []

    def signal_legacy_index(config, source, **kwargs):
        result = real_index(config, source, **kwargs)
        if config.version == "2.0":
            indexed.set()
        return result

    monkeypatch.setattr(registry, "_configuration_index", signal_legacy_index)

    def change_legacy_during_publish(root_path, manifest):
        prepared = real_build(root_path, manifest)
        # Настоящий конкурент ждёт mutation lock; синхронный вложенный вызов
        # add_configuration из publisher создавал бы искусственный deadlock.
        competitor.append(
            pool.submit(
                registry.add_configuration,
                write_export(
                    legacy_input,
                    build_configuration(name="DemoConfiguration", version="2.0"),
                ),
                keep_source=False,
            )
        )
        assert indexed.wait(10), "Source A не дошла до публикации"
        return prepared

    monkeypatch.setattr(
        registry,
        "_build_native_generation_runtime",
        change_legacy_during_publish,
    )

    with ThreadPoolExecutor(max_workers=1) as pool:
        result = coordinator.confirm("job-001", registry)
        with pytest.raises(RegistryError, match="изменилась.*Повторите загрузку"):
            competitor[0].result(timeout=10)

    assert registry.active_generation(preview.plan.identity) == preview.materialized.manifest
    assert registry.active_generation_pointer(preview.plan.identity) == result.pointer
    restarted = Registry(registry.data_dir)
    assert restarted.restore() == []
    assert restarted.generation_view("DemoConfiguration") == registry.generation_view(
        "DemoConfiguration"
    )
    assert not tuple((registry.data_dir / "generations").glob(".staging-*"))
