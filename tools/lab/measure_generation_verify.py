#!/usr/bin/env python3
"""Измерить prepare, publish и восстановление поколений на изолированном data.

Один и тот же файл запускается в baseline и candidate деревьях src. Исходные
ZIP читаются на месте; файловый кэш прогревается контрольными SHA256. Диск
холодным не считается. Тяжёлые владельцы Registry работают строго по очереди.
"""
from __future__ import annotations

import argparse
from contextlib import redirect_stderr, redirect_stdout
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
from tempfile import TemporaryDirectory
import tempfile
import threading
import time


_ROOT = Path(__file__).resolve().parents[2]
_CHUNK = 1024 * 1024
_MODULES = (
    "registry", "registry_types", "intake_v2", "intake_v2_api",
    "intake_v2_lifecycle", "intake_v2_operations", "intake_v2_registry",
    "intake_v2_transport", "member_pack", "cli", "index_cache", "fsio", "package_digest",
    "intake_v2_runtime", "module_content", "bsl_lex", "readers.modules",
)


class _Invalid(ValueError):
    """Входные условия измерения не выполнены."""


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
    parser = _Parser(description=__doc__, add_help=False)
    parser.add_argument("-h", "--help", action="help", help="Справка в JSON.")
    parser.add_argument("--base-archive", required=True, type=Path, help="ZIP базы, только чтение.")
    parser.add_argument("--extension-archive", action="append", default=[], type=Path,
                        help="ZIP расширения; параметр можно повторять.")
    parser.add_argument("--timeout-seconds", type=_positive, default=900.0,
                        help="Общий бюджет всех этапов, по умолчанию 900 секунд.")
    parser.add_argument("--report", type=Path, help="Создать новый JSON-файл, без перезаписи.")
    return parser


def _remaining(deadline):
    value = deadline - time.monotonic()
    if value <= 0:
        raise TimeoutError
    return value


def _identity(info):
    # atime меняется от чтения и не служит признаком изменения входа.
    return {name: getattr(info, "st_" + name) for name in
            ("dev", "ino", "mode", "size", "mtime_ns", "ctime_ns")}


def _open_source(path):
    before = path.lstat()
    if not stat.S_ISREG(before.st_mode):
        raise _Invalid("Исходник должен быть обычным файлом.")
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        opened = os.fstat(descriptor)
        if not stat.S_ISREG(opened.st_mode) or _identity(opened) != _identity(before):
            raise _Invalid("Исходник изменился при открытии.")
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
            break
        digest.update(block)
        size += len(block)
    return digest.hexdigest(), size


def _code_identity(deadline, *, with_git=False):
    result = {"module_sha256": {}}
    for name in _MODULES:
        with (_ROOT / "src" / "mcp1c" / (name.replace(".", "/") + ".py")).open("rb") as stream:
            result["module_sha256"]["mcp1c." + name] = _hash(stream, deadline)[0]
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


def _inputs(connection, sources, deadline, *, previous=None):
    records = []
    for index, path in enumerate(sources):
        _emit(connection, stage="source_after" if previous is not None else "source_before",
              source_index=index)
        started = time.monotonic()
        with _open_source(path)[0] as stream:
            before = _identity(os.fstat(stream.fileno()))
            digest, size = _hash(stream, deadline)
            after = _identity(os.fstat(stream.fileno()))
            if before != after or after != _identity(path.lstat()):
                raise _Invalid("Исходник изменился во время хеширования.")
        row = {"index": index, "bytes": size, "stat": after, "sha256": digest,
               "hash_seconds": time.monotonic() - started}
        if previous is not None:
            row["unchanged"] = all(row[key] == previous[index][key]
                                   for key in ("index", "bytes", "stat", "sha256"))
        records.append(row)
        _emit(connection, **{"sources_after" if previous is not None else "sources_before": records})
    return records


class _Metrics:
    """Счётчики проходят через реальные потоки; materialize в stage не входит."""

    def __init__(self, registry_module, bundle_module, member_pack):
        self.local = threading.local()
        self.counts = {}
        self.reset()
        original_verify = bundle_module.GenerationBundleStore.verify
        original_read = member_pack._PackedReader.read
        original_stage = registry_module.Registry.stage_generation
        original_write = member_pack._EntryWriter.write

        def verify(instance, *args, **kwargs):
            depth = getattr(self.local, "verify", 0)
            self.counts["verify_calls"] += 1
            self.local.verify = depth + 1
            try:
                return original_verify(instance, *args, **kwargs)
            finally:
                self.local.verify = depth

        def read(stream, *args, **kwargs):
            value = original_read(stream, *args, **kwargs)
            if getattr(self.local, "verify", 0):
                self.counts["packed_read_calls_within_verify"] += 1
                self.counts["packed_bytes_within_verify"] += len(value)
            return value

        def stage(instance, manifest, payloads, *args, **kwargs):
            self.counts["stage_calls"] += 1
            for source in payloads.values():
                if isinstance(source, bundle_module.LayerPayloadSource):
                    self.counts["member_count"] += len(source.members)
                    self.counts["member_bytes"] += sum(item.member.size for item in source.members)
            depth = getattr(self.local, "stage", 0)
            self.local.stage = depth + 1
            try:
                return original_stage(instance, manifest, payloads, *args, **kwargs)
            finally:
                self.local.stage = depth

        def write(stream, value, *args, **kwargs):
            written = original_write(stream, value, *args, **kwargs)
            if getattr(self.local, "stage", 0):
                self.counts["stage_copy_hashed_calls"] += 1
                self.counts["stage_copy_hashed_bytes"] += written
            return written

        bundle_module.GenerationBundleStore.verify = verify
        member_pack._PackedReader.read = read
        registry_module.Registry.stage_generation = stage
        member_pack._EntryWriter.write = write

    def reset(self):
        self.counts = dict.fromkeys((
            "verify_calls", "packed_read_calls_within_verify", "packed_bytes_within_verify",
            "stage_calls", "stage_copy_hashed_calls", "stage_copy_hashed_bytes",
            "member_count", "member_bytes",
        ), 0)


def _fingerprint(registry):
    snapshot = registry.snapshot()
    rows = []
    for generation in snapshot.generations.values():
        manifest = generation.manifest
        rows.append({
            "raw_sha256": manifest.raw_sha256,
            "source_kind": manifest.identity.source_kind.value,
            "layers": [{"kind": layer.kind.value, "state": layer.state.value,
                        "content_sha256": layer.content_sha256,
                        "payload_sha256": layer.payload_sha256, "items": layer.items_total}
                       for layer in sorted(manifest.layers, key=lambda layer: layer.kind.value)],
        })
    rows.sort(key=lambda row: (row["raw_sha256"], row["source_kind"]))
    encoded = json.dumps(rows, sort_keys=True, separators=(",", ":")).encode()
    return {"active_count": len(rows), "configuration_count": len(snapshot.configurations),
            "manifest_fingerprint_sha256": hashlib.sha256(encoded).hexdigest(),
            "manifests": rows, "registry_role": registry.role}


def _measure_stage(connection, metrics, name, function, registry, *, source_index=None):
    metrics.reset()
    _emit(connection, stage=name, source_index=source_index)
    started = time.monotonic()
    function()
    row = {"stage": name, "source_index": source_index,
           "seconds": time.monotonic() - started, **metrics.counts,
           "active_count": len(registry.snapshot().generations)}
    _emit(connection, measurement=row)


def _actor(connection, mode, root, sources, deadline, previous):
    os.setsid()
    # Никакого .env рабочего каталога и библиотечных сообщений с именами/путями.
    os.chdir(root)
    temporary = root / "tmp"
    temporary.mkdir(exist_ok=True)
    os.environ.update(TMPDIR=str(temporary), TEMP=str(temporary), TMP=str(temporary))
    tempfile.tempdir = None
    os.environ["API_TOKEN"] = secrets.token_urlsafe(32)
    os.environ["ADMIN_TOKEN"] = secrets.token_urlsafe(32)
    sys.path.insert(0, str(_ROOT / "src"))
    with open(os.devnull, "w") as sink, redirect_stdout(sink), redirect_stderr(sink):
        try:
            _emit(connection, actor_code=_code_identity(deadline))
            if mode == "sources_after":
                _inputs(connection, sources, deadline, previous=previous)
            else:
                from mcp1c import registry as registry_module, intake_v2_registry, member_pack
                metrics = _Metrics(registry_module, intake_v2_registry, member_pack)
                if mode == "publish":
                    _publish(connection, root, sources, deadline, registry_module, metrics)
                else:
                    registry = registry_module.Registry(root / "data")
                    for name in ("startup", "reload"):
                        _measure_stage(connection, metrics, name, registry.startup, registry)
                        _emit(connection, **{name + "_snapshot": _fingerprint(registry)})
            _emit(connection, finished=True)
        except BaseException as error:
            _emit(connection, error_type=type(error).__name__)
        finally:
            connection.close()


def _publish(connection, root, sources, deadline, registry_module, metrics):
    from mcp1c.intake_v2 import DurableCandidateStore, SourceKind
    from mcp1c.intake_v2_api import IntakeApiService
    from mcp1c.intake_v2_lifecycle import CandidateCatalog, IntakeLifecycle
    from mcp1c.intake_v2_operations import IntakeCoordinator
    from mcp1c.intake_v2_transport import BrowserStagingStore

    before = _inputs(connection, sources, deadline)
    registry = registry_module.Registry(root / "data")
    registry.startup()
    if registry.role != "owner":
        raise _Invalid("Изолированный Registry не стал владельцем.")
    storage = root / "intake"
    config_sources = root / "config-sources"
    config_sources.mkdir()
    incoming = root / "empty-incoming"
    incoming.mkdir()
    lifecycle = IntakeLifecycle(
        CandidateCatalog(storage / "catalog"), BrowserStagingStore(storage / "uploads"),
        IntakeCoordinator(storage / "operations", DurableCandidateStore(storage / "records")),
        local_sources={f"source-{index}": path for index, path in enumerate(sources)},
        config_sources_root=config_sources, incoming_root=incoming,
        directory_settle_seconds=0,
    )
    service = IntakeApiService(registry, lifecycle)
    _emit(connection, stage="discover")
    payload = service.snapshot()
    _emit(connection, discovery={"candidate_count": len(payload["candidates"]),
                                 "issue_count": len(payload["issues"])})
    if payload["issues"] or len(payload["candidates"]) != len(sources):
        raise _Invalid("Не все входы обнаружены однозначно.")
    by_source = {}
    for item in payload["candidates"]:
        candidate = lifecycle.catalog.load(item["id"])
        source_id = candidate.locator.source_id
        if source_id in by_source:
            raise _Invalid("Несколько кандидатов на один вход.")
        by_source[source_id] = candidate
    parent = ""
    for index in range(len(sources)):
        _remaining(deadline)
        candidate = by_source[f"source-{index}"]
        expected = SourceKind.CONFIGURATION if index == 0 else SourceKind.EXTENSION
        if candidate.probe.source_kind is not expected or candidate.probe.raw_sha256 != before[index]["sha256"]:
            raise _Invalid("Probe не соответствует источнику или его назначению.")
        _emit(connection, stage="start", source_index=index)
        work = service.start(candidate.candidate_id, "create" if index == 0 else "update_full",
                             parent_configuration=parent if index else "")
        _measure_stage(connection, metrics, "prepare", lambda: service.prepare(work), registry,
                       source_index=index)
        _measure_stage(connection, metrics, "confirm", lambda: service.confirm(work.job_id), registry,
                       source_index=index)
        if index == 0:
            parent = candidate.probe.internal_name
    _emit(connection, published_snapshot=_fingerprint(registry))


def _stop_actor(process):
    if process.pid is None:
        return
    # is_alive/poll перед killpg защищает от PID, уже переиспользованного ОС.
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


def _run_actor(mode, root, sources, deadline, report):
    _remaining(deadline)
    context = multiprocessing.get_context("spawn")
    receiver, sender = context.Pipe(duplex=False)
    process = context.Process(target=_actor, args=(sender, mode, root, sources, deadline,
                                                  report.get("sources_before")))
    finished = False
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
                if "actor_code" in event:
                    actor_code = event.pop("actor_code")
                    report.setdefault("actor_code_sha256", {})[mode] = actor_code["module_sha256"]
                    if actor_code["module_sha256"] != report["code"]["module_sha256"]:
                        raise _Invalid("Код изменился между процессами.")
                finished = finished or event.pop("finished", False)
                report.update(event)
                if "error_type" in event:
                    raise _Invalid("Ошибка дочернего этапа.")
            elif not process.is_alive():
                break
        process.join(timeout=_remaining(deadline))
        if process.is_alive():
            raise TimeoutError
        if not finished or process.exitcode != 0:
            raise _Invalid("Дочерний процесс не завершил протокол.")
    finally:
        _stop_actor(process)
        sender.close()
        receiver.close()


def _run_cli(root, deadline, report):
    _remaining(deadline)
    env = os.environ.copy()
    env.update(API_TOKEN=secrets.token_urlsafe(32), ADMIN_TOKEN=secrets.token_urlsafe(32),
               PYTHONPATH=str(_ROOT / "src"), PYTHONNOUSERSITE="1",
               TMPDIR=str(root / "tmp"), TEMP=str(root / "tmp"), TMP=str(root / "tmp"))
    report["stage"] = "cli_reg_list"
    started = time.monotonic()
    # Файлы не растут в RAM и удаляются вместе с temp, даже при таймауте.
    with (root / "cli.stdout").open("w+b") as stdout, (root / "cli.stderr").open("w+b") as stderr:
        process = subprocess.Popen(
            [sys.executable, "-m", "mcp1c.cli", "reg-list", "--data", str(root / "data")],
            cwd=root, env=env, stdout=stdout, stderr=stderr, start_new_session=True,
        )
        try:
            process.wait(timeout=_remaining(deadline))
            output_sha, output_size = _hash(stdout, deadline)
            error_sha, error_size = _hash(stderr, deadline)
            report["cli"] = {"seconds": time.monotonic() - started, "exit_code": process.returncode,
                             "stdout_bytes": output_size, "stdout_sha256": output_sha,
                             "stderr_bytes": error_size, "stderr_sha256": error_sha}
            if process.returncode != 0 or output_size == 0:
                raise _Invalid("CLI не подтвердил чтение реестра.")
        finally:
            try:
                if process.poll() is None and os.getpgid(process.pid) == process.pid:
                    os.killpg(process.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            if process.poll() is None:
                try:
                    process.wait(timeout=1)
                except subprocess.TimeoutExpired:
                    if process.poll() is None:
                        try:
                            if os.getpgid(process.pid) == process.pid:
                                os.killpg(process.pid, signal.SIGKILL)
                        except ProcessLookupError:
                            pass
                        process.kill()
            process.wait()


def _run(args, report):
    started = time.monotonic()
    deadline = started + args.timeout_seconds
    report["code"] = _code_identity(deadline, with_git=True)
    sources = [path.absolute() for path in (args.base_archive, *args.extension_archive)]
    try:
        with TemporaryDirectory(prefix="mcp1c-generation-verify-") as temporary:
            root = Path(temporary)
            try:
                _run_actor("publish", root, sources, deadline, report)
                _run_actor("restore", root, sources, deadline, report)
                _run_cli(root, deadline, report)
                _run_actor("sources_after", root, sources, deadline, report)
                expected = len(sources)
                snapshots = [report[name + "_snapshot"] for name in ("published", "startup", "reload")]
                report["snapshot_consistent"] = all(
                    snapshot["active_count"] == expected and snapshot["configuration_count"] == 1
                    and snapshot["registry_role"] == "owner"
                    and snapshot["manifest_fingerprint_sha256"] == snapshots[0]["manifest_fingerprint_sha256"]
                    for snapshot in snapshots
                )
                report["source_unchanged"] = (len(report["sources_after"]) == expected and
                                              all(row["unchanged"] for row in report["sources_after"]))
                confirmations = [row for row in report["measurements"] if row["stage"] == "confirm"]
                report["number_nonempty_member_sources"] = sum(
                    row["member_count"] > 0 and row["member_bytes"] > 0 for row in confirmations
                )
                report["nonempty_members"] = (
                    len(confirmations) == expected
                    and sum(row["member_count"] for row in confirmations) > 0
                    and sum(row["member_bytes"] for row in confirmations) > 0
                    and confirmations[0]["source_index"] == 0
                    and confirmations[0]["member_count"] > 0
                    and confirmations[0]["member_bytes"] > 0
                )
                if not all(report[key] for key in ("snapshot_consistent", "source_unchanged", "nonempty_members")):
                    raise _Invalid("Приёмочные условия измерения не выполнены.")
                report["status"] = "complete"
                report["stage"] = "finished"
            finally:
                report["child_processes_stopped"] = True
    finally:
        report["total_seconds"] = time.monotonic() - started
        report["temporary_directory_removed"] = "temporary" in locals() and not Path(temporary).exists()


def main(argv=None):
    report = {"schema_version": 1, "status": "incomplete", "measurements": [],
              "measured_at": datetime.now(timezone.utc).isoformat(),
              "method": "production intake; отдельные последовательные процессы publish, restore, CLI",
              "cache_conditions": "Входной SHA256 прогревает файловый кэш; холодный диск не измеряется.",
              "source_unchanged": None, "temporary_directory_removed": False,
              "child_processes_stopped": False}
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
        report.update(timeout_seconds=args.timeout_seconds, archive_count=1 + len(args.extension_archive))
        _run(args, report)
    except KeyboardInterrupt:
        report.update(status="incomplete", error_type="KeyboardInterrupt")
    except Exception as error:
        # Текст исключения может включать приватные пути и имена конфигураций.
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
