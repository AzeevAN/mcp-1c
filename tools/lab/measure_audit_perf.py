#!/usr/bin/env python3
"""Измерить повторные GET intake на изолированных копиях исходных ZIP.

Запускать одним интерпретатором из окружения проекта. Настоящий HTTP-сервер
не поднимается. Копирование уже прогревает файловый кэш: первый GET тоже
не является измерением холодного диска.
"""

from __future__ import annotations

import argparse
from contextlib import ExitStack, redirect_stderr, redirect_stdout
from dataclasses import asdict
from datetime import datetime, timezone
import hashlib
import json
import math
import multiprocessing
from multiprocessing.connection import Connection
import os
from pathlib import Path
import secrets
import shutil
import signal
import stat
import subprocess
import sys
from tempfile import TemporaryDirectory
import time
from unittest.mock import patch
import zipfile


_REPOSITORY = Path(__file__).resolve().parents[2]
_CHUNK_BYTES = 1024 * 1024


class _ArgumentsError(ValueError):
    """Аргументы не позволяют безопасно запустить измерение."""


class _Parser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        # Сообщение argparse может содержать приватный путь из аргумента.
        raise _ArgumentsError("Некорректные аргументы; используйте --help.")

    def print_help(self, file=None) -> None:
        help_text = self.format_help().replace("usage: ", "Использование: ", 1)
        print(json.dumps({"status": "help", "help": help_text}, ensure_ascii=False))


def _repeat(value: str) -> int:
    try:
        result = int(value)
    except ValueError:
        raise argparse.ArgumentTypeError("Ожидается целое число.") from None
    if result < 2:
        raise argparse.ArgumentTypeError("Нужно не менее двух GET.")
    return result


def _timeout(value: str) -> float:
    try:
        result = float(value)
    except ValueError:
        raise argparse.ArgumentTypeError("Ожидается положительное число секунд.") from None
    if not math.isfinite(result) or result <= 0:
        raise argparse.ArgumentTypeError("Таймаут должен быть конечным и положительным.")
    return result


def _parser() -> argparse.ArgumentParser:
    parser = _Parser(description=__doc__, add_help=False)
    parser._optionals.title = "Параметры"
    parser.add_argument("-h", "--help", action="help", help="Показать справку и завершить работу.")
    parser.add_argument("--incoming-archive", type=Path, action="append", required=True, help="Исходный ZIP; только чтение. Повторите параметр для нескольких архивов.")
    parser.add_argument("--repeat", type=_repeat, default=2, help="Количество GET в одном сервисе (по умолчанию 2).")
    parser.add_argument("--timeout-seconds", type=_timeout, default=180.0, help="Общий бюджет дочернего процесса, включая копирование и хеши.")
    parser.add_argument("--report", type=Path, help="Новый JSON-файл отчёта по всем архивам; существующий файл не перезаписывается.")
    return parser


def _identity(info: os.stat_result) -> dict[str, int]:
    # atime может меняться от чтения; он не доказывает изменение исходника.
    return {
        "device": info.st_dev,
        "inode": info.st_ino,
        "mode": info.st_mode,
        "size": info.st_size,
        "mtime_ns": info.st_mtime_ns,
        "ctime_ns": info.st_ctime_ns,
    }


def _open_source(path: Path):
    before = path.lstat()
    if not stat.S_ISREG(before.st_mode):
        raise _ArgumentsError("Исходник должен быть обычным файлом без символической ссылки.")
    # NONBLOCK не даёт зависнуть на FIFO, подставленном между lstat и open.
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        opened = os.fstat(descriptor)
        if not stat.S_ISREG(opened.st_mode) or _identity(before) != _identity(opened):
            raise _ArgumentsError("Исходник изменился во время открытия.")
        return os.fdopen(descriptor, "rb"), _identity(opened)
    except BaseException:
        os.close(descriptor)
        raise


def _hash_stream(stream) -> tuple[str, float]:
    started = time.monotonic()
    stream.seek(0)
    digest = hashlib.sha256()
    for block in iter(lambda: stream.read(_CHUNK_BYTES), b""):
        digest.update(block)
    return digest.hexdigest(), time.monotonic() - started


def _code_identity(modules: tuple) -> dict:
    hashes = {}
    for module in modules:
        with Path(module.__file__).open("rb") as stream:
            hashes[module.__name__] = _hash_stream(stream)[0]
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--verify", "HEAD"],
            cwd=_REPOSITORY,
            capture_output=True,
            text=True,
            check=False,
            timeout=5,
        )
        head = result.stdout.strip()
        if result.returncode != 0 or len(head) not in {40, 64} or any(c not in "0123456789abcdef" for c in head):
            head = None
    except (OSError, subprocess.TimeoutExpired):
        head = None
    return {"git_head": head, "module_sha256": hashes}


def _send(connection: Connection, **fields) -> None:
    connection.send({"update": fields})


def _source_record(index: int) -> dict:
    return {
        "index": index, "bytes": None,
        "source_stat_before": None, "source_stat_after": None,
        "source_sha256_before": None, "source_sha256_after": None,
        "source_unchanged": None,
        "hash_before_seconds": None, "copy_seconds": None,
        "hash_after_seconds": None,
    }


def _send_sources(connection: Connection, records: list[dict]) -> None:
    fields = {"sources": records, "archive_count": len(records)}
    for total, key in (
        ("archive_bytes", "bytes"),
        ("hash_before_seconds", "hash_before_seconds"),
        ("copy_seconds", "copy_seconds"),
        ("hash_after_seconds", "hash_after_seconds"),
    ):
        values = [record[key] for record in records]
        fields[total] = sum(values) if all(value is not None for value in values) else None
    unchanged = [record["source_unchanged"] for record in records]
    fields["source_unchanged"] = (
        False if any(value is False for value in unchanged)
        else True if all(value is True for value in unchanged) else None
    )
    if len(records) == 1:
        # Поля прежнего отчёта сохраняются при единственном входе.
        for key in ("source_stat_before", "source_stat_after", "source_sha256_before", "source_sha256_after"):
            fields[key] = records[0][key]
    _send(connection, **fields)


def _measure(connection: Connection, sources: list[Path], root: Path, repeat: int) -> None:
    os.environ["API_TOKEN"] = secrets.token_urlsafe(32)
    os.environ["ADMIN_TOKEN"] = secrets.token_urlsafe(32)
    # Импортируется именно рабочая копия кода; parent не создаёт сервис.
    sys.path.insert(0, str(_REPOSITORY / "src"))
    from starlette.applications import Starlette
    from starlette.testclient import TestClient

    from mcp1c import incoming, intake_v2_lifecycle, intake_v2_transport
    from mcp1c.dashboard_runtime import DASHBOARD_ON, routes
    from mcp1c.intake_v2 import DurableCandidateStore
    from mcp1c.intake_v2_api import IntakeApiService
    from mcp1c.intake_v2_lifecycle import CandidateCatalog, IntakeLifecycle
    from mcp1c.intake_v2_operations import IntakeCoordinator
    from mcp1c.intake_v2_transport import BrowserStagingStore, ZipExportTree
    from mcp1c.reference_provider import ReferenceService
    from mcp1c.registry import Registry

    _send(connection, stage="идентификация кода", code=_code_identity((intake_v2_lifecycle, incoming, intake_v2_transport)))
    records = [_source_record(index) for index in range(1, len(sources) + 1)]
    _send_sources(connection, records)
    with ExitStack() as stack:
        opened = []
        try:
            incoming_root = root / "data" / "incoming"
            incoming_root.mkdir(parents=True)
            for source, record in zip(sources, records):
                _send(connection, stage="контроль исходника до копирования", source_index=record["index"])
                stream, before = _open_source(source)
                stack.enter_context(stream)
                opened.append((source, stream, record))
                record["source_stat_before"] = before
                record["bytes"] = before["size"]
                record["source_sha256_before"], record["hash_before_seconds"] = _hash_stream(stream)
                _send_sources(connection, records)
                name = "archive.zip" if len(sources) == 1 else f"archive-{record['index']}.zip"
                copied = incoming_root / name
                _send(connection, stage="копирование", source_index=record["index"])
                started = time.monotonic()
                stream.seek(0)
                # Потоковая запись создаёт физическую копию, без hardlink/reflink.
                with copied.open("xb") as output:
                    shutil.copyfileobj(stream, output, length=_CHUNK_BYTES)
                record["copy_seconds"] = time.monotonic() - started
                _send_sources(connection, records)
                if not zipfile.is_zipfile(copied):
                    raise _ArgumentsError("Исходник не является ZIP.")
                old = time.time() - incoming.SETTLE_SECONDS - 1
                os.utime(copied, (old, old))
            registry = Registry(root / "data")
            storage = root / "intake"
            browser = BrowserStagingStore(storage / "uploads")
            candidate_records = DurableCandidateStore(storage / "records")
            lifecycle = IntakeLifecycle(
                CandidateCatalog(storage / "catalog"), browser,
                IntakeCoordinator(storage / "operations", candidate_records),
                incoming_root=registry.incoming_dir,
                config_sources_root=root / "config-sources",
                directory_settle_seconds=0,
            )
            service = IntakeApiService(registry, lifecycle)
            reference = ReferenceService.discover(
                registry.data_dir, embedded_path=root / "missing-reference.mcp1cref"
            )
            app = Starlette(routes=routes(
                registry, mode=DASHBOARD_ON, intake=service, reference=reference,
            ))
            _send(connection, stage="GET intake", intake_limits=asdict(lifecycle.intake_limits))
            counts = {"discover": 0, "zip_source_sha256": 0}
            original_discover = IntakeLifecycle._discover
            original_sha = ZipExportTree.source_sha256

            def discover(instance, *args, **kwargs):
                counts["discover"] += 1
                return original_discover(instance, *args, **kwargs)

            def source_sha(instance, *args, **kwargs):
                counts["zip_source_sha256"] += 1
                return original_sha(instance, *args, **kwargs)

            successful = True
            with patch.object(IntakeLifecycle, "_discover", discover), patch.object(ZipExportTree, "source_sha256", source_sha):
                with TestClient(app, base_url="http://127.0.0.1", raise_server_exceptions=False) as client:
                    for index in range(1, repeat + 1):
                        previous = counts.copy()
                        started = time.monotonic()
                        response = client.get("/api/v1/sources/intake", headers={"x-admin-token": os.environ["ADMIN_TOKEN"]})
                        duration = time.monotonic() - started
                        candidate_count = None
                        issue_count = None
                        try:
                            payload = response.json()
                            if isinstance(payload, dict):
                                candidates = payload.get("candidates")
                                issues = payload.get("issues")
                                if isinstance(candidates, list):
                                    candidate_count = len(candidates)
                                if isinstance(issues, list):
                                    issue_count = len(issues)
                        except ValueError:
                            pass
                        row = {
                            "number": index, "duration_seconds": duration,
                            "http_status": response.status_code,
                            "candidate_count": candidate_count, "issue_count": issue_count,
                            "discover_calls": counts["discover"] - previous["discover"],
                            "zip_source_sha256_calls": counts["zip_source_sha256"] - previous["zip_source_sha256"],
                        }
                        connection.send({"request": row})
                        successful = successful and response.status_code == 200 and candidate_count == len(sources) and issue_count == 0
            _send(connection, requests_successful=successful)
        finally:
            for source, stream, record in opened:
                _send(connection, stage="контроль исходника после запросов", source_index=record["index"])
                try:
                    after_sha, after_seconds = _hash_stream(stream)
                    after = _identity(os.fstat(stream.fileno()))
                    try:
                        path_after = _identity(source.lstat())
                    except OSError:
                        path_after = None
                    record.update(
                        source_sha256_after=after_sha, hash_after_seconds=after_seconds,
                        source_stat_after=after,
                        source_unchanged=(
                            record["source_stat_before"] == after == path_after
                            and record["source_sha256_before"] == after_sha
                        ) if record["source_sha256_before"] is not None else None,
                    )
                except OSError as error:
                    # Ошибка одного чтения не отменяет контроль остальных входов.
                    record["source_check_error_type"] = type(error).__name__
                _send_sources(connection, records)


def _child(connection: Connection, sources: list[Path], root: Path, repeat: int) -> None:
    # Группа включает короткий git-процесс: timeout убирает и его потомков.
    os.setsid()
    # Библиотечные логи и исключения могут содержать имена конфигураций/пути.
    with open(os.devnull, "w") as sink, redirect_stdout(sink), redirect_stderr(sink):
        try:
            _measure(connection, sources, root, repeat)
            connection.send({"finished": True})
        except BaseException as error:
            connection.send({"error_type": type(error).__name__})
        finally:
            connection.close()


def _run(args: argparse.Namespace, report: dict) -> None:
    started = time.monotonic()
    deadline = started + args.timeout_seconds
    context = multiprocessing.get_context("spawn")
    # Temp принадлежит parent: terminate дочернего процесса не оставит копию.
    with TemporaryDirectory(prefix="mcp1c-audit-perf-") as temporary:
        receiver, sender = context.Pipe(duplex=False)
        sources = [source.absolute() for source in args.incoming_archive]
        process = context.Process(target=_child, args=(sender, sources, Path(temporary), args.repeat))
        try:
            process.start()
            sender.close()
            finished = False
            while time.monotonic() < deadline:
                if receiver.poll(min(0.1, max(0, deadline - time.monotonic()))):
                    try:
                        event = receiver.recv()
                    except EOFError:
                        break
                    if "update" in event:
                        report.update(event["update"])
                    if "request" in event:
                        report["requests"].append(event["request"])
                    if "error_type" in event:
                        report["error_type"] = event["error_type"]
                    if event.get("finished"):
                        finished = True
                elif not process.is_alive():
                    break
            process.join(timeout=max(0, deadline - time.monotonic()))
            if process.is_alive():
                report["error"] = "Превышен общий таймаут."
            elif not finished:
                report.setdefault("error", "Дочерний процесс не завершил измерение.")
            report["status"] = "complete" if (
                finished and process.exitcode == 0 and report.get("requests_successful")
                and report.get("source_unchanged") is True
                and len(report["requests"]) == args.repeat
            ) else "incomplete"
        finally:
            if process.pid is not None:
                try:
                    # После join PID уже может быть переиспользован ОС.
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
            receiver.close()
            sender.close()
    report["total_seconds"] = time.monotonic() - started
    report["temporary_directory_removed"] = True
    report["child_process_stopped"] = True


def main(argv: list[str] | None = None) -> int:
    report = {
        "schema_version": 1, "status": "incomplete",
        "measured_at": datetime.now(timezone.utc).isoformat(),
        "method": "Starlette TestClient; один сервис и один event loop для всех GET",
        "cache_conditions": "После физического копирования; файловый кэш уже прогрет. Повторные GET в том же процессе. Холодный диск не измеряется.",
        "requests": [], "sources": [], "source_unchanged": None,
    }
    args = None
    try:
        args = _parser().parse_args(argv)
        if args.report is not None:
            try:
                args.report.lstat()
            except FileNotFoundError:
                pass
            else:
                raise _ArgumentsError("Файл --report уже существует; укажите новый путь.")
        report["repeat"] = args.repeat
        report["timeout_seconds"] = args.timeout_seconds
        report["archive_count"] = len(args.incoming_archive)
        report["sources"] = [_source_record(index) for index in range(1, len(args.incoming_archive) + 1)]
        _run(args, report)
    except _ArgumentsError as error:
        report["error"] = str(error)
    except KeyboardInterrupt:
        report["error"] = "Измерение прервано."
    except Exception as error:
        report["error"] = "Измерение не завершено."
        report["error_type"] = type(error).__name__
    if args is not None and args.report is not None:
        try:
            # exclusive-create исключает перезапись исходника, hardlink и symlink.
            with args.report.open("x", encoding="utf-8") as output:
                output.write(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
        except OSError as error:
            report["status"] = "incomplete"
            report["error"] = "Не удалось создать новый файл отчёта."
            report["report_error_type"] = type(error).__name__
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["status"] == "complete" else 2


if __name__ == "__main__":
    raise SystemExit(main())
