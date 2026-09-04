"""Тяжёлый upload не создаёт очередь и не занимает допуск HTTP-чтения."""

import asyncio
from pathlib import Path
import shutil
from threading import Event

import anyio
import httpx2 as httpx
import pytest
from starlette.applications import Starlette

from mcp1c import dashboard_backend
from mcp1c.dashboard_runtime import routes
from mcp1c.registry import Registry
from mcp1c.server import mcp_guard


async def _wait(predicate):
    for _ in range(400):
        if predicate():
            return
        await asyncio.sleep(0.005)
    raise AssertionError("синтетический worker не достиг барьера")


@pytest.mark.parametrize("check", ["admission", "read", "mixed"])
def test_upload_ограничен_до_body_и_не_занимает_пул_чтения(tmp_path, monkeypatch, check):
    monkeypatch.setenv("API_TOKEN", "synthetic-read")
    monkeypatch.setenv("ADMIN_TOKEN", "synthetic-admin")
    monkeypatch.setattr(dashboard_backend, "_JOBS", [])
    monkeypatch.setattr(dashboard_backend, "_ФОНОВЫЕ", set())
    entered, release = Event(), Event()
    saved = []

    def worker(registry, job, directory, path, suffix, **kwargs):
        saved.append(Path(directory))
        entered.set()
        try:
            assert release.wait(10)
            job["state"] = dashboard_backend.JOB_DONE
        finally:
            shutil.rmtree(directory)

    monkeypatch.setattr(dashboard_backend, "_run_job", worker)

    async def scenario():
        limiter = anyio.to_thread.current_default_thread_limiter()
        previous = limiter.total_tokens
        limiter.total_tokens = 1
        registry = Registry(tmp_path / "data")
        app = mcp_guard(Starlette(routes=routes(registry)))
        headers = {"x-api-token": "synthetic-admin"}
        read = None
        try:
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver", headers=headers) as client:
                first = await client.post("/api/v1/sources/upload", files={"file": ("first.hbk", b"synthetic")})
                assert first.status_code == 202
                await _wait(entered.is_set)
                if check == "read":
                    read = asyncio.create_task(client.get("/api/v1/sources"))
                    done, _ = await asyncio.wait({read}, timeout=0.2)
                    assert done, "чтение ждёт общий пул, занятый upload"
                    assert read.result().status_code == 200
                else:
                    paths = ["/api/v1/sources/upload"] if check == "admission" else [
                        "/api/v1/sources/intake/upload", "/api/v1/sources/intake/start",
                        "/api/v1/sources/intake/confirm", "/api/v1/sources/intake/discard",
                        "/api/v1/sources/incoming/parse",
                    ]
                    for path in paths:
                        consumed = False

                        async def body():
                            nonlocal consumed
                            consumed = True
                            yield b"body must not be read"

                        response = await client.post(path, content=body())
                        assert response.status_code == 409, (path, response.text)
                        assert not consumed
                    assert len(dashboard_backend._JOBS) == 1
                    assert len(dashboard_backend._ФОНОВЫЕ) == 1
        finally:
            release.set()
            await asyncio.gather(*tuple(dashboard_backend._ФОНОВЫЕ), return_exceptions=True)
            if read is not None:
                await read
            limiter.total_tokens = previous
        assert saved and all(not path.exists() for path in saved)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver", headers=headers) as client:
            response = await client.post("/api/v1/sources/upload", files={"file": ("invalid.txt", b"synthetic")})
            assert response.status_code == 400, "допуск не освободился после worker"

    asyncio.run(scenario())


@pytest.mark.parametrize("failure", ["parse", "write"])
def test_ошибка_загрузки_освобождает_допуск(tmp_path, monkeypatch, failure):
    from starlette.datastructures import UploadFile

    monkeypatch.setenv("API_TOKEN", "synthetic-read")
    monkeypatch.setenv("ADMIN_TOKEN", "synthetic-admin")
    monkeypatch.setattr(dashboard_backend, "_JOBS", [])
    if failure == "write":
        async def failed_read(*args, **kwargs):
            raise OSError("синтетический отказ диска")
        monkeypatch.setattr(UploadFile, "read", failed_read)

    async def scenario():
        app = mcp_guard(Starlette(routes=routes(Registry(tmp_path / "data"))))
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver", headers={"x-api-token": "synthetic-admin"}) as client:
            response = await client.post("/api/v1/sources/upload", files={"file": ("broken.hbk", b"invalid archive")})
            assert response.status_code == (500 if failure == "write" else 202)
            await _wait(lambda: not dashboard_backend._ФОНОВЫЕ)
            if failure == "parse":
                assert dashboard_backend._JOBS[-1]["state"] == dashboard_backend.JOB_FAILED
            else:
                assert dashboard_backend._JOBS == []
            retry = await client.post("/api/v1/sources/upload", files={"file": ("invalid.txt", b"synthetic")})
            assert retry.status_code == 400

    asyncio.run(scenario())


@pytest.mark.parametrize("stage", ["multipart", "copy"])
def test_отмена_приёма_закрывает_spool_и_удаляет_частичную_job(tmp_path, monkeypatch, stage):
    from starlette import formparsers
    from starlette.datastructures import UploadFile
    from mcp1c import dashboard_runtime

    monkeypatch.setenv("API_TOKEN", "synthetic-read")
    monkeypatch.setenv("ADMIN_TOKEN", "synthetic-admin")
    monkeypatch.setattr(dashboard_backend, "_JOBS", [])
    spools, directories = [], []
    real_spool = formparsers.SpooledTemporaryFile
    real_mkdtemp = dashboard_runtime.tempfile.mkdtemp

    def spool(*args, **kwargs):
        result = real_spool(*args, **kwargs)
        spools.append(result)
        return result

    def directory(*args, **kwargs):
        result = real_mkdtemp(*args, **kwargs)
        directories.append(Path(result))
        return result

    monkeypatch.setattr(formparsers, "SpooledTemporaryFile", spool)
    monkeypatch.setattr(dashboard_runtime.tempfile, "mkdtemp", directory)

    async def scenario():
        entered = asyncio.Event()
        never = asyncio.Event()
        real_read = UploadFile.read

        async def paused_read(file, *args, **kwargs):
            if file.filename == "cancel.hbk":
                entered.set()
                await never.wait()
            return await real_read(file, *args, **kwargs)

        if stage == "copy":
            monkeypatch.setattr(UploadFile, "read", paused_read)
        app = mcp_guard(Starlette(routes=routes(Registry(tmp_path / "data"))))
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver", headers={"x-api-token": "synthetic-admin"}) as client:
            async def body():
                yield b'--part\r\nContent-Disposition: form-data; name="file"; filename="cancel.hbk"\r\nContent-Type: application/octet-stream\r\n\r\nsynthetic'
                entered.set()
                await never.wait()

            if stage == "multipart":
                request = asyncio.create_task(client.post("/api/v1/sources/upload", content=body(), headers={"content-type": "multipart/form-data; boundary=part"}))
            else:
                request = asyncio.create_task(client.post("/api/v1/sources/upload", files={"file": ("cancel.hbk", b"synthetic")}))
            await asyncio.wait_for(entered.wait(), 2)
            busy = await client.post("/api/v1/sources/upload", files={"file": ("extra.hbk", b"synthetic")})
            assert busy.status_code == 409
            request.cancel()
            never.set()
            with pytest.raises(asyncio.CancelledError):
                await request
            assert spools and all(file.closed for file in spools)
            assert all(not path.exists() for path in directories)
            assert dashboard_backend._JOBS == []
            retry = await client.post("/api/v1/sources/upload", files={"file": ("invalid.txt", b"synthetic")})
            assert retry.status_code == 400

    asyncio.run(scenario())


@pytest.mark.parametrize("cancel_before_start", [False, True])
def test_отмена_фоновой_обёртки_не_освобождает_работающий_поток(cancel_before_start):
    from mcp1c.heavy_operations import HeavyOperations

    async def scenario():
        gate = HeavyOperations()
        lease = gate.acquire()
        token = gate.current.set(lease)
        entered, release = Event(), Event()

        def worker():
            entered.set()
            assert release.wait(5)

        background = gate.spawn(gate.run(worker))
        gate.current.reset(token)
        lease.release()
        try:
            if not cancel_before_start:
                await _wait(entered.is_set)
            background.cancel()
            await _wait(entered.is_set)
            assert gate.acquire() is None
            background.cancel()
            assert gate.acquire() is None
        finally:
            release.set()
            await asyncio.gather(background, return_exceptions=True)
            await _wait(lambda: not gate._pending)
        next_lease = gate.acquire()
        assert next_lease is not None
        next_lease.release()

    asyncio.run(scenario())


def test_отмена_intake_upload_ждёт_worker_до_закрытия_его_файла(tmp_path, monkeypatch):
    from types import SimpleNamespace

    monkeypatch.setenv("API_TOKEN", "synthetic-read")
    monkeypatch.setenv("ADMIN_TOKEN", "synthetic-admin")
    entered, release = Event(), Event()
    streams = []

    def accept(name, stream, **kwargs):
        streams.append(stream)
        entered.set()
        assert release.wait(5)
        assert not stream.closed
        return {"id": "synthetic-candidate"}

    service = SimpleNamespace(
        lifecycle=SimpleNamespace(browser=SimpleNamespace(max_upload_bytes=1024)),
        accept_upload=accept,
    )

    async def scenario():
        app = mcp_guard(Starlette(routes=routes(Registry(tmp_path / "data"), intake=service)))
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver", headers={"x-api-token": "synthetic-admin"}) as client:
            request = asyncio.create_task(client.post("/api/v1/sources/intake/upload", files={"file": ("candidate.zip", b"synthetic")}))
            try:
                await _wait(entered.is_set)
                request.cancel()
                await asyncio.sleep(0)
                assert not request.done()
                assert not streams[0].closed
                denied = await client.post("/api/v1/sources/upload", files={"file": ("other.hbk", b"synthetic")})
                assert denied.status_code == 409
            finally:
                release.set()
            with pytest.raises(asyncio.CancelledError):
                await request
            assert streams[0].closed
            retry = await client.post("/api/v1/sources/upload", files={"file": ("invalid.txt", b"synthetic")})
            assert retry.status_code == 400

    asyncio.run(scenario())


def test_отмена_во_время_spool_write_не_закрывает_файл_под_потоком(tmp_path, monkeypatch):
    from starlette import formparsers

    monkeypatch.setenv("API_TOKEN", "synthetic-read")
    monkeypatch.setenv("ADMIN_TOKEN", "synthetic-admin")
    entered, release = Event(), Event()
    spools = []
    real_spool = formparsers.SpooledTemporaryFile

    def spool(*args, **kwargs):
        file = real_spool(*args, **kwargs)
        write = file.write

        def blocked_write(data):
            entered.set()
            assert release.wait(5)
            assert not file.closed
            return write(data)

        file.write = blocked_write
        spools.append(file)
        return file

    monkeypatch.setattr(formparsers, "SpooledTemporaryFile", spool)

    async def scenario():
        app = mcp_guard(Starlette(routes=routes(Registry(tmp_path / "data"))))
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver", headers={"x-api-token": "synthetic-admin"}) as client:
            request = asyncio.create_task(client.post("/api/v1/sources/upload", files={"file": ("large.hbk", b"x" * (2 << 20))}))
            try:
                await _wait(entered.is_set)
                request.cancel()
                await asyncio.sleep(0.03)
                assert not request.done()
                assert spools and not spools[0].closed
            finally:
                release.set()
            with pytest.raises(asyncio.CancelledError):
                await request
            assert spools[0].closed
            retry = await client.post("/api/v1/sources/upload", files={"file": ("invalid.txt", b"synthetic")})
            assert retry.status_code == 400

    asyncio.run(scenario())


@pytest.mark.parametrize("first", ["intake", "incoming"])
def test_intake_и_incoming_делят_допуск_с_обычным_upload(tmp_path, monkeypatch, first):
    from types import SimpleNamespace

    monkeypatch.setenv("API_TOKEN", "synthetic-read")
    monkeypatch.setenv("ADMIN_TOKEN", "synthetic-admin")
    entered, release = Event(), Event()
    registry = Registry(tmp_path / "data")
    service = SimpleNamespace(
        start=lambda *a, **kw: SimpleNamespace(job_id="synthetic-job"),
        job_payload=lambda job_id: {"id": job_id},
    )

    def prepare(*args):
        entered.set()
        assert release.wait(5)

    service.prepare = prepare
    if first == "incoming":
        registry.incoming_dir.mkdir(parents=True)
        archive = registry.incoming_dir / "sample.zip"
        archive.write_bytes(b"synthetic")
        scanner = dashboard_backend._scanner(registry)
        monkeypatch.setattr(scanner, "дописывается", lambda path: False)
        monkeypatch.setattr("mcp1c.intake.planned_size", lambda path: 0)

        def incoming(reg, scanner, job, path, configuration):
            try:
                prepare()
                job["state"] = dashboard_backend.JOB_DONE
            finally:
                scanner.finish(path.name)

        monkeypatch.setattr(dashboard_backend, "_run_incoming", incoming)

    async def scenario():
        app = mcp_guard(Starlette(routes=routes(registry, intake=service)))
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver", headers={"x-api-token": "synthetic-admin"}) as client:
            try:
                path, payload = (
                    ("/api/v1/sources/intake/start", {"candidate_id": "sample", "action": "create"})
                    if first == "intake" else
                    ("/api/v1/sources/incoming/parse", {"name": "sample.zip"})
                )
                response = await client.post(path, json=payload)
                assert response.status_code == 202, response.text
                await _wait(entered.is_set)
                denied = await client.post("/api/v1/sources/upload", files={"file": ("second.hbk", b"synthetic")})
                assert denied.status_code == 409
                read = await asyncio.wait_for(client.get("/api/v1/sources"), 1)
                assert read.status_code == 200
            finally:
                release.set()
            for _ in range(400):
                retry = await client.post("/api/v1/sources/upload", files={"file": ("invalid.txt", b"synthetic")})
                if retry.status_code != 409:
                    break
                await asyncio.sleep(0.005)
            assert retry.status_code == 400
            if first == "incoming":
                assert archive.read_bytes() == b"synthetic"

    asyncio.run(scenario())
