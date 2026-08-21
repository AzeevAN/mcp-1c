"""CLI зеркалит инструменты кода и дожидается холодной сборки."""

import os
import subprocess
import sys
import threading
from pathlib import Path

import pytest

from mcp1c import cli, index_cache, tools
from mcp1c.registry import (
    KIND_MODULES,
    KIND_QUERY,
    STATUS_ERROR,
    STATUS_LOADING,
    STATUS_READY,
    Registry,
    Source,
)


@pytest.mark.parametrize(
    ("command", "expected"),
    [
        ("reg-search-procedures", (None, None, None, 10)),
        ("reg-get-procedure", (None, None, 0, 200)),
        ("reg-get-callers", (None, None, 20)),
    ],
)
def test_cli_команды_кода_имеют_mcp_defaults(
    command, expected, реестр_с_кодом, monkeypatch, capsys
):
    monkeypatch.setattr(cli, "_registry", lambda _args: реестр_с_кодом)
    calls = []
    if command == "reg-search-procedures":
        monkeypatch.setattr(
            tools,
            "search_procedures",
            lambda _registry, _query, config, extension, scope, limit: (
                calls.append((config, extension, scope, limit)) or "search"
            ),
        )
        argv = [command, "сложить"]
    elif command == "reg-get-procedure":
        monkeypatch.setattr(
            tools,
            "get_procedure",
            lambda _registry, _address, config, extension, start_line, lines: (
                calls.append((config, extension, start_line, lines)) or "procedure"
            ),
        )
        argv = [command, "ОбщийМодуль.ОбщийПример"]
    else:
        monkeypatch.setattr(
            tools,
            "get_callers",
            lambda _registry, _address, config, extension, limit: (
                calls.append((config, extension, limit)) or "callers"
            ),
        )
        argv = [command, "ОбщийМодуль.ОбщийПример::Сложить"]

    code = cli.main(argv)

    assert code == 0
    assert calls == [expected]
    assert capsys.readouterr().out.strip() in {"search", "procedure", "callers"}


def test_cli_передаёт_все_явные_параметры_без_переименования(
    реестр_с_кодом, monkeypatch
):
    monkeypatch.setattr(cli, "_registry", lambda _args: реестр_с_кодом)
    calls = []
    monkeypatch.setattr(
        tools,
        "search_procedures",
        lambda _registry, query, config, extension, scope, limit: (
            calls.append((query, config, extension, scope, limit)) or "ok"
        ),
    )

    code = cli.main(
        [
            "reg-search-procedures",
            "проверить остатки",
            "--config",
            "Пример",
            "--extension",
            "Доп",
            "--scope",
            "Документ.Пример",
            "--limit",
            "7",
        ]
    )

    assert code == 0
    assert calls == [
        ("проверить остатки", "Пример", "Доп", "Документ.Пример", 7)
    ]


def test_cli_ответы_совпадают_с_прямыми_tools(
    реестр_с_кодом, monkeypatch, capsys
):
    monkeypatch.setattr(cli, "_registry", lambda _args: реестр_с_кодом)
    expected = tools.get_procedure(
        реестр_с_кодом,
        "ОбщийМодуль.ОбщийПример::Сложить",
        config="Пример",
        start_line=0,
        lines=2,
    )

    code = cli.main(
        [
            "reg-get-procedure",
            "ОбщийМодуль.ОбщийПример::Сложить",
            "--config",
            "Пример",
            "--start-line",
            "0",
            "--lines",
            "2",
        ]
    )

    assert code == 0
    assert capsys.readouterr().out == expected + "\n"


def test_cli_неверная_граница_возвращает_2_с_честной_ошибкой(
    реестр_с_кодом, monkeypatch, capsys
):
    monkeypatch.setattr(cli, "_registry", lambda _args: реестр_с_кодом)

    code = cli.main(
        [
            "reg-get-callers",
            "ОбщийМодуль.ОбщийПример::Сложить",
            "--limit",
            "0",
        ]
    )

    assert code == 2
    assert "limit" in capsys.readouterr().err


def test_cli_help_перечисляет_три_команды_кода(capsys):
    with pytest.raises(SystemExit) as stopped:
        cli.main(["--help"])

    assert stopped.value.code == 0
    output = capsys.readouterr().out
    assert "reg-search-procedures" in output
    assert "reg-get-procedure" in output
    assert "reg-get-callers" in output


def test_cli_передаёт_окно_процедуры_и_limit_callers(
    реестр_с_кодом, monkeypatch
):
    monkeypatch.setattr(cli, "_registry", lambda _args: реестр_с_кодом)
    calls = []
    monkeypatch.setattr(
        tools,
        "get_procedure",
        lambda _registry, address, config, extension, start_line, lines: (
            calls.append(
                ("procedure", address, config, extension, start_line, lines)
            )
            or "ok"
        ),
    )
    monkeypatch.setattr(
        tools,
        "get_callers",
        lambda _registry, address, config, extension, limit: (
            calls.append(("callers", address, config, extension, limit))
            or "ok"
        ),
    )

    assert cli.main(
        [
            "reg-get-procedure",
            "Модуль::Имя",
            "--config",
            "Пример",
            "--extension",
            "Доп",
            "--start-line",
            "17",
            "--lines",
            "33",
        ]
    ) == 0
    assert cli.main(
        [
            "reg-get-callers",
            "Модуль::Имя",
            "--config",
            "Пример",
            "--extension",
            "Доп",
            "--limit",
            "9",
        ]
    ) == 0
    assert calls == [
        ("procedure", "Модуль::Имя", "Пример", "Доп", 17, 33),
        ("callers", "Модуль::Имя", "Пример", "Доп", 9),
    ]


def test_cli_без_кода_возвращает_честный_ответ_и_называет_недостающий_источник(
    tmp_path, monkeypatch, capsys
):
    from conftest import build_configuration, write_export
    from mcp1c.registry import Registry

    incoming = tmp_path / "incoming"
    incoming.mkdir()
    registry = Registry(tmp_path / "data")
    registry.add_configuration(
        write_export(incoming, build_configuration(name="Пример"))
    )
    monkeypatch.setattr(cli, "_registry", lambda _args: registry)

    code = cli.main(["reg-search-procedures", "сложить", "--config", "Пример"])

    assert code == 0
    answer = capsys.readouterr().out
    assert "выгрузка в файлы не загружена" in answer


def test_public_wait_ограничен_timeout_и_дожидается_завершения(tmp_path):
    registry = Registry(tmp_path / "data")
    release = threading.Event()
    thread = threading.Thread(target=release.wait, daemon=True)
    with registry._lock:
        registry._module_builds["Синтетика:modules"] = thread
    thread.start()

    try:
        assert not registry.wait_for_module_builds(timeout=0.01)
        release.set()
        assert registry.wait_for_module_builds(timeout=1.0)
    finally:
        release.set()
        thread.join(timeout=1)


def test_one_shot_cli_холодно_строит_четыре_кэша_и_тепло_поднимает_их(
    реестр_с_кодом,
):
    registry = реестр_с_кодом
    registry.save()
    for kind in registry.CACHE_KINDS[KIND_MODULES]:
        registry._cache_path("Пример:modules", kind).unlink(missing_ok=True)

    root = Path(__file__).parents[1]
    command = [
        sys.executable,
        "-m",
        "mcp1c.cli",
        "reg-search-procedures",
        "Сложить",
        "--data",
        str(registry.data_dir),
        "--config",
        "Пример",
    ]
    environment = {**os.environ, "PYTHONPATH": str(root / "src")}

    cold = subprocess.run(
        command,
        cwd=root,
        env=environment,
        text=True,
        capture_output=True,
        timeout=15,
        check=False,
    )

    assert cold.returncode == 0, cold.stderr
    assert "ОбщийМодуль.ОбщийПример::Сложить" in cold.stdout
    prefix = index_cache.safe_name("Пример:modules")
    cache = sorted(registry.cache_dir.glob(f"{prefix}.*"))
    assert len(cache) == 4
    timestamps = {path.name: path.stat().st_mtime_ns for path in cache}

    warm = subprocess.run(
        command,
        cwd=root,
        env=environment,
        text=True,
        capture_output=True,
        timeout=15,
        check=False,
    )

    assert warm.returncode == 0, warm.stderr
    assert "ОбщийМодуль.ОбщийПример::Сложить" in warm.stdout
    assert {path.name: path.stat().st_mtime_ns for path in cache} == timestamps


def test_reg_list_использует_общие_состояния_основного_кода_и_расширения(
    корень_кода, реестр_из_кода, monkeypatch, capsys
):
    registry = реестр_из_кода(корень_кода, extension="Доп")
    monkeypatch.setattr(cli, "_registry", lambda _args: registry)

    assert cli.main(["reg-list"]) == 0

    output = capsys.readouterr().out
    assert "модули     : не загружен" in output
    assert "расширение Доп: готов — модулей" in output


@pytest.mark.parametrize(
    ("status", "expected"),
    [
        (STATUS_LOADING, "строится: этап 2/4 «вызовы», обработано 3 из 11"),
        (STATUS_ERROR, "ошибка — причина синтетической ошибки"),
    ],
)
def test_reg_list_различает_сборку_и_ошибку(
    реестр_с_кодом, monkeypatch, capsys, status, expected
):
    loaded = реестр_с_кодом.resolve("Пример").modules
    with реестр_с_кодом._lock:
        loaded.готов = False
        loaded.source.status = status
        loaded.source.error = (
            "причина синтетической ошибки" if status == STATUS_ERROR else ""
        )
        loaded.этап = (2, 4)
        loaded.название_этапа = "вызовы"
        loaded.прогресс = (3, 11)
    monkeypatch.setattr(cli, "_registry", lambda _args: реестр_с_кодом)

    assert cli.main(["reg-list"]) == 0

    assert expected in capsys.readouterr().out


def test_reg_list_не_смешивает_metadata_query_и_код_при_remove_readd(
    корень_кода,
    реестр_с_кодом,
    архив_кода,
    tmp_path,
    monkeypatch,
    capsys,
):
    from conftest import build_configuration, write_export

    old_query = Source(
        id="syntax-query",
        kind=KIND_QUERY,
        status=STATUS_READY,
        items_total=11,
    )
    with реестр_с_кодом._lock:
        реестр_с_кодом.sources[old_query.id] = old_query
        реестр_с_кодом.query_source = old_query
    incoming = tmp_path / "incoming-v2"
    incoming.mkdir()
    export = write_export(
        incoming,
        build_configuration(name="Пример", version="2.0"),
    )
    archive = архив_кода(корень_кода)
    real = tools._summarize_code
    changed = False

    def summarize(loaded):
        nonlocal changed
        if not changed:
            changed = True
            реестр_с_кодом.remove("Пример")
            реестр_с_кодом.add_configuration(export)
            реестр_с_кодом.add_modules(archive, configuration="Пример")
            new_query = Source(
                id="syntax-query",
                kind=KIND_QUERY,
                status=STATUS_READY,
                items_total=22,
            )
            with реестр_с_кодом._lock:
                реестр_с_кодом.sources[new_query.id] = new_query
                реестр_с_кодом.query_source = new_query
        return real(loaded)

    monkeypatch.setattr(tools, "_summarize_code", summarize)
    monkeypatch.setattr(cli, "_registry", lambda _args: реестр_с_кодом)

    assert cli.main(["reg-list"]) == 0

    output = capsys.readouterr().out
    assert "Пример  2.0" in output
    assert "Пример  1.0" not in output
    assert "язык запросов: подключён, 22 страниц" in output


def test_reg_list_после_двух_смен_возвращает_стабильную_ошибку(
    корень_кода,
    реестр_с_кодом,
    архив_кода,
    monkeypatch,
    capsys,
):
    archive = архив_кода(корень_кода)
    real = tools._summarize_code

    def summarize(loaded):
        реестр_с_кодом.add_modules(archive, configuration="Пример")
        return real(loaded)

    monkeypatch.setattr(tools, "_summarize_code", summarize)
    monkeypatch.setattr(cli, "_registry", lambda _args: реестр_с_кодом)

    assert cli.main(["reg-list"]) == 2

    error = capsys.readouterr().err
    assert "изменились дважды" in error
    assert "/private/" not in error


def test_reg_list_после_capture_не_перечитывает_live_query_source(
    реестр_с_кодом, monkeypatch, capsys
):
    old_query = Source(
        id="syntax-query",
        kind=KIND_QUERY,
        status=STATUS_READY,
        items_total=11,
    )
    with реестр_с_кодом._lock:
        реестр_с_кодом.sources[old_query.id] = old_query
        реестр_с_кодом.query_source = old_query
    real = tools.configurations_snapshot
    captured = False

    def capture(registry):
        nonlocal captured
        result = real(registry)
        captured = True
        new_query = Source(
            id="syntax-query",
            kind=KIND_QUERY,
            status=STATUS_READY,
            items_total=99,
        )
        with registry._lock:
            registry.sources[new_query.id] = new_query
            registry.query_source = new_query
        return result

    monkeypatch.setattr(tools, "configurations_snapshot", capture)
    monkeypatch.setattr(cli, "_registry", lambda _args: реестр_с_кодом)

    assert cli.main(["reg-list"]) == 0

    output = capsys.readouterr().out
    assert captured
    assert "язык запросов: подключён, 11 страниц" in output
    assert "99 страниц" not in output
