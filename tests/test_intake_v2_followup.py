"""Доработки intake V2 после ревью: id каталогов, чтение записей, отказы.

Тесты закрепляют наблюдаемое поведение — ответ HTTP, содержимое диска,
права файлов — и подменяют внутренности только для моделирования сбоев.
"""

from __future__ import annotations

import asyncio
import io
import os
import stat
import threading
import uuid
from pathlib import Path

import pytest

from conftest import build_configuration, write_export
from mcp1c import intake_v2_fsio as fsio
from mcp1c.config_sources import ConfigSourceBindings
from mcp1c.intake_v2 import CandidateJobState, DurableCandidateStore
from mcp1c.intake_v2_api import IntakeApiConflict, IntakeApiService
from mcp1c.intake_v2_lifecycle import (
    CandidateCatalog,
    IntakeLifecycle,
    LifecycleConflict,
)
from mcp1c.intake_v2_operations import IntakeCoordinator
from mcp1c.intake_v2_transport import BrowserStagingStore
from mcp1c.registry import Registry
from test_intake_v2_api import _archive, _service
from test_intake_v2_collector import _configuration
from test_intake_v2_hardening import ADMIN, _client, _done_job, _extension_lifecycle


# 1. Один regexp для каталогов /config-sources и записей intake.


def test_каталог_с_подчёркиванием_привязывается_и_обновляется(tmp_path):
    registry = Registry(tmp_path / "data")
    source = tmp_path / "base"
    source.mkdir()
    registry.add_configuration(
        write_export(source, build_configuration(name="Demo")), keep_source=False
    )
    roots = tmp_path / "mounts"
    (roots / "_erp").mkdir(parents=True)
    (roots / "_erp" / "Configuration.xml").write_bytes(_configuration("Demo"))
    service = IntakeApiService.for_registry(
        registry, config_sources_root=roots, directory_settle_seconds=0
    )

    assert service.directory_sources()["roots"] == ["_erp"]
    state = service.bind_directory("Demo", "_erp")
    candidate = service.refresh_directory("Demo")

    assert state["bindings"] == {"Demo": "_erp"}
    # Запись каталога кандидатов с таким source_id читается и после рестарта.
    restarted = IntakeApiService.for_registry(
        registry, config_sources_root=roots, directory_settle_seconds=0
    )
    assert restarted.lifecycle.catalog.load(candidate["id"]).locator.source_id == "_erp"
    assert restarted.directory_sources()["bindings"] == {"Demo": "_erp"}


# 2. Чтение записи не путает атомарную замену с подменой.


def test_read_record_не_отказывает_на_атомарной_замене(tmp_path):
    path = tmp_path / "job.json"
    fsio.write_record_atomic(path, b'{"n": 0}')
    stop = threading.Event()

    def writer():
        n = 0
        while not stop.is_set():
            n += 1
            fsio.write_record_atomic(path, f'{{"n": {n}}}'.encode())

    thread = threading.Thread(target=writer)
    thread.start()
    errors: list[Exception] = []
    try:
        for _ in range(3000):
            try:
                assert fsio.read_record(path, limit=1024).startswith(b'{"n": ')
            except Exception as error:  # проверяется ниже
                errors.append(error)
    finally:
        stop.set()
        thread.join(5)
    assert errors == []


def test_read_record_отвергает_fifo_и_ссылку_без_зависания(tmp_path):
    fifo = tmp_path / "fifo.json"
    os.mkfifo(fifo)
    with pytest.raises(fsio.RecordTypeError):
        fsio.read_record(fifo, limit=1024)

    target = tmp_path / "target.json"
    target.write_bytes(b"{}")
    link = tmp_path / "link.json"
    link.symlink_to(target)
    with pytest.raises(fsio.RecordTypeError):
        fsio.read_record(link, limit=1024)

    big = tmp_path / "big.json"
    big.write_bytes(b"x" * 2048)
    with pytest.raises(fsio.RecordTooLargeError):
        fsio.read_record(big, limit=1024)


# 3. Повреждённое хранилище при снятии конфигурации — 409, а не 500.


def test_снятие_конфигурации_при_нечитаемой_job_даёт_понятный_409(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("ADMIN_TOKEN", "admin-token")
    registry = Registry(tmp_path / "data")
    source = tmp_path / "base"
    source.mkdir()
    registry.add_configuration(
        write_export(source, build_configuration(name="Demo")), keep_source=False
    )
    service = _service(registry)
    jobs_dir = service.lifecycle.operations.records.jobs_dir
    (jobs_dir / "job-broken.json").write_text("{не json", encoding="utf-8")
    client = _client(registry, service)

    response = client.post(
        "/api/v1/sources/remove",
        headers=ADMIN,
        json={"id": "Demo", "confirmation": "Demo"},
    )

    assert response.status_code == 409
    assert "job-broken" in response.json()["error"]
    # Консервативно: конфигурация не снята, нечитаемая запись не стёрта.
    assert registry.snapshot().configuration_names == ("Demo",)
    assert (jobs_dir / "job-broken.json").is_file()


# 9. Нечитаемая запись job не выключает intake.


def test_нечитаемая_job_не_ломает_старт_и_список(tmp_path, monkeypatch):
    monkeypatch.setenv("ADMIN_TOKEN", "admin-token")
    registry = Registry(tmp_path / "data")
    service = _service(registry)
    client = _client(registry, service)
    _candidate_id, done_job = _done_job(client)
    jobs_dir = service.lifecycle.operations.records.jobs_dir
    (jobs_dir / "job-broken.json").write_text('{"format_version": 999}', "utf-8")

    restarted = _service(registry)
    listed = restarted.jobs_snapshot()["jobs"]

    by_id = {job["job_id"]: job for job in listed}
    assert by_id[done_job]["state"] == "done"
    assert by_id[done_job]["broken"] is False
    assert by_id["job-broken"]["broken"] is True
    assert by_id["job-broken"]["discardable"] is False
    assert "несовместим" in by_id["job-broken"]["error"]
    with pytest.raises(IntakeApiConflict, match="job-broken"):
        restarted.ensure_configuration_purgeable("DemoConfiguration")


def test_рестарт_закрывает_оборванные_job_даже_рядом_с_битой(tmp_path):
    managed = tmp_path / "managed"
    records = DurableCandidateStore(managed / "records")
    operations = IntakeCoordinator(managed / "operations", records)
    browser = BrowserStagingStore(managed / "uploads")
    raw = _archive()
    browser.accept("candidate-001", "c.zip", io.BytesIO(raw), expected_size=len(raw))
    lifecycle = IntakeLifecycle(CandidateCatalog(managed / "catalog"), browser, operations)
    candidate_id = lifecycle.refresh().candidates[0].candidate_id
    lifecycle.start("job-ready", candidate_id)
    (records.jobs_dir / "job-broken.json").write_text("{", encoding="utf-8")

    IntakeLifecycle(
        CandidateCatalog(managed / "catalog"),
        BrowserStagingStore(managed / "uploads"),
        IntakeCoordinator(managed / "operations", DurableCandidateStore(managed / "records")),
    )

    assert records.load_job("job-ready").state is CandidateJobState.FAILED


# 5. Опубликованного держателя кандидата отменить нельзя — отказ так и говорит.


def test_extension_опубликованная_job_даёт_отдельный_отказ(tmp_path, monkeypatch):
    lifecycle, records, candidate_id = _extension_lifecycle(tmp_path)
    lifecycle.start("job-a", candidate_id, parent_configuration="ParentA")
    records.save_job(records.load_job("job-a").transition(CandidateJobState.PARSING))
    records.save_job(
        records.load_job("job-a").transition(CandidateJobState.DONE, result="job-a")
    )
    real = lifecycle.operations.load_commit

    def committed(job_id):
        if job_id == "job-a":
            return object()
        return real(job_id)

    monkeypatch.setattr(lifecycle.operations, "load_commit", committed)

    with pytest.raises(LifecycleConflict, match="опубликовано") as error:
        lifecycle.start("job-b", candidate_id, parent_configuration="ParentB")
    assert "отмените preview" not in str(error.value)


# 6. role tools сверяются при любом исходе confirm.


def test_role_tools_обновляются_при_необработанной_ошибке_confirm(
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

    def broken_confirm(_job_id):
        raise OSError("диск отключён посреди публикации")

    monkeypatch.setattr(service, "confirm", broken_confirm)
    with pytest.raises(OSError):
        client.post("/api/v1/sources/intake/confirm", headers=ADMIN, json={"job_id": job_id})
    assert calls == [1]


# 7. Атомарная замена не сужает права записи.


def test_write_record_atomic_сохраняет_права(tmp_path):
    umask = os.umask(0)
    os.umask(umask)
    fresh = tmp_path / "fresh.json"
    fsio.write_record_atomic(fresh, b"{}")
    assert stat.S_IMODE(fresh.stat().st_mode) == 0o644 & ~umask

    existing = tmp_path / "existing.json"
    existing.write_bytes(b"{}")
    existing.chmod(0o640)
    fsio.write_record_atomic(existing, b'{"v": 2}')
    assert stat.S_IMODE(existing.stat().st_mode) == 0o640


# 8. Остатки оборванной атомарной записи убираются при открытии.


def test_остатки_tmp_состояния_incoming_и_привязок_убираются(tmp_path):
    from mcp1c.incoming import IncomingScanner

    registry = Registry(tmp_path / "data")
    registry.data_dir.mkdir(parents=True, exist_ok=True)
    state_tmp = registry.data_dir / ".incoming-state.json.abc123.tmp"
    bindings = registry.data_dir / "config-source-bindings.json"
    bindings_tmp = registry.data_dir / ".config-source-bindings.json.def456.tmp"
    unrelated = registry.data_dir / ".другое.tmp"
    for path in (state_tmp, bindings_tmp, unrelated):
        path.write_bytes(b"partial")

    IncomingScanner(registry)
    ConfigSourceBindings(bindings, tmp_path / "mounts")

    assert not state_tmp.exists()
    assert not bindings_tmp.exists()
    assert unrelated.exists()


# 10. Тяжёлая инициализация не в event loop; один fsync-цикл на показ.


def test_сервис_intake_создаётся_вне_event_loop(tmp_path, monkeypatch):
    from mcp1c import dashboard_runtime

    monkeypatch.setenv("ADMIN_TOKEN", "admin-token")
    registry = Registry(tmp_path / "data")
    loops: list[bool] = []
    real = IntakeApiService.for_registry.__func__

    def recording(cls, *args, **kwargs):
        try:
            asyncio.get_running_loop()
            loops.append(True)
        except RuntimeError:
            loops.append(False)
        return real(cls, *args, **kwargs)

    monkeypatch.setattr(
        dashboard_runtime.IntakeApiService, "for_registry", classmethod(recording)
    )
    client = _client(registry, None)

    response = client.get("/api/v1/sources/intake/jobs", headers=ADMIN)

    assert response.status_code == 200
    assert loops == [False]


def test_показ_incoming_пишет_состояние_один_раз_за_проход(tmp_path, monkeypatch):
    import time

    from mcp1c.incoming import IncomingScanner

    registry = Registry(tmp_path / "data")
    registry.incoming_dir.mkdir(parents=True, exist_ok=True)
    old = time.time() - 60
    for i in range(5):
        path = registry.incoming_dir / f"f{i}.zip"
        path.write_bytes(os.urandom(64))
        os.utime(path, (old, old))
    scanner = IncomingScanner(registry)
    writes: list[Path] = []
    real = fsio.write_record_atomic

    def counting(path, payload, **kwargs):
        writes.append(path)
        return real(path, payload, **kwargs)

    monkeypatch.setattr(fsio, "write_record_atomic", counting)
    scanner.scan()

    assert len(writes) == 1
    # Повторный показ при неизменных файлах ничего не пишет.
    scanner.scan()
    assert len(writes) == 1


# 11. Идентификаторы, которые сервер генерирует сам, проходят общее правило.


def test_генерируемые_идентификаторы_проходят_общее_правило():
    import secrets

    generated = [
        f"schema-v1-{uuid.uuid4().hex}",  # registry: schema-v1 поверх native
        f"generation-{secrets.token_hex(16)}",  # intake: новое поколение
        f"job-{secrets.token_hex(16)}",
        f"candidate-{secrets.token_hex(16)}",
        f"source-{'a' * 40}",
        uuid.uuid4().hex,  # recovery_id activation
    ]
    assert all(fsio.is_safe_identifier(value) for value in generated)


def test_schema_v1_поколение_проходит_verify_после_рестарта(tmp_path):
    from test_intake_v2_runtime import (
        test_schema_v1_самостоятельно_публикуется_поверх_несовместимого_native as scenario,
    )

    # Сценарий публикует A_ONLY-поколение с id `schema-v1-<uuid>` поверх
    # native; новый Registry при restore проверяет все pointer-ы, и строгий
    # regexp идентификатора не должен сделать их нечитаемыми.
    scenario(tmp_path)
    restarted = Registry(tmp_path / "data")
    # Сообщения об уборке кэша индексов допустимы; падения verify — нет.
    restarted.startup()
    pointers = list(restarted._generation_pointers.values())
    assert pointers
    assert any(p.generation_id.startswith("schema-v1-") for p in pointers)
    assert all(fsio.is_safe_identifier(p.generation_id) for p in pointers)


def test_запись_проходит_на_фс_без_chmod_и_не_теряет_дескриптор(tmp_path, monkeypatch):
    from mcp1c import intake_v2_fsio as fsio

    def refuse(*_args, **_kwargs):
        raise PermissionError(1, "Operation not permitted")

    monkeypatch.setattr(fsio.os, "fchmod", refuse)
    before = len(os.listdir("/dev/fd"))
    target = tmp_path / "record.json"
    for index in range(20):
        fsio.write_record_atomic(target, f'{{"n": {index}}}'.encode())
    assert target.read_bytes() == b'{"n": 19}'
    assert len(os.listdir("/dev/fd")) == before
    assert [p.name for p in tmp_path.iterdir()] == ["record.json"]
