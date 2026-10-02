"""Границы идентификаторов, списка job и восстановления intake V2.

Каждый тест закрепляет наблюдаемый отказ: что сервер отвечает клиенту и что
остаётся на диске. Внутренние вызовы подменяются только там, где без этого
нельзя смоделировать сбой файловой системы.
"""

from __future__ import annotations

import io
import json
import os
import time
from pathlib import Path

import pytest
from starlette.applications import Starlette

from conftest import живой_клиент
from mcp1c import intake_v2_fsio as fsio
from mcp1c.dashboard_runtime import DASHBOARD_ON, routes
from mcp1c.intake_v2 import CandidateJobState, DurableCandidateStore
from mcp1c.intake_v2_lifecycle import (
    CandidateCatalog,
    IntakeLifecycle,
    LifecycleConflict,
    LifecycleError,
)
from mcp1c.intake_v2_operations import IntakeCoordinator, OperationError
from mcp1c.intake_v2_transport import BrowserStagingStore
from mcp1c.registry import Registry
from test_intake_v2_api import _archive, _service, _wait_job
from test_intake_v2_lifecycle import _archive as _write_zip
from test_intake_v2_lifecycle import _backend, _extension


ADMIN = {"x-api-token": "admin-token"}


def _client(registry: Registry, service, refresh=None):
    return живой_клиент(
        Starlette(
            routes=routes(
                registry,
                mode=DASHBOARD_ON,
                intake=service,
                role_tools_refresh=refresh,
            )
        )
    )


def _done_job(client) -> tuple[str, str]:
    candidate = client.post(
        "/api/v1/sources/intake/upload",
        headers=ADMIN,
        files={"file": ("configuration.zip", _archive())},
    ).json()["candidate"]
    started = client.post(
        "/api/v1/sources/intake/start",
        headers=ADMIN,
        json={"candidate_id": candidate["id"], "action": "create"},
    )
    assert started.status_code == 202
    job_id = started.json()["job"]["job_id"]
    assert _wait_job(client, job_id)["state"] == "done"
    return candidate["id"], job_id


@pytest.mark.parametrize(
    "value",
    ["", ".", "..", ".hidden", "-flag", "a b", "кириллица", "a/b", "a" * 129, None],
)
def test_общий_идентификатор_отвергает_пути_и_мусор(value):
    assert fsio.is_safe_identifier(value) is False


@pytest.mark.parametrize("value", ["job-1", "a", "a.b_c-d", "A" * 128])
def test_общий_идентификатор_принимает_штатные_id(value):
    assert fsio.is_safe_identifier(value) is True


@pytest.mark.parametrize("job_id", ["..", "."])
def test_job_id_точки_не_стирают_каталог_операций(tmp_path, monkeypatch, job_id):
    monkeypatch.setenv("ADMIN_TOKEN", "admin-token")
    registry = Registry(tmp_path / "data")
    service = _service(registry)
    client = _client(registry, service)
    candidate_id, done_job = _done_job(client)
    operations = service.lifecycle.operations
    preview = operations.previews_dir / f"{done_job}.json"
    assert preview.is_file()

    response = client.post(
        "/api/v1/sources/intake/start",
        headers=ADMIN,
        json={
            "candidate_id": candidate_id,
            "action": "create",
            "job_id": job_id,
        },
    )
    # Дать фоновой задаче шанс выполнить rmtree, если бы старт был принят.
    deadline = time.monotonic() + 2
    while time.monotonic() < deadline and preview.is_file():
        time.sleep(0.05)

    assert response.status_code == 422
    assert preview.is_file()
    assert (operations.work_dir / done_job).is_dir()
    listed = client.get("/api/v1/sources/intake/jobs", headers=ADMIN)
    assert listed.status_code == 200
    assert [job["job_id"] for job in listed.json()["jobs"]] == [done_job]


def test_reset_work_не_выходит_за_каталог_work(tmp_path):
    records = DurableCandidateStore(tmp_path / "records")
    operations = IntakeCoordinator(tmp_path / "operations", records)
    outside = tmp_path / "operations" / "requests" / "keep.json"
    outside.write_text("{}", encoding="utf-8")

    for bad in ("..", ".", "../requests"):
        with pytest.raises(OperationError, match="недопустимый формат"):
            operations._reset_work(bad)
        with pytest.raises(OperationError, match="недопустимый формат"):
            operations._discard_work(bad)
    assert outside.is_file()


@pytest.mark.parametrize("job_id", ["a b", "кириллица", "x" * 300, "..", "-x"])
def test_невалидный_job_id_даёт_422_а_не_500(tmp_path, monkeypatch, job_id):
    monkeypatch.setenv("ADMIN_TOKEN", "admin-token")
    registry = Registry(tmp_path / "data")
    service = _service(registry)
    client = _client(registry, service)
    candidate = client.post(
        "/api/v1/sources/intake/upload",
        headers=ADMIN,
        files={"file": ("configuration.zip", _archive())},
    ).json()["candidate"]

    started = client.post(
        "/api/v1/sources/intake/start",
        headers=ADMIN,
        json={"candidate_id": candidate["id"], "action": "create", "job_id": job_id},
    )
    confirmed = client.post(
        "/api/v1/sources/intake/confirm", headers=ADMIN, json={"job_id": job_id}
    )
    discarded = client.post(
        "/api/v1/sources/intake/discard", headers=ADMIN, json={"job_id": job_id}
    )

    assert started.status_code == 422
    assert confirmed.status_code == 422
    assert discarded.status_code == 422
    for response in (started, confirmed, discarded):
        assert "job_id" in response.json()["error"]


def test_невалидный_job_id_в_пути_даёт_422(tmp_path, monkeypatch):
    monkeypatch.setenv("ADMIN_TOKEN", "admin-token")
    registry = Registry(tmp_path / "data")
    client = _client(registry, _service(registry))

    response = client.get("/api/v1/sources/intake/jobs/a%20b", headers=ADMIN)

    assert response.status_code == 422


def test_повреждённая_запись_job_даёт_409_а_не_500(tmp_path, monkeypatch):
    monkeypatch.setenv("ADMIN_TOKEN", "admin-token")
    registry = Registry(tmp_path / "data")
    service = _service(registry)
    client = _client(registry, service)
    _candidate_id, job_id = _done_job(client)
    (service.lifecycle.operations.records.jobs_dir / f"{job_id}.json").write_text(
        "{не json", encoding="utf-8"
    )

    response = client.get(f"/api/v1/sources/intake/jobs/{job_id}", headers=ADMIN)
    confirmed = client.post(
        "/api/v1/sources/intake/confirm", headers=ADMIN, json={"job_id": job_id}
    )

    assert response.status_code == 409
    assert confirmed.status_code == 409


def test_битая_done_job_не_роняет_список_intake(tmp_path, monkeypatch):
    monkeypatch.setenv("ADMIN_TOKEN", "admin-token")
    registry = Registry(tmp_path / "data")
    service = _service(registry)
    client = _client(registry, service)
    _first_candidate, broken = _done_job(client)
    operations = service.lifecycle.operations
    (operations.previews_dir / f"{broken}.json").unlink()

    listed = client.get("/api/v1/sources/intake/jobs", headers=ADMIN)
    snapshot = client.get("/api/v1/sources/intake", headers=ADMIN)

    assert listed.status_code == 200
    assert snapshot.status_code == 200
    (job,) = listed.json()["jobs"]
    assert job["job_id"] == broken
    assert job["state"] == "failed"
    assert "preview" in job["error"]
    assert snapshot.json()["jobs"] == listed.json()["jobs"]

    # Битую job можно убрать из того же списка.
    discarded = client.post(
        "/api/v1/sources/intake/discard", headers=ADMIN, json={"job_id": broken}
    )
    assert discarded.status_code == 200
    assert client.get("/api/v1/sources/intake/jobs", headers=ADMIN).json()[
        "jobs"
    ] == []


def test_discard_сначала_снимает_job_а_остаток_work_убирается_при_старте(
    tmp_path, monkeypatch
):
    registry = Registry(tmp_path / "data")
    service = _service(registry)
    raw = _archive()
    candidate = service.accept_upload(
        "configuration.zip", io.BytesIO(raw), expected_size=len(raw)
    )
    work = service.start(candidate["id"], "create", job_id="job-half-discard")
    service.prepare(work)
    operations = service.lifecycle.operations

    def broken_rmtree(path, *args, **kwargs):
        raise OSError("диск недоступен")

    monkeypatch.setattr("mcp1c.intake_v2_operations.shutil.rmtree", broken_rmtree)
    with pytest.raises(OperationError):
        service.discard(work.job_id)
    monkeypatch.undo()

    # Job уже снята: список не упирается в недоделанную очистку.
    assert service.jobs_snapshot() == {"jobs": []}
    assert (operations.work_dir / work.job_id).is_dir()

    restarted = _service(registry)
    restarted_operations = restarted.lifecycle.operations
    assert not (restarted_operations.work_dir / work.job_id).exists()
    for directory in (
        restarted_operations.requests_dir,
        restarted_operations.previews_dir,
    ):
        assert not (directory / f"{work.job_id}.json").exists()


@pytest.mark.parametrize("state", ["accepted", "probing", "ready"])
def test_рестарт_закрывает_осиротевшую_job_до_parse(tmp_path, state):
    managed, browser, records, operations = _backend(tmp_path)
    raw = _write_zip(tmp_path / "configuration.zip", _archive_descriptor())
    browser.accept(
        "candidate-001", "configuration.zip", io.BytesIO(raw), expected_size=len(raw)
    )
    lifecycle = IntakeLifecycle(
        CandidateCatalog(managed / "catalog"), browser, operations
    )
    discovered = lifecycle.refresh().candidates[0]
    if state == "accepted":
        operations.create_job("job-orphan", discovered.candidate_id)
    elif state == "probing":
        job = operations.create_job("job-orphan", discovered.candidate_id)
        records.save_job(job.transition(CandidateJobState.PROBING))
    else:
        lifecycle.start("job-orphan", discovered.candidate_id)
    assert records.load_job("job-orphan").state.value == state

    restarted = IntakeLifecycle(
        CandidateCatalog(managed / "catalog"),
        BrowserStagingStore(managed / "uploads"),
        IntakeCoordinator(
            managed / "operations", DurableCandidateStore(managed / "records")
        ),
    )

    failed = restarted.operations.records.load_job("job-orphan")
    assert failed.state is CandidateJobState.FAILED
    assert "перезапуском" in failed.error
    # FAILED больше не держит конфигурацию и browser ZIP.
    restarted.ensure_configuration_purgeable("DemoConfiguration")
    restarted.remove_browser_candidate(discovered.candidate_id)
    assert browser.candidate_ids() == ()


def _archive_descriptor() -> bytes:
    from test_intake_v2_collector import _configuration

    return _configuration("DemoConfiguration")


def _extension_lifecycle(tmp_path: Path):
    managed, browser, records, operations = _backend(tmp_path)
    incoming = tmp_path / "incoming"
    _write_zip(incoming / "extension.zip", _extension())
    lifecycle = IntakeLifecycle(
        CandidateCatalog(managed / "catalog"),
        browser,
        operations,
        incoming_root=incoming,
    )
    discovered = lifecycle.refresh().candidates[0]
    return lifecycle, records, discovered.candidate_id


def test_extension_после_failed_job_принимает_другого_родителя(tmp_path):
    lifecycle, records, candidate_id = _extension_lifecycle(tmp_path)
    lifecycle.start("job-a", candidate_id, parent_configuration="ParentA")
    lifecycle.operations.fail("job-a", OperationError("отменено"))

    candidate = lifecycle.start("job-b", candidate_id, parent_configuration="ParentB")

    assert candidate.identity.parent_configuration == "ParentB"
    assert records.load_job("job-b").state is CandidateJobState.READY


def test_extension_после_discard_не_помнит_прежнего_родителя(tmp_path):
    lifecycle, records, candidate_id = _extension_lifecycle(tmp_path)
    lifecycle.start("job-a", candidate_id, parent_configuration="ParentA")
    job = records.load_job("job-a")
    records.save_job(job.transition(CandidateJobState.PARSING))
    records.save_job(
        records.load_job("job-a").transition(CandidateJobState.DONE, result="job-a")
    )
    lifecycle.discard("job-a")

    with pytest.raises(KeyError):
        records.load_candidate(candidate_id)
    candidate = lifecycle.start("job-b", candidate_id, parent_configuration="ParentB")
    assert candidate.identity.parent_configuration == "ParentB"


def test_extension_с_живой_job_другого_родителя_даёт_понятный_отказ(tmp_path):
    lifecycle, records, candidate_id = _extension_lifecycle(tmp_path)
    lifecycle.start("job-a", candidate_id, parent_configuration="ParentA")

    with pytest.raises(LifecycleConflict, match="ParentA"):
        lifecycle.start("job-b", candidate_id, parent_configuration="ParentB")
    with pytest.raises(KeyError):
        records.load_job("job-b")
    assert records.load_candidate(candidate_id).identity.parent_configuration == (
        "ParentA"
    )


def test_role_tools_обновляются_даже_если_очистка_после_confirm_упала(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("ADMIN_TOKEN", "admin-token")
    registry = Registry(tmp_path / "data")
    service = _service(registry)
    calls: list[int] = []

    async def refresh() -> bool:
        calls.append(1)
        return True

    client = _client(registry, service, refresh)
    _candidate_id, job_id = _done_job(client)

    def broken_release(_job_id):
        raise LifecycleError("не удалось удалить browser ZIP")

    monkeypatch.setattr(
        service.lifecycle, "release_committed_candidate", broken_release
    )
    confirmed = client.post(
        "/api/v1/sources/intake/confirm", headers=ADMIN, json={"job_id": job_id}
    )

    assert confirmed.status_code == 422
    assert registry.snapshot().configuration_names == ("DemoConfiguration",)
    assert calls == [1]


def test_start_parsing_job_не_запускает_второй_parse(tmp_path):
    registry = Registry(tmp_path / "data")
    service = _service(registry)
    raw = _archive()
    candidate = service.accept_upload(
        "configuration.zip", io.BytesIO(raw), expected_size=len(raw)
    )
    service.start(candidate["id"], "create", job_id="job-stuck")
    records = service.lifecycle.operations.records
    records.save_job(records.load_job("job-stuck").transition(CandidateJobState.PARSING))

    from mcp1c.intake_v2_api import IntakeApiConflict

    with pytest.raises(IntakeApiConflict, match="выполняется"):
        service.start(candidate["id"], "create", job_id="job-stuck")
    assert records.load_job("job-stuck").state is CandidateJobState.PARSING


def test_ошибка_mkdir_при_копировании_слоя_не_оставляет_дескриптор(
    tmp_path, monkeypatch
):
    from mcp1c.intake_v2 import LayerKind
    from mcp1c.intake_v2_registry import BundleStoreError, GenerationBundleStore

    source = tmp_path / "layer.json"
    source.write_bytes(b"{}")
    store = GenerationBundleStore(tmp_path / "data")
    target = tmp_path / "blocked" / "layer.json"
    real_mkdir = Path.mkdir

    def broken_mkdir(self, *args, **kwargs):
        if self == target.parent:
            raise OSError("нет места")
        return real_mkdir(self, *args, **kwargs)

    monkeypatch.setattr(Path, "mkdir", broken_mkdir)
    before = len(os.listdir("/dev/fd"))
    with pytest.raises(BundleStoreError):
        store._copy_layer(LayerKind.BASE_STRUCTURE, source, target)
    assert len(os.listdir("/dev/fd")) == before


def test_incoming_state_пишется_атомарно_и_забывает_удалённые_файлы(
    tmp_path, monkeypatch
):
    from mcp1c.incoming import IncomingScanner

    registry = Registry(tmp_path / "data")
    registry.incoming_dir.mkdir(parents=True, exist_ok=True)
    kept = registry.incoming_dir / "kept.zip"
    gone = registry.incoming_dir / "gone.zip"
    for path in (kept, gone):
        path.write_bytes(b"zip")
        os.utime(path, (time.time() - 60, time.time() - 60))
    scanner = IncomingScanner(registry)
    scanner.scan()
    state_path = registry.data_dir / "incoming-state.json"
    assert set(json.loads(state_path.read_text(encoding="utf-8"))["digests"]) == {
        "gone.zip",
        "kept.zip",
    }

    gone.unlink()
    scanner.scan()
    assert set(json.loads(state_path.read_text(encoding="utf-8"))["digests"]) == {
        "kept.zip"
    }

    before = state_path.read_bytes()
    scanner.note_failure(kept, "сбой")

    def broken_replace(*_args, **_kwargs):
        raise OSError("диск отключён")

    monkeypatch.setattr("mcp1c.intake_v2_fsio.os.replace", broken_replace)
    scanner.note_failure(kept, "другой сбой")
    monkeypatch.undo()
    stored = json.loads(state_path.read_text(encoding="utf-8"))
    assert stored["failures"]["kept.zip"]["reason"] == "сбой"
    assert before != state_path.read_bytes()
    assert not [p for p in state_path.parent.iterdir() if p.name.endswith(".tmp")]
