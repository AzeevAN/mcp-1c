#!/usr/bin/env python3
"""Измерить один browser intake upload без prepare и confirm.

Запускать из tools/lab через .venv/bin/python. Один и тот же файл используется
в baseline и candidate деревьях src. Локальный ASGI без сети, измерения RSS и
холодного диска. Исходный ZIP читается на месте через file handle; контрольный
SHA256 прогревает файловый кэш. Payload не собирается в памяти измерителя.
"""
from __future__ import annotations

import argparse
import asyncio
from contextlib import ExitStack, redirect_stderr, redirect_stdout
from datetime import datetime, timezone
import hashlib
import json
import math
import multiprocessing
import os
from pathlib import Path
import secrets
import signal
import stat
import subprocess
import sys
import tempfile
import threading
import time
from unittest.mock import patch


_ROOT = Path(__file__).resolve().parents[2]
_CHUNK = 1024 * 1024


class _Invalid(ValueError):
    """Входные или приёмочные условия измерения не выполнены."""


class _Parser(argparse.ArgumentParser):
    def error(self, message):
        raise _Invalid("Некорректные аргументы; используйте --help.")

    def print_help(self, file=None):
        print(json.dumps({"status": "help", "help": self.format_help()}, ensure_ascii=False))


def _positive(value):
    try:
        result = float(value)
    except ValueError:
        raise argparse.ArgumentTypeError("Ожидается положительное число.") from None
    if not math.isfinite(result) or result <= 0:
        raise argparse.ArgumentTypeError("Таймаут должен быть конечным и положительным.")
    return result


def _parser():
    parser = _Parser(prog="measure_intake_upload.py", description=__doc__, add_help=False)
    parser.add_argument("-h", "--help", action="help", help="Справка в JSON.")
    parser.add_argument("--archive", required=True, type=Path, help="ZIP базы; только чтение.")
    parser.add_argument("--timeout-seconds", type=_positive, default=900.0,
                        help="Общий бюджет, включая контрольные SHA256; по умолчанию 900 секунд.")
    parser.add_argument("--report", type=Path, help="Создать новый JSON-файл без перезаписи.")
    return parser


def _remaining(deadline):
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise TimeoutError
    return remaining


def _identity(info):
    # atime меняется при чтении и не доказывает изменение содержимого.
    return {name: getattr(info, "st_" + name) for name in
            ("dev", "ino", "mode", "size", "mtime_ns", "ctime_ns")}


def _open_source(path):
    before = path.lstat()
    if not stat.S_ISREG(before.st_mode):
        raise _Invalid("Исходник должен быть обычным файлом без символической ссылки.")
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        opened = os.fstat(descriptor)
        if not stat.S_ISREG(opened.st_mode) or _identity(before) != _identity(opened):
            raise _Invalid("Исходник изменился во время открытия.")
        os.set_blocking(descriptor, True)
        stream = os.fdopen(descriptor, "rb")
    except BaseException:
        os.close(descriptor)
        raise
    return stream, _identity(opened)


def _hash(stream, deadline):
    stream.seek(0)
    digest = hashlib.sha256()
    size = 0
    while True:
        _remaining(deadline)
        block = stream.read(_CHUNK)
        if not block:
            return digest.hexdigest(), size
        digest.update(block)
        size += len(block)


def _checked_hash(path, deadline):
    with _open_source(path)[0] as stream:
        before = _identity(os.fstat(stream.fileno()))
        digest, _ = _hash(stream, deadline)
        if before != _identity(os.fstat(stream.fileno())) or before != _identity(path.lstat()):
            raise _Invalid("Код изменился во время контрольного чтения.")
        return digest


def _code_identity(deadline, *, with_git=False):
    package = _ROOT / "src" / "mcp1c"
    if not package.is_dir() or package.is_symlink() or package.parent.is_symlink():
        raise _Invalid("Не найден исходный пакет для контрольных сумм.")
    hashes = {}
    for path in sorted(package.rglob("*")):
        _remaining(deadline)
        if path.is_symlink():
            raise _Invalid("Symlink в исходном пакете не допускается.")
        if path.suffix == ".py" and path.is_file():
            if not path.resolve().is_relative_to(package.resolve()):
                raise _Invalid("Исходный файл вышел за пределы пакета.")
            hashes[path.relative_to(package).as_posix()] = _checked_hash(path, deadline)
    if not hashes:
        raise _Invalid("Не найден исходный пакет для контрольных сумм.")
    encoded = json.dumps(hashes, sort_keys=True, separators=(",", ":")).encode("utf-8")
    result = {"package_sha256": hashlib.sha256(encoded).hexdigest(), "module_sha256": hashes,
              "script_sha256": _checked_hash(Path(__file__), deadline)}
    if with_git:
        result["git_head"] = None
        try:
            completed = subprocess.run(
                ["git", "rev-parse", "--verify", "HEAD"], cwd=_ROOT,
                capture_output=True, timeout=min(5, _remaining(deadline)), check=False,
            )
            head = completed.stdout.decode("ascii").strip()
            if completed.returncode == 0 and len(head) in (40, 64) and all(
                char in "0123456789abcdef" for char in head
            ):
                result["git_head"] = head
        except (OSError, UnicodeError, subprocess.TimeoutExpired):
            pass
    return result


def _emit(connection, **fields):
    connection.send(fields)


def _progress(stage, started):
    print(json.dumps({"stage": stage, "elapsed_seconds": round(time.monotonic() - started, 1)},
                     ensure_ascii=False), file=sys.stderr, flush=True)


class _FileProxy:
    def __init__(self, stream):
        self.stream = stream

    def __getattr__(self, name):
        return getattr(self.stream, name)

    def __enter__(self):
        self.stream.__enter__()
        return self

    def __exit__(self, *args):
        return self.stream.__exit__(*args)


class _ReadProxy(_FileProxy):
    def __init__(self, stream, meters, prefix):
        super().__init__(stream)
        self.meters = meters
        self.prefix = prefix

    def read(self, *args, **kwargs):
        _remaining(self.meters.deadline)
        value = self.stream.read(*args, **kwargs)
        self.meters.add(self.prefix + "_read_calls", 1)
        self.meters.add(self.prefix + "_read_bytes", len(value))
        return value

    def readinto(self, buffer):
        _remaining(self.meters.deadline)
        count = self.stream.readinto(buffer)
        self.meters.add(self.prefix + "_read_calls", 1)
        self.meters.add(self.prefix + "_read_bytes", count or 0)
        return count


class _WriteProxy(_FileProxy):
    def __init__(self, stream, meters):
        super().__init__(stream)
        self.meters = meters

    def write(self, value):
        _remaining(self.meters.deadline)
        written = self.stream.write(value)
        self.meters.add("staging_payload_write_calls", 1)
        self.meters.add("staging_payload_write_bytes", written)
        return written

    def writelines(self, lines):
        for line in lines:
            self.write(line)


class _SpoolProxy(_ReadProxy):
    def __init__(self, stream, meters, record):
        super().__init__(stream, meters, "multipart_spool")
        self.record = record

    def observe(self):
        self.record["rolled"] = self.record["rolled"] or bool(getattr(self.stream, "_rolled", False))
        self.record["closed"] = self.stream.closed

    def write(self, value):
        _remaining(self.meters.deadline)
        written = self.stream.write(value)
        self.meters.add("multipart_spool_write_calls", 1)
        self.meters.add("multipart_spool_write_bytes", written)
        self.record["written_bytes"] += written
        self.observe()
        return written

    def close(self):
        self.observe()
        try:
            return self.stream.close()
        finally:
            self.observe()

    def __exit__(self, *args):
        try:
            return self.stream.__exit__(*args)
        finally:
            self.observe()


class _DigestProxy:
    """Только состояние digest и число байт, без накопления исходных блоков."""

    def __init__(self, digest, meters, initial_bytes=0):
        self.digest_object = digest
        self.meters = meters
        self.total_bytes = initial_bytes
        self.own_input_bytes = initial_bytes
        self.reported = False

    def __getattr__(self, name):
        return getattr(self.digest_object, name)

    def update(self, value):
        _remaining(self.meters.deadline)
        result = self.digest_object.update(value)
        self.total_bytes += len(value)
        self.own_input_bytes += len(value)
        self.meters.add("sha256_input_bytes", len(value))
        return result

    def report(self, hex_digest):
        if (not self.reported and self.total_bytes == self.meters.source_bytes
                and hex_digest == self.meters.source_sha256):
            self.reported = True
            # copy() не является вторым чтением общего префикса хеша.
            if self.own_input_bytes == self.total_bytes:
                self.meters.add("full_archive_sha256_calls", 1)
                self.meters.add("full_archive_sha256_input_bytes", self.own_input_bytes)
            else:
                self.meters.add("full_archive_sha256_copied_state_finalizations", 1)

    def hexdigest(self):
        result = self.digest_object.hexdigest()
        self.report(result)
        return result

    def digest(self):
        result = self.digest_object.digest()
        self.report(result.hex())
        return result

    def copy(self):
        result = _DigestProxy(self.digest_object.copy(), self.meters)
        result.total_bytes = self.total_bytes
        return result


class _UploadMeters:
    def __init__(self, payloads_dir, source_bytes, source_sha256, deadline):
        self.payloads_dir = payloads_dir.resolve()
        self.source_bytes = source_bytes
        self.source_sha256 = source_sha256
        self.deadline = deadline
        self.lock = threading.Lock()
        self.active = False
        self.payload_descriptors = set()
        self.counts = {}
        self.spools = []

    def begin(self):
        self.counts = dict.fromkeys((
            "upload_source_read_calls", "upload_source_read_bytes",
            "multipart_spool_write_calls", "multipart_spool_write_bytes",
            "multipart_spool_read_calls", "multipart_spool_read_bytes",
            "staging_payload_files_created", "staging_payload_write_calls", "staging_payload_write_bytes",
            "staged_payload_read_calls", "staged_payload_read_bytes",
            "sha256_objects", "sha256_input_bytes", "full_archive_sha256_calls",
            "full_archive_sha256_input_bytes", "full_archive_sha256_copied_state_finalizations",
            "zip_source_sha256_calls", "zip_source_sha256_cached_calls",
            "zip_source_sha256_uncached_calls", "zip_source_sha256_unknown_cache_calls",
        ), 0)
        self.spools = []
        self.active = True

    def add(self, name, value):
        if self.active:
            with self.lock:
                self.counts[name] += value

    def finish(self):
        self.active = False
        result = dict(self.counts)
        result["multipart_spools"] = [dict(record) for record in self.spools]
        result["payload_write_bytes"] = (
            result["multipart_spool_write_bytes"] + result["staging_payload_write_bytes"]
        )
        result["payload_write_factor"] = result["payload_write_bytes"] / self.source_bytes
        return result

    def install(self, stack, formparsers, transport):
        original_spool = formparsers.SpooledTemporaryFile
        original_mkstemp = tempfile.mkstemp
        original_fdopen = os.fdopen
        original_sha256 = hashlib.sha256
        original_open = transport._open_regular_file
        original_source_sha256 = transport.ZipExportTree.source_sha256

        def spool(*args, **kwargs):
            stream = original_spool(*args, **kwargs)
            if not self.active:
                return stream
            record = {"max_size": getattr(stream, "_max_size", None),
                      "written_bytes": 0, "rolled": False, "closed": False}
            self.spools.append(record)
            return _SpoolProxy(stream, self, record)

        def mkstemp(*args, **kwargs):
            descriptor, name = original_mkstemp(*args, **kwargs)
            if self.active and Path(name).parent.resolve() == self.payloads_dir:
                with self.lock:
                    self.payload_descriptors.add(descriptor)
                    self.counts["staging_payload_files_created"] += 1
            return descriptor, name

        def fdopen(descriptor, *args, **kwargs):
            stream = original_fdopen(descriptor, *args, **kwargs)
            with self.lock:
                is_payload = descriptor in self.payload_descriptors
                self.payload_descriptors.discard(descriptor)
            return _WriteProxy(stream, self) if is_payload else stream

        def sha256(data=b"", *args, **kwargs):
            digest = original_sha256(data, *args, **kwargs)
            if not self.active:
                return digest
            self.add("sha256_objects", 1)
            self.add("sha256_input_bytes", len(data))
            return _DigestProxy(digest, self, len(data))

        def open_regular(path, *args, **kwargs):
            stream, identity = original_open(path, *args, **kwargs)
            if self.active and Path(path).parent.resolve() == self.payloads_dir:
                return _ReadProxy(stream, self, "staged_payload"), identity
            return stream, identity

        def source_sha256(tree, *args, **kwargs):
            cached = getattr(tree, "_source_sha256", "unknown")
            self.add("zip_source_sha256_calls", 1)
            self.add("zip_source_sha256_unknown_cache_calls" if cached == "unknown" else
                     "zip_source_sha256_uncached_calls" if cached is None else
                     "zip_source_sha256_cached_calls", 1)
            return original_source_sha256(tree, *args, **kwargs)

        # Две непересекающиеся write-границы. Inner spool/file.write не патчим.
        stack.enter_context(patch.object(formparsers, "SpooledTemporaryFile", spool))
        stack.enter_context(patch.object(tempfile, "mkstemp", mkstemp))
        stack.enter_context(patch.object(os, "fdopen", fdopen))
        stack.enter_context(patch.object(hashlib, "sha256", sha256))
        # Если обработчик импортировал sha256 напрямую, это тот же счётчик.
        for name, module in tuple(sys.modules.items()):
            if module is None or not name.startswith("mcp1c."):
                continue
            for attribute, value in tuple(vars(module).items()):
                if value is original_sha256:
                    stack.enter_context(patch.object(module, attribute, sha256))
        stack.enter_context(patch.object(transport, "_open_regular_file", open_regular))
        stack.enter_context(patch.object(transport.ZipExportTree, "source_sha256", source_sha256))


async def _post(app, httpx, stream, token):
    # ASGITransport передаёт multipart chunks; TestClient.request.read() собрал бы весь ZIP.
    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
    async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1", timeout=None) as client:
        return await client.post(
            "/api/v1/sources/intake/upload", headers={"x-admin-token": token},
            files={"file": ("source.zip", stream, "application/zip")},
        )


def _semantic_hash(value, excluded):
    stable = {key: item for key, item in value.items() if key not in excluded}
    encoded = json.dumps(stable, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _measure(connection, source, root, deadline):
    from starlette import formparsers
    from starlette.applications import Starlette
    from starlette.testclient import httpx

    from mcp1c import intake_v2_transport as transport
    from mcp1c.dashboard_runtime import DASHBOARD_ON, routes
    from mcp1c.intake_v2 import CandidateTransport, DurableCandidateStore, SourceKind
    from mcp1c.intake_v2_api import IntakeApiService
    from mcp1c.intake_v2_lifecycle import CandidateCatalog, IntakeLifecycle
    from mcp1c.intake_v2_operations import IntakeCoordinator
    from mcp1c.reference_provider import ReferenceService
    from mcp1c.registry import Registry

    _emit(connection, stage="source_before")
    stream, before = _open_source(source)
    with stream:
        try:
            started = time.monotonic()
            source_sha, source_bytes = _hash(stream, deadline)
            _emit(connection, source_stat_before=before, source_sha256_before=source_sha,
                  source_bytes=source_bytes, hash_before_seconds=time.monotonic() - started)
            if source_bytes <= 0 or before != _identity(os.fstat(stream.fileno())) or before != _identity(source.lstat()):
                raise _Invalid("Исходник изменился во время контрольного чтения.")
            data = root / "data"
            incoming = data / "incoming"
            incoming.mkdir(parents=True)
            config_sources = root / "config-sources"
            config_sources.mkdir()
            registry = Registry(data)
            _emit(connection, stage="registry_startup")
            registry.startup()
            if registry.role != "owner":
                raise _Invalid("Изолированный Registry не стал владельцем.")
            storage = root / "intake"
            browser = transport.BrowserStagingStore(storage / "uploads")
            lifecycle = IntakeLifecycle(
                CandidateCatalog(storage / "catalog"), browser,
                IntakeCoordinator(storage / "operations", DurableCandidateStore(storage / "records")),
                incoming_root=incoming, config_sources_root=config_sources,
                local_sources={}, directory_settle_seconds=0,
            )
            service = IntakeApiService(registry, lifecycle)
            reference = ReferenceService.discover(data, embedded_path=root / "missing-reference.mcp1cref")
            app = Starlette(routes=routes(registry, mode=DASHBOARD_ON, intake=service, reference=reference))
            limits = lifecycle.intake_limits
            default_spool_limit = getattr(formparsers.MultiPartParser, "spool_max_size", None)
            if default_spool_limit is None:
                default_spool_limit = getattr(formparsers.MultiPartParser, "max_file_size", None)
            _emit(connection, controls={
                "browser_max_upload_bytes": browser.max_upload_bytes,
                "browser_free_space_reserve": browser.free_space_reserve,
                "multipart_default_spool_max_size": default_spool_limit,
                "intake_archive_limits": {key: getattr(limits, key) for key in (
                    "max_entries", "max_entry_bytes", "max_total_bytes", "max_compression_ratio", "ratio_min_bytes")},
                "client_module": httpx.__name__, "client_version": getattr(httpx, "__version__", None),
            })
            if source_bytes > browser.max_upload_bytes:
                raise _Invalid("Архив превышает штатный предел browser upload.")
            meters = _UploadMeters(browser.payloads_dir, source_bytes, source_sha, deadline)
            with ExitStack() as stack:
                meters.install(stack, formparsers, transport)
                _remaining(deadline)
                _emit(connection, stage="post_upload")
                stream.seek(0)
                meters.begin()
                started = time.monotonic()
                try:
                    response = asyncio.run(_post(app, httpx, _ReadProxy(stream, meters, "upload_source"),
                                                 os.environ["ADMIN_TOKEN"]))
                finally:
                    elapsed = time.monotonic() - started
                    counts = meters.finish()
                    _emit(connection, measurement={"stage": "post_upload", "seconds": elapsed, **counts})
                _emit(connection, http_status=response.status_code)
                if response.status_code != 201:
                    raise _Invalid("HTTP upload не принял кандидата.")
                payload = response.json()
                candidate_payload = payload.get("candidate") if isinstance(payload, dict) else None
                if not isinstance(candidate_payload, dict) or not isinstance(candidate_payload.get("id"), str):
                    raise _Invalid("HTTP upload не вернул кандидата.")
                candidate_id = candidate_payload["id"]
                candidate = lifecycle.catalog.load(candidate_id)
                record = browser.load(candidate_id)
                probe = candidate.probe
                accepted = (
                    candidate_payload == service.candidate_payload(candidate)
                    and candidate.locator.transport is CandidateTransport.BROWSER
                    and probe.transport is CandidateTransport.BROWSER
                    and probe.source_kind is SourceKind.CONFIGURATION
                    and probe.raw_sha256 == record.sha256 == source_sha
                    and record.size == source_bytes
                    and len(browser.list_uploads()) == 1
                    and browser._payload_path(candidate_id).stat().st_size == source_bytes
                )
                spools = counts["multipart_spools"]
                rollover_known = all(isinstance(record["max_size"], int) and record["max_size"] > 0
                                     for record in spools if record["written_bytes"])
                large_spools = [record for record in spools if isinstance(record["max_size"], int)
                                and 0 < record["max_size"] < record["written_bytes"]]
                rollover_observed = all(record["rolled"] for record in large_spools) if large_spools else None
                rollover_check = ("unknown" if not rollover_known else "failed" if rollover_observed is False
                                  else "passed" if rollover_observed is True else "not_applicable")
                write_boundaries_valid = (
                    counts["upload_source_read_bytes"] == source_bytes
                    and counts["staging_payload_write_bytes"] == source_bytes
                    and counts["staging_payload_files_created"] == 1
                    and counts["multipart_spool_write_bytes"] in (0, source_bytes)
                    and all(record["closed"] for record in spools)
                    and rollover_known and rollover_observed is not False
                )
                hash_boundary_valid = (
                    counts["full_archive_sha256_calls"] >= 1
                    and counts["full_archive_sha256_input_bytes"] == source_bytes * counts["full_archive_sha256_calls"]
                    and counts["zip_source_sha256_unknown_cache_calls"] == 0
                )
                _emit(connection, accepted={
                    "valid": accepted, "write_boundaries_valid": write_boundaries_valid,
                    "hash_boundary_valid": hash_boundary_valid,
                    # ID случаен, snapshot fingerprint содержит физическую identity ZIP.
                    "candidate_semantic_sha256": _semantic_hash(candidate_payload, ("id",)),
                    "candidate_semantic_excluded_fields": ["id"],
                    "probe_semantic_sha256": _semantic_hash(probe.to_dict(), ("snapshot_fingerprint",)),
                    "probe_semantic_excluded_fields": ["snapshot_fingerprint"],
                    "probe_keys": sorted(probe.to_dict()),
                    "candidate_keys": sorted(candidate_payload), "response_keys": sorted(payload),
                    "source_kind": probe.source_kind.value, "transport": probe.transport.value,
                    "layout": probe.layout.value, "raw_sha256": probe.raw_sha256, "raw_size": record.size,
                    "multipart_large_spools_rolled": rollover_observed,
                    "multipart_rollover_check": rollover_check,
                })
                if not accepted or not write_boundaries_valid or not hash_boundary_valid:
                    raise _Invalid("Кандидат или границы I/O не прошли проверку.")
                for number in (1, 2):
                    _remaining(deadline)
                    _emit(connection, stage="open_tree", open_number=number)
                    meters.begin()
                    started = time.monotonic()
                    try:
                        with browser.open_tree(candidate_id, limits=lifecycle.intake_limits) as tree:
                            digest = tree.source_sha256()
                            entries = len(tree.paths())
                    finally:
                        elapsed = time.monotonic() - started
                        counts = meters.finish()
                        _emit(connection, measurement={"stage": "open_tree", "number": number,
                                                       "seconds": elapsed, **counts})
                    _emit(connection, open_tree_result={"number": number, "sha256": digest,
                                                        "entry_count": entries, "matches_source": digest == source_sha})
                    if (digest != source_sha or entries <= 0 or counts["payload_write_bytes"] != 0
                            or counts["zip_source_sha256_calls"] < 1
                            or counts["zip_source_sha256_unknown_cache_calls"] != 0):
                        raise _Invalid("Повторное открытие изменило или не подтвердило payload.")
            snapshot = registry.snapshot()
            final = {"job_count": len(service.jobs_snapshot()["jobs"]),
                     "active_generation_count": len(snapshot.generations),
                     "configuration_count": len(snapshot.configurations),
                     "modules_count": len(snapshot.modules),
                     "incoming_file_count": len(tuple(incoming.iterdir())),
                     "config_sources_file_count": len(tuple(config_sources.iterdir())),
                     "browser_payload_count": len(tuple(browser.payloads_dir.iterdir())),
                     "browser_record_count": len(tuple(browser.records_dir.iterdir())),
                     "catalog_record_count": len(tuple(lifecycle.catalog.records_dir.glob("*.json")))}
            no_publication = all(final[key] == 0 for key in (
                "job_count", "active_generation_count", "configuration_count", "modules_count",
                "incoming_file_count", "config_sources_file_count")) and all(
                    final[key] == 1 for key in ("browser_payload_count", "browser_record_count", "catalog_record_count"))
            _emit(connection, final_state=final, upload_only=no_publication)
            if not no_publication:
                raise _Invalid("Upload создал операцию, публикацию или лишнюю копию источника.")
        finally:
            _emit(connection, stage="source_after")
            started = time.monotonic()
            try:
                after_sha, after_bytes = _hash(stream, deadline)
                after = _identity(os.fstat(stream.fileno()))
                _emit(connection, source_stat_after=after, source_sha256_after=after_sha,
                      hash_after_seconds=time.monotonic() - started,
                      source_unchanged=(before == after == _identity(source.lstat())
                                        and source_sha == after_sha and after_bytes == before["size"]))
            except Exception as error:
                _emit(connection, source_check_error_type=type(error).__name__, source_unchanged=None)


def _child(connection, source, root, deadline):
    os.setsid()
    os.chdir(root)
    internal_temp = root / "tmp"
    internal_temp.mkdir()
    os.environ.update(API_TOKEN=secrets.token_urlsafe(32), ADMIN_TOKEN=secrets.token_urlsafe(32),
                      TMPDIR=str(internal_temp), TEMP=str(internal_temp), TMP=str(internal_temp))
    tempfile.tempdir = None
    sys.path.insert(0, str(_ROOT / "src"))
    with open(os.devnull, "w") as sink, redirect_stdout(sink), redirect_stderr(sink):
        try:
            _emit(connection, actor_code_before=_code_identity(deadline), stage="imports")
            _measure(connection, source, root, deadline)
            _emit(connection, actor_code_after=_code_identity(deadline), finished=True)
        except BaseException as error:
            _emit(connection, error_type=type(error).__name__)
        finally:
            connection.close()


def _stop(process):
    if process.pid is None:
        return
    # is_alive перед killpg защищает от уже переиспользованного PID.
    try:
        if process.is_alive() and os.getpgid(process.pid) == process.pid:
            os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        pass
    if process.is_alive():
        process.terminate()
        process.join(timeout=1)
    if process.is_alive():
        try:
            if os.getpgid(process.pid) == process.pid:
                os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        process.kill()
    process.join()
    process.close()


def _run(args, report):
    started = time.monotonic()
    deadline = started + args.timeout_seconds
    try:
        report["stage"] = "code_identity"
        _progress(report["stage"], started)
        report["code"] = _code_identity(deadline, with_git=True)
        expected_code = {key: value for key, value in report["code"].items() if key != "git_head"}
        source = args.archive.absolute()
        context = multiprocessing.get_context("spawn")
        with tempfile.TemporaryDirectory(prefix="mcp1c-intake-upload-") as temporary:
            receiver, sender = context.Pipe(duplex=False)
            process = context.Process(target=_child, args=(sender, source, Path(temporary), deadline))
            finished = False
            next_progress = time.monotonic() + 30
            try:
                process.start()
                sender.close()
                while True:
                    if receiver.poll(min(0.1, _remaining(deadline))):
                        try:
                            event = receiver.recv()
                        except EOFError:
                            break
                        if "measurement" in event:
                            report["measurements"].append(event.pop("measurement"))
                        if "open_tree_result" in event:
                            report["open_tree_results"].append(event.pop("open_tree_result"))
                        finished = event.pop("finished", False) or finished
                        report.update(event)
                        for key in ("actor_code_before", "actor_code_after"):
                            if key in event and event[key] != expected_code:
                                raise _Invalid("Код изменился между процессами.")
                        if "stage" in event:
                            _progress(report["stage"], started)
                            next_progress = time.monotonic() + 30
                    elif not process.is_alive():
                        break
                    if time.monotonic() >= next_progress:
                        _progress(report["stage"], started)
                        next_progress = time.monotonic() + 30
                process.join(timeout=_remaining(deadline))
                if process.is_alive():
                    raise TimeoutError
                report["code_after"] = _code_identity(deadline)
                report["code_unchanged"] = all(report.get(key) == expected_code for key in (
                    "actor_code_before", "actor_code_after", "code_after"))
                rows = report["measurements"]
                opens = report["open_tree_results"]
                report["protocol_complete"] = (
                    len(rows) == 3 and [row["stage"] for row in rows] == ["post_upload", "open_tree", "open_tree"]
                    and [row.get("number") for row in rows[1:]] == [1, 2]
                    and len(opens) == 2 and all(row["matches_source"] for row in opens)
                )
                if (finished and process.exitcode == 0 and report.get("http_status") == 201
                        and report.get("accepted", {}).get("valid") is True
                        and report.get("accepted", {}).get("write_boundaries_valid") is True
                        and report.get("accepted", {}).get("hash_boundary_valid") is True
                        and report.get("upload_only") is True and report.get("source_unchanged") is True
                        and report["code_unchanged"] and report["protocol_complete"]):
                    report.update(status="complete", stage="finished")
            finally:
                _stop(process)
                receiver.close()
                sender.close()
                report["child_process_stopped"] = True
    finally:
        report["total_seconds"] = time.monotonic() - started
        report["temporary_directory_removed"] = "temporary" in locals() and not Path(temporary).exists()


def main(argv=None):
    report = {"schema_version": 1, "status": "incomplete", "measurements": [], "open_tree_results": [],
              "measured_at": datetime.now(timezone.utc).isoformat(),
              "method": "Один POST intake/upload через streaming multipart file handle и ASGITransport; два open_tree.",
              "measurement_scope": "Локальный ASGI без сети; RSS, физические disk I/O и холодный диск не измеряются.",
              "cache_conditions": "Контрольный SHA256 до POST прогревает файловый кэш.",
              "counter_semantics": (
                  "payload_write_bytes — сумма двух непересекающихся логических write-границ multipart spool и "
                  "browser payload fd; metadata и внутренний rollover.copy не считаются повторно. "
                  "На больших файлах проверяется настоящий штатный rollover; малый spool может оставаться в RAM. "
                  "full_archive_sha256_calls — завершённый SHA256 по размеру и digest исходного ZIP, без хранения блоков. "
                  "Source SHA/stat до/после и code guards идут вне I/O counters. "
                  "seconds включает инструментирование; тело запроса не собирается измерителем целиком."
              ),
              "runtime": {"python": sys.version.split()[0], "byteorder": sys.byteorder},
              "source_unchanged": None, "code_unchanged": None,
              "temporary_directory_removed": False, "child_process_stopped": False}
    args = None
    try:
        args = _parser().parse_args(argv)
        if args.report is not None:
            try:
                args.report.lstat()
            except FileNotFoundError:
                pass
            else:
                raise _Invalid("Файл отчёта уже существует.")
        report["timeout_seconds"] = args.timeout_seconds
        _run(args, report)
    except KeyboardInterrupt:
        report.update(status="incomplete", error_type="KeyboardInterrupt")
    except Exception as error:
        # Текст исключения может содержать приватные имена, пути или токены.
        report["status"] = "incomplete"
        report.setdefault("error_type", type(error).__name__)
    if args is not None and args.report is not None:
        try:
            with args.report.open("x", encoding="utf-8") as stream:
                stream.write(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
        except OSError as error:
            report.update(status="incomplete", report_error_type=type(error).__name__)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["status"] == "complete" else 2


if __name__ == "__main__":
    raise SystemExit(main())
