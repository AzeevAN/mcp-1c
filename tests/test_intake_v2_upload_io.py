"""M13: однократная запись browser upload и проверка identity.

Синтетические ZIP, штатные HTTP-маршруты и старый публичный accept(stream).
Счётчики стоят на существующих границах I/O, а не на будущем builder API.
"""

import asyncio
import hashlib
import io
import os
from pathlib import Path
import zipfile

import pytest
from starlette import formparsers
from starlette.applications import Starlette
from starlette.requests import ClientDisconnect

from mcp1c import dashboard_backend, intake_v2_transport as transport
from mcp1c.dashboard_runtime import DASHBOARD_ON, routes
from mcp1c.registry import Registry
from test_intake_v2_api import _archive, _client, _service


def _считать_полные_sha(monkeypatch, raw, *, streamed=None, active_sink=None):
    """Считается хеширование всего ZIP, а не мелкие fingerprint/metadata."""
    оригинал = hashlib.sha256
    полные = []

    class Хеш:
        def __init__(self, data=b"", **kwargs):
            self._hash = оригинал(data, **kwargs)
            self._bytes = bytearray(data)
            self._reported = False

        def update(self, data):
            self._bytes.extend(data)
            if (
                streamed is not None and self._bytes == raw
                and active_sink is not None and active_sink()
            ):
                streamed.append(len(self._bytes))
            return self._hash.update(data)

        def hexdigest(self):
            if not self._reported and self._bytes == raw:
                полные.append(len(self._bytes))
                self._reported = True
            return self._hash.hexdigest()

        def digest(self):
            self.hexdigest()
            return self._hash.digest()

        def copy(self):
            result = object.__new__(Хеш)
            result._hash = self._hash.copy()
            result._bytes = self._bytes.copy()
            result._reported = False
            return result

        def __getattr__(self, name):
            return getattr(self._hash, name)

    monkeypatch.setattr(hashlib, "sha256", Хеш)
    return полные


def _считать_upload_write(monkeypatch, browser):
    """Раздельные sink: multipart spool и файл payload, без двойного счёта.

    Rollover spool вызывает запись на физический диск внутри самого spool;
    она уже учтена внешним spool.write и повторно не инструментируется.
    Дескрипторы metadata вне payloads_dir также не считаются телом ZIP.
    """
    spools, sinks = [], []
    fd_paths = {}
    real_spool = formparsers.SpooledTemporaryFile
    real_mkstemp = transport.tempfile.mkstemp
    real_fdopen = transport.os.fdopen

    def spool(*args, **kwargs):
        kwargs["max_size"] = 1
        result = real_spool(*args, **kwargs)
        запись = {"file": result, "bytes": 0}
        spools.append(запись)
        real_write = result.write

        def write(data):
            written = real_write(data)
            запись["bytes"] += written
            return written

        result.write = write
        return result

    def mkstemp(*args, **kwargs):
        fd, name = real_mkstemp(*args, **kwargs)
        if Path(name).parent == browser.payloads_dir:
            fd_paths[fd] = Path(name)
        return fd, name

    class Sink:
        def __init__(self, file, path):
            self.file, self.path, self.bytes = file, path, 0

        def write(self, data):
            written = self.file.write(data)
            self.bytes += written
            return written

        def __getattr__(self, name):
            return getattr(self.file, name)

        def __enter__(self):
            self.file.__enter__()
            return self

        def __exit__(self, *args):
            return self.file.__exit__(*args)

    def fdopen(fd, *args, **kwargs):
        file = real_fdopen(fd, *args, **kwargs)
        path = fd_paths.pop(fd, None)
        if path is None:
            return file
        sink = Sink(file, path)
        sinks.append(sink)
        return sink

    monkeypatch.setattr(formparsers, "SpooledTemporaryFile", spool)
    monkeypatch.setattr(transport.tempfile, "mkstemp", mkstemp)
    monkeypatch.setattr(transport.os, "fdopen", fdopen)
    return spools, sinks


def test_intake_http_пишет_zip_один_раз_и_хеширует_на_лету(tmp_path, monkeypatch):
    monkeypatch.setenv("ADMIN_TOKEN", "admin-token")
    registry = Registry(tmp_path / "data")
    service = _service(registry)
    raw = _archive()
    expected_sha = hashlib.sha256(raw).hexdigest()
    spools, sinks = _считать_upload_write(monkeypatch, service.lifecycle.browser)
    streamed = []
    полные_sha = _считать_полные_sha(
        monkeypatch, raw, streamed=streamed,
        active_sink=lambda: (
            any(not sink.file.closed for sink in sinks)
            or any(not item["file"].closed for item in spools)
        ),
    )
    client = _client(registry, service)

    response = client.post(
        "/api/v1/sources/intake/upload",
        headers={"x-api-token": "admin-token"},
        files={"file": ("configuration.zip", raw)},
    )

    assert response.status_code == 201
    candidate = response.json()["candidate"]
    assert candidate["transport"] == "browser"
    assert candidate["internal_name"] == "DemoConfiguration"
    assert candidate["actions"] == ["create"]
    record = service.lifecycle.browser.load(candidate["id"])
    assert record.size == len(raw) and record.sha256 == expected_sha
    assert registry.snapshot().configuration_names == ()
    assert service.lifecycle.operations.records.list_jobs() == ()
    assert not registry.incoming_dir.exists() or not any(registry.incoming_dir.iterdir())
    assert all(item["file"].closed for item in spools)
    assert all(sink.file.closed for sink in sinks)
    assert (
        sum(item["bytes"] for item in spools)
        + sum(sink.bytes for sink in sinks)
    ) == len(raw)
    assert полные_sha == [len(raw)]
    assert streamed == [len(raw)], "SHA должен обновляться до закрытия upload sink"


def test_browser_identity_hit_не_хеширует_а_рестарт_хеширует_один_раз(
    tmp_path, monkeypatch
):
    raw = _archive()
    expected_sha = hashlib.sha256(raw).hexdigest()
    root = tmp_path / "managed"
    browser = transport.BrowserStagingStore(root, max_upload_bytes=len(raw))
    # Это прежний публичный путь с обычным BinaryIO: он остаётся рабочим.
    record = browser.accept("candidate-cache", "demo.zip", io.BytesIO(raw), expected_size=len(raw))
    assert record.sha256 == expected_sha
    полные_sha = _считать_полные_sha(monkeypatch, raw)

    for _ in range(2):
        with browser.open_tree(record.candidate_id) as tree:
            assert tree.source_sha256() == expected_sha
    assert полные_sha == []

    restarted = transport.BrowserStagingStore(root, max_upload_bytes=len(raw))
    for _ in range(2):
        with restarted.open_tree(record.candidate_id) as tree:
            assert tree.source_sha256() == expected_sha
    assert полные_sha == [len(raw)]


@pytest.mark.parametrize("replace_inode", [False, True])
def test_browser_identity_cache_не_скрывает_подмену_с_тем_же_mtime(
    tmp_path, replace_inode
):
    # Равный размер гарантирован конструкцией ZIP-комментария, а не тем,
    # насколько удачно deflate сожмёт две разные версии XML.
    buffer = io.BytesIO(_archive())
    with zipfile.ZipFile(buffer, "a") as archive:
        archive.comment = b"first"
    first = buffer.getvalue()
    second = first[:-5] + b"other"
    assert len(first) == len(second) and first != second
    browser = transport.BrowserStagingStore(tmp_path / "managed", max_upload_bytes=len(first))
    record = browser.accept("candidate-tamper", "demo.zip", io.BytesIO(first), expected_size=len(first))
    with browser.open_tree(record.candidate_id):
        pass
    payload = browser.payloads_dir / f"{record.candidate_id}.upload"
    before = payload.stat()
    if replace_inode:
        replacement = tmp_path / "replacement.zip"
        replacement.write_bytes(second)
        os.utime(replacement, ns=(before.st_atime_ns, before.st_mtime_ns))
        os.replace(replacement, payload)
    else:
        payload.write_bytes(second)
        os.utime(payload, ns=(before.st_atime_ns, before.st_mtime_ns))
    after = payload.stat()
    assert (after.st_size, after.st_mtime_ns) == (before.st_size, before.st_mtime_ns)
    assert (after.st_ino, after.st_ctime_ns) != (before.st_ino, before.st_ctime_ns)
    with pytest.raises(transport.TransportError, match="SHA|подмен|поврежд"):
        browser.open_tree(record.candidate_id)


def test_intake_http_disconnect_удаляет_незавершённую_загрузку(tmp_path, monkeypatch):
    monkeypatch.setenv("ADMIN_TOKEN", "admin-token")
    monkeypatch.setattr(dashboard_backend, "JOBS", [])
    monkeypatch.setattr(dashboard_backend, "ФОНОВЫЕ", set())
    registry = Registry(tmp_path / "data")
    service = _service(registry)
    browser = service.lifecycle.browser
    spools, sinks = _считать_upload_write(monkeypatch, browser)
    app = Starlette(routes=routes(registry, mode=DASHBOARD_ON, intake=service))
    prefix = (
        b'--part\r\nContent-Disposition: form-data; name="file"; '
        b'filename="configuration.zip"\r\nContent-Type: application/zip\r\n\r\n'
        + _archive()[:64]
    )
    messages = iter((
        {"type": "http.request", "body": prefix, "more_body": True},
        {"type": "http.disconnect"},
    ))
    scope = {
        "type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1",
        "method": "POST", "scheme": "http", "path": "/api/v1/sources/intake/upload",
        "raw_path": b"/api/v1/sources/intake/upload", "query_string": b"", "root_path": "",
        "headers": [(b"content-type", b"multipart/form-data; boundary=part"),
                    (b"x-api-token", b"admin-token"), (b"host", b"testserver")],
        "client": ("test", 1), "server": ("testserver", 80),
    }

    async def scenario():
        async def receive():
            return next(messages)

        async def send(message):
            pass

        with pytest.raises(ClientDisconnect):
            await asyncio.wait_for(app(scope, receive, send), timeout=3)

    asyncio.run(scenario())
    assert spools or sinks, "Обрыв должен произойти после создания upload sink"
    assert all(item["file"].closed for item in spools)
    assert all(sink.file.closed for sink in sinks)
    assert browser.candidate_ids() == ()
    assert list(browser.payloads_dir.iterdir()) == []
    assert list(browser.records_dir.iterdir()) == []
    assert service.lifecycle.operations.records.list_jobs() == ()
    assert dashboard_backend.JOBS == [] and dashboard_backend.ФОНОВЫЕ == set()
    assert registry.snapshot().configuration_names == ()


# R3 (ревью реализации аудита): ZIP из дашборда уже лежит в `.part` на томе
# staging, поэтому accept не резервирует его размер второй раз; резерв
# свободного места и коды ответа при нехватке места закреплены отдельно.

def _usage(free):
    return lambda _path: type("usage", (), {"total": 1 << 40, "used": 0, "free": free})()


def _own_stream(store, payload):
    writer = store._new_upload_writer()
    writer.write(payload)
    return writer


def test_r3_own_stream_не_резервирует_размер_архива_второй_раз(tmp_path, monkeypatch):
    payload = b"z" * 1000
    store = transport.BrowserStagingStore(
        tmp_path / "managed", max_upload_bytes=10_000, free_space_reserve=100,
    )
    writer = _own_stream(store, payload)
    # После записи `.part` свободно меньше размера архива, но больше резерва.
    monkeypatch.setattr(transport.shutil, "disk_usage", _usage(500 + 100))

    record = store.accept("candidate-r3", "demo.zip", writer, expected_size=len(payload))

    assert record.size == len(payload)
    assert record.sha256 == hashlib.sha256(payload).hexdigest()


def test_r3_own_stream_соблюдает_резерв_свободного_места(tmp_path, monkeypatch):
    payload = b"z" * 1000
    store = transport.BrowserStagingStore(
        tmp_path / "managed", max_upload_bytes=10_000, free_space_reserve=100,
    )
    writer = _own_stream(store, payload)
    monkeypatch.setattr(transport.shutil, "disk_usage", _usage(99))

    with pytest.raises(transport.TransportStorageFullError, match="мест"):
        store.accept("candidate-r3", "demo.zip", writer, expected_size=len(payload))

    assert not any(store.payloads_dir.iterdir())
    assert not any(store.records_dir.iterdir())


def test_r3_own_stream_проверяет_допустимость_expected_size(tmp_path, monkeypatch):
    store = transport.BrowserStagingStore(
        tmp_path / "managed", max_upload_bytes=10, free_space_reserve=0,
    )
    writer = _own_stream(store, b"1234")
    monkeypatch.setattr(transport.shutil, "disk_usage", _usage(1 << 30))

    with pytest.raises(transport.TransportLimitError, match="предел"):
        store.accept("candidate-r3", "demo.zip", writer, expected_size=11)
    assert not any(store.payloads_dir.iterdir())


def test_r3_внешний_поток_по_прежнему_резервирует_размер(tmp_path, monkeypatch):
    store = transport.BrowserStagingStore(
        tmp_path / "managed", max_upload_bytes=10_000, free_space_reserve=100,
    )
    monkeypatch.setattr(transport.shutil, "disk_usage", _usage(1000 + 99))

    with pytest.raises(transport.TransportStorageFullError, match="мест"):
        store.accept("candidate-r3", "demo.zip", io.BytesIO(b"z" * 1000), expected_size=1000)


@pytest.mark.parametrize("stage", ["seal", "link", "record"])
def test_r3_enospc_при_приёме_убирает_файлы(tmp_path, monkeypatch, stage):
    import errno

    payload = b"z" * 1000
    store = transport.BrowserStagingStore(
        tmp_path / "managed", max_upload_bytes=10_000, free_space_reserve=0,
    )
    writer = _own_stream(store, payload)
    monkeypatch.setattr(transport.shutil, "disk_usage", _usage(1 << 30))

    def no_space(*_args, **_kwargs):
        raise OSError(errno.ENOSPC, "No space left on device")

    if stage == "seal":
        # Отложенная запись на части ФС сообщает о нехватке места только
        # на fsync при сбросе на диск.
        monkeypatch.setattr(transport.os, "fsync", no_space)
    elif stage == "link":
        monkeypatch.setattr(transport.os, "link", no_space)
    else:
        monkeypatch.setattr(store, "_write_record", no_space)

    with pytest.raises(transport.TransportStorageFullError, match="мест"):
        store.accept("candidate-r3", "demo.zip", writer, expected_size=len(payload))

    assert not any(store.payloads_dir.iterdir()), "`.part` и payload удалены"
    assert not any(store.records_dir.iterdir())


def test_r3_http_нехватка_места_при_приёме_даёт_507(tmp_path, monkeypatch):
    monkeypatch.setenv("ADMIN_TOKEN", "admin-token")
    registry = Registry(tmp_path / "data")
    service = _service(registry)
    raw = _archive()
    client = _client(registry, service)
    browser = service.lifecycle.browser
    monkeypatch.setattr(transport.shutil, "disk_usage", _usage(browser.free_space_reserve - 1 if browser.free_space_reserve else 0))
    if not browser.free_space_reserve:
        monkeypatch.setattr(browser, "free_space_reserve", 1)

    response = client.post(
        "/api/v1/sources/intake/upload",
        headers={"x-api-token": "admin-token"},
        files={"file": ("configuration.zip", raw)},
    )

    assert response.status_code == 507
    assert "мест" in response.json()["error"]
