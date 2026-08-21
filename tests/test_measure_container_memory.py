"""Контракт воспроизводимого контейнерного замера памяти."""

from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).parents[1]
SCRIPT = ROOT / "tools" / "lab" / "measure_container_memory.py"


def _module():
    spec = importlib.util.spec_from_file_location("measure_container_memory", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


class _Result:
    def __init__(self, stdout: str = "", returncode: int = 0, stderr: str = ""):
        self.stdout = stdout
        self.stderr = stderr
        self.returncode = returncode


def _registry(
    data: Path,
    statuses: tuple[str, ...] = ("ready", "ready"),
    *,
    config_loaded_at: str = "config-generation-1",
    code_loaded_at: str = "code-generation-1",
) -> None:
    data.mkdir(parents=True, exist_ok=True)
    rows = [
        {
            "id": "Demo",
            "kind": "configuration",
            "sha256": "meta",
            "loaded_at": config_loaded_at,
        },
        {
            "id": "Demo:modules",
            "kind": "modules",
            "sha256": "base",
            "loaded_at": code_loaded_at,
            "status": statuses[0],
        },
        {
            "id": "Demo:ext:Ext",
            "kind": "extension",
            "sha256": "ext",
            "loaded_at": code_loaded_at,
            "status": statuses[1],
        },
    ]
    (data / "registry.json").write_text(
        json.dumps({"sources": rows}), encoding="utf-8"
    )


def _expected_cache_names() -> set[str]:
    kinds = ("modules-toc", "modules-calls", "modules-forms", "modules-search")
    return {
        f"{source}.{kind}"
        for source in ("Demo_modules", "Demo_ext_Ext")
        for kind in kinds
    }


def _cache(data: Path, names: set[str] | None = None) -> Path:
    cache = data / "index" / "cache"
    cache.mkdir(parents=True, exist_ok=True)
    for path in cache.iterdir():
        if any(path.name.endswith(f".{kind}") for kind in (
            "modules-toc", "modules-calls", "modules-forms", "modules-search"
        )):
            path.unlink()
    for name in names or _expected_cache_names():
        (cache / name).write_bytes(b"cache")
    return cache


def _runner(
    data: Path,
    calls: list[tuple[str, ...]],
    timeouts: list[float] | None = None,
    mutate_on: tuple[str, ...] | None = None,
    mutation=None,
):
    def run(args, *, timeout):
        command = tuple(args)
        calls.append(command)
        if timeouts is not None:
            timeouts.append(timeout)
        if command == ("docker", "compose", "up", "-d", "--force-recreate", "mcp1c"):
            _cache(data)
        if mutate_on is not None and command[: len(mutate_on)] == mutate_on:
            assert mutation is not None
            mutation()
        if command[:3] == ("docker", "inspect", "--format"):
            return _Result("healthy|0|false\n")
        if command[:3] == ("docker", "exec", "mcp1c"):
            return _Result(
                "1185505280\n1198469120\nVmHWM: 669056 kB\nVmRSS: 667008 kB\n"
            )
        if command[:3] == ("docker", "stats", "--no-stream"):
            return _Result("699.1MiB / 7.654GiB\n")
        if command[:3] == ("docker", "logs", "--since"):
            return _Result("строка один\nERROR агрегат\nстрока три\n")
        return _Result()

    return run


def test_cold_удаляет_только_расходный_кэш_и_возвращает_агрегаты(tmp_path: Path):
    module = _module()
    data = tmp_path / "data"
    _registry(data)
    cache = _cache(data)
    keep = cache / "syntax.cache"
    keep.write_bytes(b"keep")
    stale = cache / "stale-source.modules-toc"
    stale.write_bytes(b"stale")
    marker = module._code_source_signatures(data / "registry.json")
    anchor = module._canonical_paths(data)[2]
    assert module._delete_expected_cache(
        anchor, module._expected_cache_names(marker)
    ) == 8
    assert stale.read_bytes() == b"stale"
    _cache(data)
    calls: list[tuple[str, ...]] = []

    timeouts: list[float] = []
    result = module.measure(
        "cold", data, run=_runner(data, calls, timeouts), sleep=lambda _: None
    )

    assert keep.read_bytes() == b"keep"
    assert result["mode"] == "cold"
    assert result["cache_action"] == "deleted_and_rebuilt"
    assert result["deleted_cache_files"] == 8
    assert result["code_sources"] == 2
    assert result["expected_cache_files"] == result["cache_files"] == 8
    assert result["cgroup_memory_current_bytes"] == 1_185_505_280
    assert result["cgroup_memory_peak_bytes"] == 1_198_469_120
    assert result["pid1_vm_rss_kib"] == 667_008
    assert result["pid1_vm_hwm_kib"] == 669_056
    assert result["docker_stats_memory"] == "699.1MiB"
    assert result["health"] == "healthy"
    assert result["restart_count"] == 0
    assert result["oom_killed"] is False
    assert result["log_lines"] == 3
    assert result["problem_terms"] == {
        "critical": 0,
        "error": 1,
        "exception": 0,
        "traceback": 0,
    }
    assert ("docker", "compose", "build", "mcp1c") in calls
    assert ("docker", "compose", "stop", "mcp1c") in calls
    assert (
        "docker",
        "compose",
        "up",
        "-d",
        "--force-recreate",
        "mcp1c",
    ) in calls
    assert timeouts and all(value > 0 for value in timeouts)
    assert timeouts == sorted(timeouts, reverse=True)


def test_warm_сохраняет_кэш_и_перезапускает_тот_же_контейнер(tmp_path: Path):
    module = _module()
    data = tmp_path / "data"
    _registry(data)
    _cache(data)
    calls: list[tuple[str, ...]] = []

    result = module.measure(
        "warm", data, run=_runner(data, calls), sleep=lambda _: None
    )

    assert result["cache_action"] == "preserved"
    assert result["deleted_cache_files"] == 0
    assert len(list((data / "index" / "cache").glob("*.modules-*"))) == 8
    assert ("docker", "stop", "mcp1c") in calls
    assert ("docker", "start", "mcp1c") in calls
    assert not any(command[:3] == ("docker", "compose", "build") for command in calls)


def test_warm_принимает_новое_runtime_поколение_владельца_после_start(
    tmp_path: Path,
):
    module = _module()
    data = tmp_path / "data"
    _registry(data)
    _cache(data)
    now = 0.0

    def advancing_clock():
        return now

    def advancing_sleep(seconds):
        nonlocal now
        now += seconds

    result = module.measure(
        "warm",
        data,
        run=_runner(
            data,
            [],
            mutate_on=("docker", "start", "mcp1c"),
            mutation=lambda: _registry(
                data, config_loaded_at="config-runtime-generation-2"
            ),
        ),
        clock=advancing_clock,
        sleep=advancing_sleep,
        timeout=10.0,
    )

    assert result["completed"] is True
    assert result["cache_files"] == 8


def test_warm_фиксирует_первое_post_start_поколение_владельца(
    tmp_path: Path,
):
    module = _module()
    data = tmp_path / "data"
    _registry(data)
    _cache(data)
    inspect_calls = 0
    base_run = _runner(
        data,
        [],
        mutate_on=("docker", "start", "mcp1c"),
        mutation=lambda: _registry(
            data, config_loaded_at="config-runtime-generation-2"
        ),
    )

    def run(args, *, timeout):
        nonlocal inspect_calls
        result = base_run(args, timeout=timeout)
        if tuple(args[:3]) == ("docker", "inspect", "--format"):
            inspect_calls += 1
            if inspect_calls == 2:
                _registry(data, config_loaded_at="config-runtime-generation-3")
        return result

    with pytest.raises(module.MeasurementError, match="изменились во время запуска"):
        module.measure("warm", data, run=run, sleep=lambda _: None)


def test_warm_дожидается_стабильного_файла_того_же_post_start_поколения(
    tmp_path: Path,
):
    module = _module()
    data = tmp_path / "data"
    _registry(data)
    _cache(data)
    inspect_calls = 0
    base_run = _runner(
        data,
        [],
        mutate_on=("docker", "start", "mcp1c"),
        mutation=lambda: _registry(
            data, config_loaded_at="config-runtime-generation-2"
        ),
    )

    def run(args, *, timeout):
        nonlocal inspect_calls
        result = base_run(args, timeout=timeout)
        if tuple(args[:3]) == ("docker", "inspect", "--format"):
            inspect_calls += 1
            if inspect_calls == 2:
                _registry(data, config_loaded_at="config-runtime-generation-2")
        return result

    result = module.measure("warm", data, run=run, sleep=lambda _: None)

    assert result["completed"] is True
    assert inspect_calls >= 3


def test_warm_не_принимает_смену_поколения_кода_за_обычный_restart(
    tmp_path: Path,
):
    module = _module()
    data = tmp_path / "data"
    _registry(data)
    _cache(data)
    now = 0.0

    def advancing_clock():
        return now

    def advancing_sleep(seconds):
        nonlocal now
        now += seconds

    with pytest.raises(module.MeasurementError, match="не перешли в ready"):
        module.measure(
            "warm",
            data,
            run=_runner(
                data,
                [],
                mutate_on=("docker", "start", "mcp1c"),
                mutation=lambda: _registry(
                    data, code_loaded_at="code-runtime-generation-2"
                ),
            ),
            clock=advancing_clock,
            sleep=advancing_sleep,
            timeout=10.0,
        )


def test_health_без_ready_всех_источников_не_завершает_ожидание(tmp_path: Path):
    module = _module()
    data = tmp_path / "data"
    _registry(data, statuses=("ready", "building"))
    _cache(data)
    signatures = module._code_source_signatures(data / "registry.json")
    anchor = module._canonical_paths(data)[2]

    assert not module._readiness_observation(
        data / "registry.json",
        anchor,
        signatures,
        "healthy",
    )

    _registry(data)
    assert module._readiness_observation(
        data / "registry.json",
        anchor,
        signatures,
        "healthy",
    )


def test_cli_явно_требует_cold_или_warm_и_не_раскрывает_путь(tmp_path: Path):
    help_result = subprocess.run(
        [sys.executable, str(SCRIPT), "--help"],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
    )
    invalid = subprocess.run(
        [sys.executable, str(SCRIPT), "--mode", "other"],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
    )
    unavailable = subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "--mode",
            "cold",
            "--data",
            str(tmp_path / "private-data"),
        ],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
    )

    assert help_result.returncode == 0
    assert "--mode {cold,warm}" in help_result.stdout
    assert invalid.returncode == 2
    assert invalid.stderr == ""
    assert json.loads(invalid.stdout) == {
        "completed": False,
        "error": "неверные аргументы командной строки",
        "schema_version": 1,
    }
    assert unavailable.returncode == 2
    assert json.loads(unavailable.stdout) == {
        "completed": False,
        "error": "каталог кэша должен находиться внутри data без символических ссылок",
        "schema_version": 1,
    }
    assert str(tmp_path) not in unavailable.stdout + unavailable.stderr


def test_cold_отвергает_symlink_cache_до_unlink_и_не_трогает_внешний_файл(
    tmp_path: Path,
):
    module = _module()
    data = tmp_path / "data"
    _registry(data)
    external_index = tmp_path / "external-index"
    external_cache = external_index / "cache"
    external_cache.mkdir(parents=True)
    sentinel = external_cache / "Demo_modules.modules-toc"
    sentinel.write_bytes(b"external")
    (data / "index").symlink_to(external_index, target_is_directory=True)
    calls: list[tuple[str, ...]] = []

    with pytest.raises(module.MeasurementError, match="каталог кэша"):
        module.measure(
            "cold", data, run=_runner(data, calls), sleep=lambda _: None
        )

    assert sentinel.read_bytes() == b"external"
    assert calls == []
    cli = subprocess.run(
        [sys.executable, str(SCRIPT), "--mode", "warm", "--data", str(data)],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
    )
    assert cli.returncode == 2
    assert cli.stderr == ""
    assert json.loads(cli.stdout)["error"] == (
        "каталог кэша должен находиться внутри data без символических ссылок"
    )
    assert str(tmp_path) not in cli.stdout


def test_readiness_требует_точные_производственные_имена_кэшей(tmp_path: Path):
    module = _module()
    data = tmp_path / "data"
    _registry(data)
    names = _expected_cache_names()
    names.remove("Demo_modules.modules-toc")
    names.add("stale-source.modules-toc")
    _cache(data, names)
    marker = module._code_source_signatures(data / "registry.json")
    anchor = module._canonical_paths(data)[2]

    assert not module._readiness_observation(
        data / "registry.json",
        anchor,
        marker,
        "healthy",
    )


@pytest.mark.parametrize(
    "phase",
    [
        ("docker", "exec", "mcp1c"),
        ("docker", "stats", "--no-stream"),
        ("docker", "logs", "--since"),
    ],
)
def test_смена_same_sha_поколения_во_время_метрик_отменяет_ответ(
    tmp_path: Path, phase: tuple[str, ...]
):
    module = _module()
    data = tmp_path / "data"
    _registry(data)
    _cache(data)
    calls: list[tuple[str, ...]] = []

    with pytest.raises(module.MeasurementError, match="изменились во время замера"):
        module.measure(
            "warm",
            data,
            run=_runner(
                data,
                calls,
                mutate_on=phase,
                mutation=lambda: _registry(data, code_loaded_at="code-generation-2"),
            ),
            sleep=lambda _: None,
        )


def test_смена_привязки_к_перезагруженной_конфигурации_отменяет_ответ(
    tmp_path: Path,
):
    module = _module()
    data = tmp_path / "data"
    _registry(data)
    _cache(data)

    with pytest.raises(module.MeasurementError, match="изменились во время замера"):
        module.measure(
            "warm",
            data,
            run=_runner(
                data,
                [],
                mutate_on=("docker", "exec", "mcp1c"),
                mutation=lambda: _registry(
                    data, config_loaded_at="config-generation-2"
                ),
            ),
            sleep=lambda _: None,
        )


@pytest.mark.parametrize(
    "option,value",
    [
        ("--timeout", "0"),
        ("--timeout", "-1"),
        ("--timeout", "nan"),
        ("--timeout", "inf"),
        ("--poll-interval", "0"),
    ],
)
def test_cli_отвергает_неположительные_и_нечисловые_границы_одним_json(
    tmp_path: Path, option: str, value: str
):
    result = subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "--mode",
            "cold",
            "--data",
            str(tmp_path / "missing-data"),
            option,
            value,
        ],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 2
    assert result.stderr == ""
    assert len(result.stdout.splitlines()) == 1
    assert json.loads(result.stdout) == {
        "completed": False,
        "error": "timeout и poll_interval должны быть конечными числами больше нуля",
        "schema_version": 1,
    }


@pytest.mark.parametrize(
    "raw",
    [
        b"\xff\xfe",
        b"{broken",
        b"[]",
        b'{"sources": "wrong"}',
    ],
)
def test_cli_ошибка_registry_всегда_один_path_free_json(
    tmp_path: Path, raw: bytes
):
    data = tmp_path / "secret-data"
    data.mkdir()
    (data / "index" / "cache").mkdir(parents=True)
    (data / "registry.json").write_bytes(raw)

    result = subprocess.run(
        [sys.executable, str(SCRIPT), "--mode", "cold", "--data", str(data)],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 2
    assert result.stderr == ""
    assert len(result.stdout.splitlines()) == 1
    assert json.loads(result.stdout)["completed"] is False
    assert str(tmp_path) not in result.stdout


def test_timeout_subprocess_нормализуется_и_cli_печатает_один_json(
    tmp_path: Path, monkeypatch, capsys
):
    module = _module()

    def hung(_args, *, timeout):
        raise subprocess.TimeoutExpired("docker", timeout)

    with pytest.raises(module.MeasurementError, match="превысила время"):
        module._checked(hung, ("docker", "version"), "docker", 10.0, lambda: 1.0)

    def failed_measure(*_args, **_kwargs):
        raise module.MeasurementError("команда превысила время ожидания")

    monkeypatch.setattr(module, "measure", failed_measure)
    code = module.main(
        ["--mode", "cold", "--data", str(tmp_path), "--timeout", "1"]
    )
    captured = capsys.readouterr()

    assert code == 2
    assert captured.err == ""
    assert len(captured.out.splitlines()) == 1
    assert json.loads(captured.out)["completed"] is False


def test_cold_swap_cache_на_symlink_после_stop_не_касается_внешних_файлов(
    tmp_path: Path, monkeypatch, capsys
):
    module = _module()
    data = tmp_path / "data"
    _registry(data)
    cache = _cache(data)
    external = tmp_path / "external"
    external.mkdir()
    sentinels = []
    for name in _expected_cache_names():
        sentinel = external / name
        sentinel.write_bytes(b"outside")
        sentinels.append(sentinel)

    def swap():
        retired = data / "index" / "retired-cache"
        cache.rename(retired)
        cache.symlink_to(external, target_is_directory=True)

    runner = _runner(
        data,
        [],
        mutate_on=("docker", "compose", "stop"),
        mutation=swap,
    )
    with pytest.raises(module.MeasurementError, match="каталог кэша изменился"):
        module.measure("cold", data, run=runner, sleep=lambda _: None)
    assert all(path.read_bytes() == b"outside" for path in sentinels)

    monkeypatch.setattr(module, "_run", runner)
    code = module.main(["--mode", "cold", "--data", str(data)])
    captured = capsys.readouterr()
    assert code == 2
    assert captured.err == ""
    assert len(captured.out.splitlines()) == 1
    assert json.loads(captured.out)["completed"] is False
    assert str(tmp_path) not in captured.out


def test_cold_unlink_hardlink_оставляет_внешнее_содержимое(tmp_path: Path):
    module = _module()
    data = tmp_path / "data"
    _registry(data)
    cache = _cache(data)
    name = "Demo_modules.modules-toc"
    (cache / name).unlink()
    external = tmp_path / "external-cache-content"
    external.write_bytes(b"shared")
    (cache / name).hardlink_to(external)

    module.measure("cold", data, run=_runner(data, []), sleep=lambda _: None)

    assert external.read_bytes() == b"shared"


def test_warm_изменение_точного_файла_во_время_start_отменяет_замер(
    tmp_path: Path,
):
    module = _module()
    data = tmp_path / "data"
    _registry(data)
    cache = _cache(data)
    target = cache / "Demo_modules.modules-toc"

    with pytest.raises(module.MeasurementError, match="тёплый кэш изменился"):
        module.measure(
            "warm",
            data,
            run=_runner(
                data,
                [],
                mutate_on=("docker", "start", "mcp1c"),
                mutation=lambda: target.write_bytes(b"corrupted-and-rewritten"),
            ),
            sleep=lambda _: None,
        )


def test_warm_изменение_кэша_во_время_метрик_не_публикуется(tmp_path: Path):
    module = _module()
    data = tmp_path / "data"
    _registry(data)
    cache = _cache(data)
    target = cache / "Demo_ext_Ext.modules-search"

    with pytest.raises(module.MeasurementError, match="изменились во время замера"):
        module.measure(
            "warm",
            data,
            run=_runner(
                data,
                [],
                mutate_on=("docker", "stats", "--no-stream"),
                mutation=lambda: target.write_bytes(b"same-name-new-cache"),
            ),
            sleep=lambda _: None,
        )


def test_семантически_тот_же_registry_rewrite_меняет_поколение(tmp_path: Path):
    module = _module()
    data = tmp_path / "data"
    _registry(data)
    _cache(data)
    registry = data / "registry.json"

    def rewrite_identically():
        raw = registry.read_bytes()
        previous = registry.stat().st_mtime_ns
        registry.write_bytes(raw)
        current = registry.stat()
        if current.st_mtime_ns == previous:
            os.utime(registry, ns=(current.st_atime_ns, previous + 1))

    with pytest.raises(module.MeasurementError, match="изменились во время замера"):
        module.measure(
            "warm",
            data,
            run=_runner(
                data,
                [],
                mutate_on=("docker", "exec", "mcp1c"),
                mutation=rewrite_identically,
            ),
            sleep=lambda _: None,
        )


def test_final_cache_snapshot_не_может_скрыть_registry_rewrite(
    tmp_path: Path, monkeypatch
):
    module = _module()
    data = tmp_path / "data"
    _registry(data)
    _cache(data)
    registry = data / "registry.json"
    original_snapshot = module._cache_snapshot
    snapshot_calls = 0

    def snapshot_with_registry_rewrite(anchor):
        nonlocal snapshot_calls
        snapshot_calls += 1
        if snapshot_calls == 4:
            raw = registry.read_bytes()
            previous = registry.stat().st_mtime_ns
            registry.write_bytes(raw)
            current = registry.stat()
            if current.st_mtime_ns == previous:
                os.utime(registry, ns=(current.st_atime_ns, previous + 1))
        return original_snapshot(anchor)

    monkeypatch.setattr(module, "_cache_snapshot", snapshot_with_registry_rewrite)

    with pytest.raises(module.MeasurementError, match="изменились во время замера"):
        module.measure("warm", data, run=_runner(data, []), sleep=lambda _: None)


@pytest.mark.parametrize(
    "replacement", ["symlink", "directory", "regular", "missing"]
)
def test_warm_после_stop_повторно_закрепляет_cache_до_start(
    tmp_path: Path, replacement: str
):
    module = _module()
    data = tmp_path / "data"
    _registry(data)
    cache = _cache(data)
    outside = tmp_path / "outside"
    outside.mkdir()
    sentinels = []
    for name in _expected_cache_names():
        sentinel = outside / name
        sentinel.write_bytes(b"outside")
        sentinels.append(sentinel)

    def swap_after_stop():
        cache.rename(data / "index" / "retired-warm-cache")
        if replacement == "symlink":
            cache.symlink_to(outside, target_is_directory=True)
        elif replacement == "directory":
            cache.mkdir()
            for name in _expected_cache_names():
                (cache / name).write_bytes(b"replacement")
        elif replacement == "regular":
            cache.write_bytes(b"replacement")

    calls: list[tuple[str, ...]] = []
    now = 0.0

    def advancing_clock():
        nonlocal now
        now += 0.01
        return now

    with pytest.raises(module.MeasurementError, match="тёплый кэш изменился после stop"):
        module.measure(
            "warm",
            data,
            run=_runner(
                data,
                calls,
                mutate_on=("docker", "stop", "mcp1c"),
                mutation=swap_after_stop,
            ),
            clock=advancing_clock,
            sleep=lambda _: None,
            timeout=1.0,
        )

    assert ("docker", "start", "mcp1c") not in calls
    assert all(path.read_bytes() == b"outside" for path in sentinels)


def test_warm_после_stop_не_следует_symlink_родительского_каталога(
    tmp_path: Path,
):
    module = _module()
    data = tmp_path / "data"
    _registry(data)
    cache = _cache(data)
    outside = tmp_path / "outside"
    outside.mkdir()
    sentinel = outside / "sentinel"
    sentinel.write_bytes(b"outside")

    def swap_parent_after_stop():
        moved_index = outside / "index"
        (data / "index").rename(moved_index)
        (data / "index").symlink_to(moved_index, target_is_directory=True)

    calls: list[tuple[str, ...]] = []
    with pytest.raises(module.MeasurementError, match="тёплый кэш изменился после stop"):
        module.measure(
            "warm",
            data,
            run=_runner(
                data,
                calls,
                mutate_on=("docker", "stop", "mcp1c"),
                mutation=swap_parent_after_stop,
            ),
            sleep=lambda _: None,
        )

    assert ("docker", "start", "mcp1c") not in calls
    assert sentinel.read_bytes() == b"outside"
    assert cache.name == "cache"
