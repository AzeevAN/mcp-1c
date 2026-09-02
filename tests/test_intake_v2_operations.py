"""RED-контракты durable backend для preview единого intake."""

from __future__ import annotations

import importlib
import io
import zipfile

import pytest

from mcp1c.intake_v2 import (
    CandidateJobState,
    DurableCandidateStore,
    LayerKind,
)
from mcp1c.intake_v2_planner import IntakeAction
from mcp1c.intake_v2_transport import BrowserStagingStore
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
