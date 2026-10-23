#!/usr/bin/env python3
"""Измерить повторные GET intake с готовым неподтверждённым Source B preview.

Запускать через .venv/bin/python. Один и тот же файл используется в baseline
и candidate деревьях src. ZIP читается на месте; контрольный SHA256 и prepare
прогревают файловый кэш. Холодный диск и расход памяти здесь не измеряются.
"""
from __future__ import annotations

import argparse
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
    """Входные условия измерения не выполнены."""


class _Parser(argparse.ArgumentParser):
    def error(self, message):
        # argparse включает переданный пользователем путь в некоторые ошибки.
        raise _Invalid("Некорректные аргументы; используйте --help.")

    def print_help(self, file=None):
        print(json.dumps({"status": "help", "help": self.format_help()}, ensure_ascii=False))


def _repeat(value):
    try:
        result = int(value)
    except ValueError:
        raise argparse.ArgumentTypeError("Ожидается целое число.") from None
    if result < 2:
        raise argparse.ArgumentTypeError("Нужно не менее двух GET.")
    return result


def _positive(value):
    try:
        result = float(value)
    except ValueError:
        raise argparse.ArgumentTypeError("Ожидается положительное число.") from None
    if not math.isfinite(result) or result <= 0:
        raise argparse.ArgumentTypeError("Таймаут должен быть конечным и положительным.")
    return result


def _parser():
    parser = _Parser(prog="measure_intake_preview.py", description=__doc__, add_help=False)
    parser.add_argument("-h", "--help", action="help", help="Справка в JSON.")
    parser.add_argument("--base-archive", required=True, type=Path, help="ZIP базы; только чтение.")
    parser.add_argument("--repeat", type=_repeat, default=2, help="Количество GET, по умолчанию 2.")
    parser.add_argument("--timeout-seconds", type=_positive, default=900.0,
                        help="Общий бюджет, включая prepare и SHA256, по умолчанию 900 секунд.")
    parser.add_argument("--report", type=Path, help="Создать новый JSON-файл без перезаписи.")
    return parser


def _remaining(deadline):
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise TimeoutError
    return remaining


def _identity(info):
    # atime меняется при чтении, поэтому в доказательство неизменности не входит.
    return {name: getattr(info, "st_" + name) for name in
            ("dev", "ino", "mode", "size", "mtime_ns", "ctime_ns")}


def _open_source(path):
    before = path.lstat()
    if not stat.S_ISREG(before.st_mode):
        raise _Invalid("Исходник должен быть обычным файлом без символической ссылки.")
    # NONBLOCK защищает от FIFO, подставленного между lstat и open.
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


def _code_identity(deadline, *, with_git=False):
    # Хешируется весь пакет, включая materializer, converter и их зависимости.
    package_root = _ROOT / "src" / "mcp1c"
    hashes = {}
    package = hashlib.sha256(b"mcp1c-package-code-v1\0")
    for path in sorted(package_root.rglob("*.py")):
        _remaining(deadline)
        name = path.relative_to(package_root).as_posix()
        encoded_name = name.encode("utf-8")
        size = path.stat().st_size
        digest = hashlib.sha256()
        package.update(len(encoded_name).to_bytes(8, "big"))
        package.update(encoded_name)
        package.update(size.to_bytes(8, "big"))
        read_size = 0
        with path.open("rb") as stream:
            while True:
                _remaining(deadline)
                block = stream.read(_CHUNK)
                if not block:
                    break
                read_size += len(block)
                package.update(block)
                digest.update(block)
        if read_size != size:
            raise _Invalid("Код изменился во время идентификации.")
        hashes[name] = digest.hexdigest()
    if not hashes:
        raise _Invalid("Дерево исходников отсутствует.")
    result = {"package_sha256": package.hexdigest(), "module_sha256": hashes}
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


class _GetCounters:
    """Scope начинается внутри snapshot в том же потоке, что работа GET."""

    def __init__(self):
        self.local = threading.local()
        self.counts = {}
        self.reset()

    def reset(self):
        self.counts = {"snapshot_calls": 0, "load_layer_payload_calls": 0,
                       "configuration_from_base_layer_calls": 0}

    def install(self, stack, service_type, bundle_module, runtime_module):
        original_snapshot = service_type.snapshot

        def snapshot(service, *args, **kwargs):
            depth = getattr(self.local, "depth", 0)
            self.local.depth = depth + 1
            self.counts["snapshot_calls"] += 1
            try:
                return original_snapshot(service, *args, **kwargs)
            finally:
                self.local.depth = depth

        stack.enter_context(patch.object(service_type, "snapshot", snapshot))
        originals = {
            "load_layer_payload": bundle_module.load_layer_payload,
            "configuration_from_base_layer": runtime_module.configuration_from_base_layer,
        }
        bindings = {}
        for name, original in originals.items():
            def counted(*args, _original=original, _name=name, **kwargs):
                if getattr(self.local, "depth", 0):
                    self.counts[_name + "_calls"] += 1
                return _original(*args, **kwargs)

            bindings[name] = 0
            # from-import создаёт самостоятельную ссылку. Одной подмены
            # определения недостаточно: registry и operations держат aliases.
            for module_name, module in tuple(sys.modules.items()):
                if module is None or not module_name.startswith("mcp1c."):
                    continue
                for attribute, value in tuple(vars(module).items()):
                    if value is original:
                        stack.enter_context(patch.object(module, attribute, counted))
                        bindings[name] += 1
        return bindings


def _request_record(response, number, elapsed, counts, job_id):
    row = {"number": number, "seconds": elapsed, "http_status": response.status_code,
           "candidate_count": None, "job_count": None, "issue_count": None,
           "preview_present": False, "job_done": False, "commit_absent": False,
           "preview_semantic_sha256": None,
           **counts}
    try:
        payload = response.json()
    except ValueError:
        return row
    if not isinstance(payload, dict):
        return row
    for field, label in (("candidates", "candidate_count"), ("jobs", "job_count"),
                         ("issues", "issue_count")):
        if isinstance(payload.get(field), list):
            row[label] = len(payload[field])
    jobs = payload.get("jobs")
    if isinstance(jobs, list):
        matches = [job for job in jobs if isinstance(job, dict) and job.get("job_id") == job_id]
        if len(matches) == 1:
            job = matches[0]
            row.update(preview_present=isinstance(job.get("preview"), dict),
                       job_done=job.get("state") == "done", commit_absent=job.get("commit") is None)
            if row["preview_present"]:
                semantic = {key: value for key, value in job["preview"].items()
                            if key not in {"base_generation_id", "candidate_generation_id"}}
                encoded = json.dumps(semantic, sort_keys=True, separators=(",", ":")).encode("utf-8")
                row["preview_semantic_sha256"] = hashlib.sha256(encoded).hexdigest()
    return row


def _measure(connection, source, root, repeat, deadline):
    from starlette.applications import Starlette
    from starlette.testclient import TestClient

    from mcp1c import intake_v2_registry, intake_v2_runtime
    from mcp1c.dashboard_runtime import DASHBOARD_ON, routes
    from mcp1c.intake_v2 import CandidateJobState, DurableCandidateStore, SourceKind
    from mcp1c.intake_v2_api import IntakeApiService
    from mcp1c.intake_v2_lifecycle import CandidateCatalog, IntakeLifecycle
    from mcp1c.intake_v2_operations import IntakeCoordinator
    from mcp1c.intake_v2_transport import BrowserStagingStore
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
            if before != _identity(os.fstat(stream.fileno())) or before != _identity(source.lstat()):
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
            lifecycle = IntakeLifecycle(
                CandidateCatalog(storage / "catalog"), BrowserStagingStore(storage / "uploads"),
                IntakeCoordinator(storage / "operations", DurableCandidateStore(storage / "records")),
                incoming_root=incoming, config_sources_root=config_sources,
                local_sources={"source-0": source}, directory_settle_seconds=0,
            )
            service = IntakeApiService(registry, lifecycle)
            _emit(connection, stage="discover")
            discovered = service.snapshot()
            _emit(connection, discovery={"candidate_count": len(discovered["candidates"]),
                                         "issue_count": len(discovered["issues"])})
            if len(discovered["candidates"]) != 1 or discovered["issues"]:
                raise _Invalid("Вход не обнаружен однозначно.")
            candidate = lifecycle.catalog.load(discovered["candidates"][0]["id"])
            if (candidate.locator.source_id != "source-0"
                    or candidate.probe.source_kind is not SourceKind.CONFIGURATION
                    or candidate.probe.raw_sha256 != source_sha):
                raise _Invalid("Probe не соответствует базе.")
            _emit(connection, stage="start")
            started = time.monotonic()
            work = service.start(candidate.candidate_id, "create")
            start_seconds = time.monotonic() - started
            _emit(connection, stage="prepare")
            started = time.monotonic()
            service.prepare(work)
            prepare_seconds = time.monotonic() - started
            preview = lifecycle.operations.load_preview(work.job_id)
            job = lifecycle.operations.records.load_job(work.job_id)
            member_count = sum(len(payload.members) for payload in preview.materialized.payloads.values())
            member_bytes = sum(member.member.size for payload in preview.materialized.payloads.values()
                               for member in payload.members)
            _emit(connection, prepared={"seconds": prepare_seconds, "start_seconds": start_seconds,
                                        "member_count": member_count,
                                        "member_bytes": member_bytes, "job_done": job.state is CandidateJobState.DONE,
                                        "active_count": len(registry.snapshot().generations)})
            if job.state is not CandidateJobState.DONE or member_count <= 0 or member_bytes <= 0:
                raise _Invalid("Preview не готов или не содержит members.")
            # Ни confirm, ни discard не вызываются: измеряется сохранённый DONE.
            del preview
            _emit(connection, stage="asgi_setup")
            reference = ReferenceService.discover(data, embedded_path=root / "missing-reference.mcp1cref")
            app = Starlette(routes=routes(registry, mode=DASHBOARD_ON, intake=service, reference=reference))
            counters = _GetCounters()
            successful = True
            with ExitStack() as stack:
                bindings = counters.install(stack, IntakeApiService, intake_v2_registry, intake_v2_runtime)
                _emit(connection, counter_bindings=bindings)
                client = stack.enter_context(TestClient(app, base_url="http://127.0.0.1",
                                                        raise_server_exceptions=False))
                for number in range(1, repeat + 1):
                    _remaining(deadline)
                    _emit(connection, stage="get_intake", request_number=number)
                    counters.reset()
                    started = time.monotonic()
                    response = client.get("/api/v1/sources/intake",
                                          headers={"x-admin-token": os.environ["ADMIN_TOKEN"]})
                    elapsed = time.monotonic() - started
                    row = _request_record(response, number, elapsed, counters.counts.copy(), work.job_id)
                    _emit(connection, request=row)
                    successful = successful and (
                        row["http_status"] == 200 and row["candidate_count"] == 1
                        and row["job_count"] == 1 and row["issue_count"] == 0
                        and row["preview_present"] and row["job_done"] and row["commit_absent"]
                        and row["snapshot_calls"] == 1
                    )
            job = lifecycle.operations.records.load_job(work.job_id)
            try:
                lifecycle.operations.load_commit(work.job_id)
            except KeyError:
                commit_absent = True
            else:
                commit_absent = False
            unchanged_preview = (job.state is CandidateJobState.DONE and commit_absent
                                 and len(registry.snapshot().generations) == 0)
            _emit(connection, requests_successful=successful, preview_unconfirmed=unchanged_preview)
            if not successful or not unchanged_preview:
                raise _Invalid("GET не сохранил готовый неподтверждённый preview.")
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


def _child(connection, source, root, repeat, deadline):
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
            _emit(connection, actor_code_before=_code_identity(deadline))
            _emit(connection, stage="imports")
            _measure(connection, source, root, repeat, deadline)
            _emit(connection, actor_code_after=_code_identity(deadline), finished=True)
        except BaseException as error:
            _emit(connection, error_type=type(error).__name__)
        finally:
            connection.close()


def _stop(process):
    if process.pid is None:
        return
    # После join PID может быть переиспользован: killpg разрешён только живому actor.
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
        source = args.base_archive.absolute()
        context = multiprocessing.get_context("spawn")
        with tempfile.TemporaryDirectory(prefix="mcp1c-intake-preview-") as temporary:
            receiver, sender = context.Pipe(duplex=False)
            process = context.Process(target=_child, args=(sender, source, Path(temporary), args.repeat, deadline))
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
                        if "request" in event:
                            report["requests"].append(event.pop("request"))
                        finished = event.pop("finished", False) or finished
                        report.update(event)
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
                report["code_unchanged"] = all(
                    report.get(key, {}).get("package_sha256") == report["code"]["package_sha256"]
                    for key in ("actor_code_before", "actor_code_after")
                )
                preview_hashes = [row["preview_semantic_sha256"] for row in report["requests"]]
                report["preview_consistent"] = (len(preview_hashes) == args.repeat
                                                 and all(preview_hashes) and len(set(preview_hashes)) == 1)
                if (finished and process.exitcode == 0 and report.get("requests_successful")
                        and report.get("preview_unconfirmed") and report.get("source_unchanged") is True
                        and report["code_unchanged"] and report["preview_consistent"]
                        and len(report["requests"]) == args.repeat):
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
    report = {"schema_version": 1, "status": "incomplete", "requests": [],
              "measured_at": datetime.now(timezone.utc).isoformat(),
              "method": "production prepare без confirm; GET intake через Starlette TestClient",
              "cache_conditions": "После контрольного SHA256 и prepare; холодный диск не измеряется.",
              "source_unchanged": None, "preview_consistent": None,
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
        report.update(repeat=args.repeat, timeout_seconds=args.timeout_seconds)
        _run(args, report)
    except KeyboardInterrupt:
        report.update(status="incomplete", error_type="KeyboardInterrupt")
    except Exception as error:
        # str(error) может включать приватные пути, имена и содержимое кандидата.
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
