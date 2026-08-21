"""Замер холодной и тёплой памяти финального контейнера.

Скрипт печатает один JSON-объект только с агрегатами. Имена источников,
конфигураций, локальные пути и содержимое журнала в вывод не попадают.

Холодный режим собирает финальный образ, удаляет только точные расходные файлы
четырёх модульных индексов и пересоздаёт контейнер. Тёплый режим сохраняет
кэш и останавливает/запускает тот же контейнер.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import stat
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Sequence


# Скрипт запускается прямо из tools/lab, поэтому пакет проекта не находится в
# sys.path. Имя каждого файла берём из рабочей функции, а не копируем её:
# иначе изменение safe_name разойдётся с измерителем незаметно.
PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT / "src"))
from mcp1c import index_cache  # noqa: E402


CONTAINER = "mcp1c"
MODULE_CACHE_KINDS = (
    "modules-toc",
    "modules-calls",
    "modules-forms",
    "modules-search",
)
PROBLEM_TERMS = ("critical", "error", "exception", "traceback")
LIMIT_ERROR = "timeout и poll_interval должны быть конечными числами больше нуля"
CACHE_PATH_ERROR = (
    "каталог кэша должен находиться внутри data без символических ссылок"
)


class MeasurementError(RuntimeError):
    """Безопасная для публичного вывода ошибка замера."""


@dataclass(frozen=True)
class CacheAnchor:
    path: Path
    device: int
    inode: int


@dataclass(frozen=True)
class CacheFileIdentity:
    name: str
    device: int
    inode: int
    size: int
    mtime_ns: int


@dataclass(frozen=True)
class RegistryMarker:
    file_identity: tuple[int, int, int, int]
    sources: tuple[tuple, ...]


class _JsonArgumentParser(argparse.ArgumentParser):
    """Ошибки аргументов — тот же один JSON, что и прочие отказы."""

    def error(self, _message: str) -> None:
        _print_json_error("неверные аргументы командной строки")
        raise SystemExit(2)


def _print_json_error(message: str) -> None:
    print(
        json.dumps(
            {"schema_version": 1, "completed": False, "error": message},
            ensure_ascii=False,
            sort_keys=True,
        )
    )


def _run(args: Sequence[str], *, timeout: float):
    return subprocess.run(
        list(args),
        text=True,
        capture_output=True,
        check=False,
        timeout=timeout,
    )


def _remaining(deadline: float, clock: Callable[[], float]) -> float:
    value = deadline - clock()
    if not math.isfinite(value) or value <= 0:
        raise MeasurementError("замер превысил отведённое время")
    return value


def _checked(
    run: Callable,
    args: Sequence[str],
    label: str,
    deadline: float,
    clock: Callable[[], float],
):
    try:
        result = run(list(args), timeout=_remaining(deadline, clock))
    except subprocess.TimeoutExpired as exc:
        raise MeasurementError(f"команда {label} превысила время ожидания") from exc
    except (OSError, UnicodeError) as exc:
        raise MeasurementError(f"не удалось выполнить {label}") from exc
    if result.returncode != 0:
        raise MeasurementError(f"команда {label} завершилась ошибкой")
    return result


def _validate_limits(timeout: float, poll_interval: float) -> None:
    if (
        not math.isfinite(timeout)
        or not math.isfinite(poll_interval)
        or timeout <= 0
        or poll_interval <= 0
    ):
        raise MeasurementError(LIMIT_ERROR)


def _canonical_paths(data: Path) -> tuple[Path, Path, CacheAnchor]:
    try:
        data_root = data.resolve(strict=True)
        if not data_root.is_dir():
            raise MeasurementError("data не является каталогом")
        index_dir = data_root / "index"
        cache_candidate = index_dir / "cache"
        if index_dir.is_symlink() or cache_candidate.is_symlink():
            raise MeasurementError(CACHE_PATH_ERROR)
        cache_dir = cache_candidate.resolve(strict=True)
        if cache_dir != cache_candidate or cache_dir.parent != index_dir:
            raise MeasurementError(CACHE_PATH_ERROR)
        if not cache_dir.is_dir():
            raise MeasurementError(CACHE_PATH_ERROR)
        cache_stat = cache_dir.stat(follow_symlinks=False)
        if not stat.S_ISDIR(cache_stat.st_mode):
            raise MeasurementError(CACHE_PATH_ERROR)
    except MeasurementError:
        raise
    except (OSError, RuntimeError) as exc:
        raise MeasurementError(CACHE_PATH_ERROR) from exc
    return (
        data_root,
        data_root / "registry.json",
        CacheAnchor(cache_dir, cache_stat.st_dev, cache_stat.st_ino),
    )


def _registry_payload(registry_path: Path) -> tuple[dict, tuple[int, int, int, int]]:
    try:
        with registry_path.open("rb") as stream:
            raw = stream.read()
            registry_stat = os.fstat(stream.fileno())
        payload = json.loads(raw.decode("utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError, TypeError) as exc:
        raise MeasurementError("registry.json недоступен или повреждён") from exc
    if not isinstance(payload, dict) or not isinstance(payload.get("sources"), list):
        raise MeasurementError("registry.json не содержит списка источников")
    if not all(isinstance(row, dict) for row in payload["sources"]):
        raise MeasurementError("registry.json содержит неверную запись источника")
    return payload, (
        registry_stat.st_dev,
        registry_stat.st_ino,
        registry_stat.st_size,
        registry_stat.st_mtime_ns,
    )


def _binding_id(row: dict) -> str:
    source_id = row.get("id")
    kind = row.get("kind")
    if not isinstance(source_id, str):
        raise MeasurementError("у источника кода неполная идентичность")
    if kind == "modules" and source_id.endswith(":modules"):
        return source_id[: -len(":modules")]
    if kind == "extension":
        configuration, separator, extension = source_id.partition(":ext:")
        if separator and configuration and extension:
            return configuration
    raise MeasurementError("источник кода не привязан к конфигурации")


def _code_snapshot(registry_path: Path) -> tuple[list[dict], RegistryMarker]:
    payload, file_identity = _registry_payload(registry_path)
    sources = payload["sources"]
    configurations = {
        row.get("id"): row
        for row in sources
        if row.get("kind") == "configuration" and isinstance(row.get("id"), str)
    }
    rows = [row for row in sources if row.get("kind") in ("modules", "extension")]
    markers: list[tuple] = []
    for row in rows:
        source_id = row.get("id")
        sha256 = row.get("sha256")
        loaded_at = row.get("loaded_at")
        kind = row.get("kind")
        if not all(
            isinstance(value, str) and value
            for value in (source_id, kind, sha256, loaded_at)
        ):
            raise MeasurementError("у источника кода неполная идентичность")
        binding_id = _binding_id(row)
        owner = configurations.get(binding_id)
        if owner is None:
            raise MeasurementError("у источника кода нет конфигурации-владельца")
        owner_sha = owner.get("sha256")
        owner_loaded_at = owner.get("loaded_at")
        if not all(
            isinstance(value, str) and value
            for value in (owner_sha, owner_loaded_at)
        ):
            raise MeasurementError("у конфигурации-владельца неполная идентичность")
        markers.append(
            (
                source_id,
                kind,
                sha256,
                loaded_at,
                binding_id,
                owner_sha,
                owner_loaded_at,
            )
        )
    if not markers:
        raise MeasurementError("в реестре нет источников кода")
    if len(set(markers)) != len(markers):
        raise MeasurementError("в реестре повторяется источник кода")
    return rows, RegistryMarker(file_identity, tuple(sorted(markers)))


def _code_source_signatures(registry_path: Path) -> RegistryMarker:
    """Совместимое имя тестового шва: полный маркер, не только sha256."""

    return _code_snapshot(registry_path)[1]


def _expected_cache_names(marker: RegistryMarker) -> frozenset[str]:
    names = {
        index_cache.path_for(Path("."), source[0], kind).name
        for source in marker.sources
        for kind in MODULE_CACHE_KINDS
    }
    if len(names) != len(marker.sources) * len(MODULE_CACHE_KINDS):
        raise MeasurementError("имена источников кода пересекаются после очистки")
    return frozenset(names)


def _restart_compatible_sources(
    expected: tuple[tuple, ...],
    current: tuple[tuple, ...],
) -> bool:
    """Разрешает только штатное новое runtime-поколение конфигурации."""

    if len(expected) != len(current):
        return False
    # `Registry.restore()` заново разбирает метаданные через
    # `add_configuration()`, поэтому Source конфигурации получает новый
    # loaded_at при каждом старте. Source кода восстанавливается из сохранённой
    # строки и сохраняет своё поколение. Сравниваем все поля, кроме последнего
    # owner_loaded_at: id, вид, sha и loaded_at кода, привязку и sha владельца.
    return all(
        expected_source[:-1] == current_source[:-1]
        for expected_source, current_source in zip(expected, current)
    )


def _open_cache_dir(anchor: CacheAnchor) -> int:
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    descriptor: int | None = None
    try:
        descriptor = os.open(anchor.path, flags)
        current = os.fstat(descriptor)
        canonical_path = anchor.path.resolve(strict=True)
        canonical_parent = anchor.path.parent.resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        if descriptor is not None:
            os.close(descriptor)
        raise MeasurementError("каталог кэша изменился после проверки") from exc
    if (
        (current.st_dev, current.st_ino) != (anchor.device, anchor.inode)
        or canonical_path != anchor.path
        or canonical_parent != anchor.path.parent
    ):
        os.close(descriptor)
        raise MeasurementError("каталог кэша изменился после проверки")
    return descriptor


def _cache_snapshot_from_descriptor(
    descriptor: int,
) -> tuple[CacheFileIdentity, ...]:
    files: list[CacheFileIdentity] = []
    try:
        names = os.listdir(descriptor)
        for name in names:
            if not name.rpartition(".")[2].startswith("modules-"):
                continue
            current = os.stat(name, dir_fd=descriptor, follow_symlinks=False)
            if not stat.S_ISREG(current.st_mode):
                raise MeasurementError("модульный кэш содержит недопустимый путь")
            files.append(
                CacheFileIdentity(
                    name,
                    current.st_dev,
                    current.st_ino,
                    current.st_size,
                    current.st_mtime_ns,
                )
            )
    except MeasurementError:
        raise
    except OSError as exc:
        raise MeasurementError("не удалось проверить файл модульного кэша") from exc
    return tuple(sorted(files, key=lambda item: item.name))


def _cache_snapshot(anchor: CacheAnchor) -> tuple[CacheFileIdentity, ...]:
    descriptor = _open_cache_dir(anchor)
    try:
        return _cache_snapshot_from_descriptor(descriptor)
    finally:
        os.close(descriptor)


def _verify_warm_cache_after_stop(
    anchor: CacheAnchor,
    expected: tuple[CacheFileIdentity, ...],
) -> None:
    """Повторно закрепляет и сверяет тёплый кэш до запуска контейнера."""

    try:
        descriptor = _open_cache_dir(anchor)
        try:
            current = _cache_snapshot_from_descriptor(descriptor)
            if current != expected:
                raise MeasurementError("тёплый кэш изменился после stop")
        finally:
            os.close(descriptor)
    except MeasurementError as exc:
        raise MeasurementError("тёплый кэш изменился после stop") from exc


def _cache_names(snapshot: tuple[CacheFileIdentity, ...]) -> frozenset[str]:
    return frozenset(item.name for item in snapshot)


def _delete_expected_cache(anchor: CacheAnchor, expected: frozenset[str]) -> int:
    descriptor = _open_cache_dir(anchor)
    try:
        present = set(os.listdir(descriptor))
        removable: list[str] = []
        # Сначала проверяется весь набор. Ошибка в последней цели не должна
        # оставлять первые уже удалёнными.
        for name in sorted(expected):
            if Path(name).name != name:
                raise MeasurementError(CACHE_PATH_ERROR)
            if name not in present:
                continue
            current = os.stat(name, dir_fd=descriptor, follow_symlinks=False)
            if not stat.S_ISREG(current.st_mode):
                raise MeasurementError("модульный кэш содержит недопустимый путь")
            removable.append(name)
        for name in removable:
            os.unlink(name, dir_fd=descriptor)
        return len(removable)
    except MeasurementError:
        raise
    except OSError as exc:
        raise MeasurementError("не удалось удалить расходный модульный кэш") from exc
    finally:
        os.close(descriptor)


def _inspect_state(
    run: Callable, deadline: float, clock: Callable[[], float]
) -> dict:
    result = _checked(
        run,
        (
            "docker",
            "inspect",
            "--format",
            "{{.State.Health.Status}}|{{.RestartCount}}|{{.State.OOMKilled}}",
            CONTAINER,
        ),
        "docker inspect",
        deadline,
        clock,
    )
    parts = result.stdout.strip().split("|")
    if len(parts) != 3:
        raise MeasurementError("docker inspect вернул неожиданный формат")
    try:
        restart_count = int(parts[1])
    except ValueError as exc:
        raise MeasurementError("docker inspect вернул неверный счётчик") from exc
    oom_text = parts[2].casefold()
    if oom_text not in ("true", "false"):
        raise MeasurementError("docker inspect вернул неверный признак OOM")
    return {
        "health": parts[0],
        "restart_count": restart_count,
        "oom_killed": oom_text == "true",
    }


def _readiness_observation(
    registry_path: Path,
    cache_anchor: CacheAnchor,
    expected_marker: RegistryMarker,
    health: str,
) -> bool:
    return _ready_snapshot(
        registry_path,
        cache_anchor,
        expected_marker.sources,
        health,
    ) is not None


def _ready_snapshot(
    registry_path: Path,
    cache_anchor: CacheAnchor,
    expected_sources: tuple[tuple, ...],
    health: str,
) -> tuple[RegistryMarker, tuple[CacheFileIdentity, ...]] | None:
    if health != "healthy":
        return None
    try:
        rows, marker = _code_snapshot(registry_path)
        cache_snapshot = _cache_snapshot(cache_anchor)
        expected_names = _expected_cache_names(
            RegistryMarker(marker.file_identity, expected_sources)
        )
    except MeasurementError:
        return None
    ready = (
        _restart_compatible_sources(expected_sources, marker.sources)
        and all(row.get("status") == "ready" for row in rows)
        and _cache_names(cache_snapshot) == expected_names
    )
    return (marker, cache_snapshot) if ready else None


def _wait_until_ready(
    registry_path: Path,
    cache_anchor: CacheAnchor,
    expected_marker: RegistryMarker,
    *,
    expected_cache_snapshot: tuple[CacheFileIdentity, ...] | None,
    run: Callable,
    clock: Callable[[], float],
    sleep: Callable[[float], None],
    deadline: float,
    poll_interval: float,
) -> tuple[dict, RegistryMarker, tuple[CacheFileIdentity, ...]]:
    previous: tuple[RegistryMarker, tuple[CacheFileIdentity, ...]] | None = None
    post_start_sources: tuple[tuple, ...] | None = None
    state: dict | None = None
    # registry.json заменяется атомарно, а маркер включает поколение источника
    # кода и конфигурации-владельца. Два готовых наблюдения с точным набором
    # рабочих имён кэша не принимают промежуточную или смешанную запись.
    while clock() < deadline:
        try:
            state = _inspect_state(run, deadline, clock)
        except MeasurementError:
            state = None
        observation = (
            _ready_snapshot(
                registry_path,
                cache_anchor,
                expected_marker.sources,
                state["health"],
            )
            if state is not None
            else None
        )
        if observation is not None:
            current_sources = observation[0].sources
            if post_start_sources is None:
                # Первое полное готовое поколение после запуска становится
                # единственным кандидатом. Перезапись registry.json с теми же
                # строками может сменить inode/mtime и потребовать ещё одного
                # наблюдения, но перейти на следующее поколение уже нельзя.
                post_start_sources = current_sources
            elif current_sources != post_start_sources:
                raise MeasurementError("источники изменились во время запуска")
        if (
            observation is not None
            and expected_cache_snapshot is not None
            and observation[1] != expected_cache_snapshot
        ):
            raise MeasurementError("тёплый кэш изменился во время запуска")
        if observation is not None and observation == previous:
            return state, observation[0], observation[1]
        previous = observation
        sleep(min(poll_interval, _remaining(deadline, clock)))
    raise MeasurementError("источники кода не перешли в ready за отведённое время")


def _memory_metrics(
    run: Callable, deadline: float, clock: Callable[[], float]
) -> dict:
    result = _checked(
        run,
        (
            "docker",
            "exec",
            CONTAINER,
            "sh",
            "-c",
            "cat /sys/fs/cgroup/memory.current; "
            "cat /sys/fs/cgroup/memory.peak; "
            "awk '/VmRSS|VmHWM/ {print}' /proc/1/status",
        ),
        "чтение памяти контейнера",
        deadline,
        clock,
    )
    lines = [line.strip() for line in result.stdout.splitlines() if line.strip()]
    try:
        current = int(lines[0])
        peak = int(lines[1])
        proc = {
            line.split(":", 1)[0]: int(line.split()[1])
            for line in lines[2:]
            if ":" in line and len(line.split()) >= 2
        }
        rss = proc["VmRSS"]
        hwm = proc["VmHWM"]
    except (IndexError, KeyError, ValueError) as exc:
        raise MeasurementError("контейнер вернул неожиданные поля памяти") from exc
    return {
        "cgroup_memory_current_bytes": current,
        "cgroup_memory_peak_bytes": peak,
        "pid1_vm_rss_kib": rss,
        "pid1_vm_hwm_kib": hwm,
    }


def _docker_stats_memory(
    run: Callable, deadline: float, clock: Callable[[], float]
) -> str:
    result = _checked(
        run,
        (
            "docker",
            "stats",
            "--no-stream",
            "--format",
            "{{.MemUsage}}",
            CONTAINER,
        ),
        "docker stats",
        deadline,
        clock,
    )
    usage = result.stdout.strip().split()
    if not usage:
        raise MeasurementError("docker stats не вернул память")
    return usage[0]


def _log_metrics(
    run: Callable, deadline: float, clock: Callable[[], float]
) -> dict:
    result = _checked(
        run,
        ("docker", "logs", "--since", "10m", CONTAINER),
        "docker logs",
        deadline,
        clock,
    )
    lines = result.stdout.splitlines() + result.stderr.splitlines()
    lowered = [line.casefold() for line in lines]
    terms = {
        term: sum(term in line for line in lowered)
        for term in sorted(PROBLEM_TERMS)
    }
    return {
        "log_lines": len(lines),
        "problem_lines": sum(
            any(term in line for term in PROBLEM_TERMS) for line in lowered
        ),
        "problem_terms": terms,
    }


def _final_snapshot(
    registry_path: Path,
    cache_anchor: CacheAnchor,
    expected_marker: RegistryMarker,
    expected_cache: tuple[CacheFileIdentity, ...],
    expected_names: frozenset[str],
) -> tuple[list[dict], tuple[CacheFileIdentity, ...]]:
    """Собирает последний согласованный снимок реестра и кэша."""

    rows_before, marker_before = _code_snapshot(registry_path)
    cache_snapshot = _cache_snapshot(cache_anchor)
    rows_after, marker_after = _code_snapshot(registry_path)
    if not (
        marker_before == expected_marker == marker_after
        and all(row.get("status") == "ready" for row in rows_before)
        and all(row.get("status") == "ready" for row in rows_after)
        and cache_snapshot == expected_cache
        and _cache_names(cache_snapshot) == expected_names
    ):
        raise MeasurementError("источники изменились во время замера")
    return rows_after, cache_snapshot


def measure(
    mode: str,
    data: Path,
    *,
    run: Callable = _run,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
    timeout: float = 300.0,
    poll_interval: float = 0.5,
) -> dict:
    if mode not in ("cold", "warm"):
        raise MeasurementError("режим должен быть cold или warm")
    _validate_limits(timeout, poll_interval)
    deadline = clock() + timeout
    _data_root, registry_path, cache_anchor = _canonical_paths(data)
    _rows, marker = _code_snapshot(registry_path)
    expected_names = _expected_cache_names(marker)
    expected_warm_cache: tuple[CacheFileIdentity, ...] | None = None

    if mode == "cold":
        _checked(
            run,
            ("docker", "compose", "build", CONTAINER),
            "docker compose build",
            deadline,
            clock,
        )
        _checked(
            run,
            ("docker", "compose", "stop", CONTAINER),
            "docker compose stop",
            deadline,
            clock,
        )
        deleted = _delete_expected_cache(cache_anchor, expected_names)
        cache_action = "deleted_and_rebuilt"
        start_command = (
            "docker",
            "compose",
            "up",
            "-d",
            "--force-recreate",
            CONTAINER,
        )
        start_label = "docker compose up"
    else:
        expected_warm_cache = _cache_snapshot(cache_anchor)
        if _cache_names(expected_warm_cache) != expected_names:
            raise MeasurementError("тёплый режим требует точный модульный кэш")
        deleted = 0
        cache_action = "preserved"
        _checked(
            run,
            ("docker", "stop", CONTAINER),
            "docker stop",
            deadline,
            clock,
        )
        _verify_warm_cache_after_stop(cache_anchor, expected_warm_cache)
        start_command = ("docker", "start", CONTAINER)
        start_label = "docker start"

    started = clock()
    _checked(run, start_command, start_label, deadline, clock)
    state, ready_marker, ready_cache_snapshot = _wait_until_ready(
        registry_path,
        cache_anchor,
        marker,
        expected_cache_snapshot=expected_warm_cache,
        run=run,
        clock=clock,
        sleep=sleep,
        deadline=deadline,
        poll_interval=poll_interval,
    )
    ready_seconds = round(clock() - started, 3)

    metrics = _memory_metrics(run, deadline, clock)
    stats = _docker_stats_memory(run, deadline, clock)
    logs = _log_metrics(run, deadline, clock)
    final_state = _inspect_state(run, deadline, clock)
    # Метрики собираются без замка реестра. Последняя проверка дважды читает
    # полный маркер вокруг снимка кэша: смена любого поколения внутри этой
    # последовательности отбрасывает весь смешанный результат.
    if not (
        final_state["health"] == "healthy"
    ):
        raise MeasurementError("источники изменились во время замера")
    final_rows, final_cache_snapshot = _final_snapshot(
        registry_path,
        cache_anchor,
        ready_marker,
        ready_cache_snapshot,
        expected_names,
    )

    result = {
        "schema_version": 1,
        "completed": True,
        "mode": mode,
        "cache_action": cache_action,
        "cache_message": (
            f"удалено и заново построено только расходных модульных файлов: {deleted}"
            if mode == "cold"
            else "расходный модульный кэш сохранён"
        ),
        "deleted_cache_files": deleted,
        "code_sources": len(marker.sources),
        "expected_cache_files": len(expected_names),
        "cache_files": len(final_cache_snapshot),
        "ready_seconds": ready_seconds,
        **metrics,
        "docker_stats_memory": stats,
        **state,
        **logs,
    }
    result.update(final_state)
    return result


def _arguments(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = _JsonArgumentParser(description=__doc__)
    parser.add_argument("--mode", choices=("cold", "warm"), required=True)
    parser.add_argument("--data", type=Path, required=True)
    parser.add_argument("--timeout", type=float, default=300.0)
    parser.add_argument("--poll-interval", type=float, default=0.5)
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = _arguments(argv)
    try:
        _validate_limits(args.timeout, args.poll_interval)
        result = measure(
            args.mode,
            args.data,
            run=_run,
            timeout=args.timeout,
            poll_interval=args.poll_interval,
        )
    except MeasurementError as exc:
        _print_json_error(str(exc))
        return 2
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
