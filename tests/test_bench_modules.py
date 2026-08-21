"""Стенд процедур измеряет настоящий поисковый индекс загруженного кода."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from mcp1c import bench, tools
from mcp1c import modules_index
from conftest import build_configuration, write_export
from mcp1c.registry import Registry, RegistryError
from mcp1c.search import SearchIndex


def _набор(path: Path, cases: list[dict]) -> Path:
    path.write_text(
        json.dumps(
            {"schema_version": 1, "domain": "procedures", "cases": cases},
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    return path


def _корпус(root: Path, *, suffix: str = "") -> Path:
    общий = root / "CommonModules" / f"Стенд{suffix}" / "Ext"
    общий.mkdir(parents=True)
    (общий / "Module.bsl").write_text(
        "// Проверяет остатки товаров на складе.\n"
        "Процедура ПроверитьОстатки() Экспорт\n"
        "КонецПроцедуры\n"
        "// Проверяет доступный остаток перед записью.\n"
        "Процедура ПроверитьДоступность() Экспорт\n"
        "КонецПроцедуры\n",
        encoding="utf-8",
    )
    return root


def test_стенд_считает_метрики_по_настоящему_индексу_процедур(
    tmp_path, реестр_из_кода, monkeypatch
):
    registry = реестр_из_кода(_корпус(tmp_path / "base"))
    path = _набор(
        tmp_path / "procedures.json",
        [
            {
                "query": "проверить остатки на складе",
                "expected": ["ПроверитьОстатки"],
                "note": "живой вопрос о назначении процедуры",
            },
            {
                "query": "ПроверитьОстатки",
                "expected": ["ОбщийМодуль.Стенд::ПроверитьОстатки"],
            },
        ],
    )

    def не_рендерить(*_args, **_kwargs):
        raise AssertionError("стенд не должен разбирать текст MCP-ответа")

    monkeypatch.setattr(tools, "search_procedures", не_рендерить)

    report = bench.run_procedures(registry, path, config="Пример")

    assert report.total == 2
    assert report.hit1 == 2
    assert report.at(3) == 2
    assert report.mrr == pytest.approx(2.0)
    assert report.results[0].got[0].endswith("::ПроверитьОстатки")
    assert report.results[1].separation > 0


def test_имя_и_адрес_ожидания_сравниваются_без_учёта_регистра(
    tmp_path, реестр_из_кода
):
    registry = реестр_из_кода(_корпус(tmp_path / "case"))
    path = _набор(
        tmp_path / "case.json",
        [
            {
                "query": "ПРОВЕРИТЬ ОСТАТКИ СКЛАД",
                "expected": ["проверитьостатки"],
            },
            {
                "query": "проверитьостатки",
                "expected": ["общиймодуль.стенд::проверитьостатки"],
            },
        ],
    )

    report = bench.run_procedures(registry, path, config="Пример")

    assert [result.rank for result in report.results] == [0, 0]


def test_одноимённые_процедуры_разворачиваются_во_все_точные_адреса(
    tmp_path, реестр_из_кода
):
    root = _корпус(tmp_path / "duplicates")
    второй = root / "CommonModules" / "Второй" / "Ext"
    второй.mkdir(parents=True)
    (второй / "Module.bsl").write_text(
        "// Проверяет остатки второй подсистемы.\n"
        "Процедура ПроверитьОстатки() Экспорт\n"
        "КонецПроцедуры\n",
        encoding="utf-8",
    )
    registry = реестр_из_кода(root)
    path = _набор(
        tmp_path / "duplicates.json",
        [
            {
                "query": "остатки второй подсистемы",
                "expected": ["ПроверитьОстатки"],
            }
        ],
    )

    report = bench.run_procedures(registry, path, config="Пример")

    assert report.results[0].rank == 0
    assert len(report.results[0].expected) == 2
    assert all("::ПроверитьОстатки" in address for address in report.results[0].expected)


def test_расширение_меряется_отдельным_корпусом(
    tmp_path, реестр_из_кода
):
    registry = реестр_из_кода(
        _корпус(tmp_path / "extension", suffix="Доп"),
        extension="Доп",
    )
    path = _набор(
        tmp_path / "extension.json",
        [
            {
                "query": "проверить остатки на складе",
                "expected": ["ПроверитьОстатки"],
            }
        ],
    )

    report = bench.run_procedures(
        registry,
        path,
        config="Пример",
        extension="Доп",
    )

    assert report.hit1 == 1
    assert report.results[0].got[0].startswith("ОбщийМодуль.СтендДоп::")


@pytest.mark.parametrize(
    ("payload", "message"),
    [
        (
            {"schema_version": 1, "domain": "procedures", "cases": []},
            "не содержит запросов",
        ),
        (
            {"schema_version": 1, "domain": "unknown", "cases": []},
            "domain",
        ),
        (
            {"schema_version": 1, "domain": "procedures", "cases": [{}]},
            "query",
        ),
        (
            {
                "schema_version": 1,
                "domain": "procedures",
                "cases": [{"query": "что найти", "expected": []}],
            },
            "expected",
        ),
        (
            {
                "schema_version": 1,
                "domain": "procedures",
                "cases": [
                        {"query": "что найти", "expected": [42]}
                ],
            },
            "expected",
        ),
    ],
)
def test_пустой_или_повреждённый_набор_не_становится_нулевым_замером(
    tmp_path, реестр_с_кодом, payload, message
):
    path = tmp_path / "broken.json"
    path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(ValueError, match=message):
        bench.run_procedures(реестр_с_кодом, path, config="Пример")


def test_дубли_запроса_без_учёта_регистра_отклоняются(
    tmp_path, реестр_с_кодом
):
    path = _набор(
        tmp_path / "duplicate-query.json",
        [
            {"query": "Проверить остатки", "expected": ["А"]},
            {"query": "проверить ОСТАТКИ", "expected": ["Б"]},
        ],
    )

    with pytest.raises(ValueError, match="повторяется"):
        bench.run_procedures(реестр_с_кодом, path, config="Пример")


def test_неизвестное_ожидаемое_имя_явно_ошибка_набора(
    tmp_path, реестр_с_кодом
):
    path = _набор(
        tmp_path / "missing-name.json",
        [
            {
                "query": "что найти",
                "expected": ["НетТакойПроцедуры"],
            }
        ],
    )

    with pytest.raises(ValueError, match="НетТакойПроцедуры.*не найден"):
        bench.run_procedures(реестр_с_кодом, path, config="Пример")


def test_cli_сначала_проверяет_все_наборы_и_не_печатает_частичный_отчёт(
    tmp_path, реестр_с_кодом, monkeypatch, capsys
):
    queries = tmp_path / "queries"
    queries.mkdir()
    (queries / "metadata.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "domain": "metadata",
                "cases": [
                    {
                        "query": "контрагенты",
                        "expected": ["Справочник.Контрагенты"],
                        "note": "",
                    }
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    _набор(
        queries / "procedures.json",
        [
            {
                "query": "неизвестная процедура",
                "expected": ["НетТакойПроцедуры"],
            }
        ],
    )
    save = tmp_path / "report.json"
    реестр_с_кодом.save()
    monkeypatch.setattr(bench, "QUERIES_DIR", queries)

    code = bench.main(
        [
            "--data",
            str(реестр_с_кодом.data_dir),
            "--config",
            "Пример",
            "--sets",
            "metadata,procedures",
            "--save",
            str(save),
        ]
    )

    output = capsys.readouterr().out
    assert code == 2
    assert "НетТакойПроцедуры" in output
    assert "=== metadata ===" not in output
    assert not save.exists()


def test_cli_extension_меряет_выбранный_корпус(
    tmp_path, реестр_из_кода, monkeypatch, capsys
):
    registry = реестр_из_кода(
        _корпус(tmp_path / "cli-extension", suffix="Доп"),
        extension="Доп",
    )
    registry.save()
    queries = tmp_path / "queries"
    queries.mkdir()
    _набор(
        queries / "procedures.json",
        [
            {
                "query": "проверить остатки на складе",
                "expected": ["ПроверитьОстатки"],
            }
        ],
    )
    monkeypatch.setattr(bench, "QUERIES_DIR", queries)

    code = bench.main(
        [
            "--data",
            str(registry.data_dir),
            "--config",
            "Пример",
            "--extension",
            "Доп",
            "--sets",
            "procedures",
        ]
    )

    output = capsys.readouterr().out
    assert code == 0
    assert "=== procedures ===" in output
    assert "P@1" in output


def test_cli_без_индекса_кода_завершается_ошибкой_без_нулевого_замера(
    tmp_path, monkeypatch, capsys
):
    registry = Registry(tmp_path / "data")
    incoming = tmp_path / "incoming"
    incoming.mkdir()
    registry.add_configuration(
        write_export(incoming, build_configuration(name="Пример"))
    )
    registry.save()
    queries = tmp_path / "queries"
    queries.mkdir()
    _набор(
        queries / "procedures.json",
        [{"query": "найти", "expected": ["Найти"]}],
    )
    monkeypatch.setattr(bench, "QUERIES_DIR", queries)

    code = bench.main(
        [
            "--data",
            str(registry.data_dir),
            "--config",
            "Пример",
            "--sets",
            "procedures",
        ]
    )

    output = capsys.readouterr().out
    assert code == 2
    assert "код" in output.lower()
    assert "=== procedures ===" not in output


@pytest.mark.parametrize(
    ("state", "message"),
    [
        ("loading", "строится"),
        ("error", "не построен"),
        ("incomplete", "неполон"),
    ],
)
def test_неполный_или_неготовый_индекс_не_становится_нулевым_замером(
    tmp_path, реестр_с_кодом, state, message
):
    path = _набор(
        tmp_path / f"{state}.json",
        [{"query": "Сложить", "expected": ["Сложить"]}],
    )
    loaded = реестр_с_кодом.resolve("Пример").modules
    with реестр_с_кодом._lock:
        if state == "loading":
            loaded.готов = False
            loaded.source.status = "loading"
            loaded.прогресс = (2, 10)
        elif state == "error":
            loaded.готов = False
            loaded.source.status = "error"
            loaded.source.error = "синтетическая ошибка"
        else:
            loaded.поиск = None

    with pytest.raises(RegistryError, match=message):
        bench.run_procedures(реестр_с_кодом, path, config="Пример")


def test_ошибка_индекса_не_раскрывает_локальный_путь_источника(
    tmp_path, реестр_с_кодом
):
    path = _набор(
        tmp_path / "error-path.json",
        [{"query": "Сложить", "expected": ["Сложить"]}],
    )
    loaded = реестр_с_кодом.resolve("Пример").modules
    with реестр_с_кодом._lock:
        loaded.готов = False
        loaded.source.status = "error"
        loaded.source.error = (
            "Permission denied: /private/secret/customer/Modules.zip"
        )

    with pytest.raises(RegistryError) as caught:
        bench.run_procedures(реестр_с_кодом, path, config="Пример")

    message = str(caught.value)
    assert "Индекс кода не построен" in message
    assert "/private/" not in message
    assert "Permission denied" not in message


def test_cli_не_выбирает_конфигурацию_молча(
    tmp_path, monkeypatch, capsys
):
    registry = Registry(tmp_path / "data")
    incoming = tmp_path / "incoming"
    incoming.mkdir()
    for name in ("Первая", "Вторая"):
        registry.add_configuration(
            write_export(incoming, build_configuration(name=name))
        )
    registry.save()
    queries = tmp_path / "queries"
    queries.mkdir()
    _набор(
        queries / "procedures.json",
        [{"query": "Найти", "expected": ["Найти"]}],
    )
    monkeypatch.setattr(bench, "QUERIES_DIR", queries)

    code = bench.main(
        ["--data", str(registry.data_dir), "--sets", "procedures"]
    )

    output = capsys.readouterr().out
    assert code == 2
    assert "несколько конфигураций" in output.lower()
    assert "=== procedures ===" not in output


def test_cli_не_подменяет_отсутствующее_расширение_базовым_кодом(
    tmp_path, реестр_с_кодом, monkeypatch, capsys
):
    реестр_с_кодом.save()
    queries = tmp_path / "queries"
    queries.mkdir()
    _набор(
        queries / "procedures.json",
        [{"query": "Сложить", "expected": ["Сложить"]}],
    )
    monkeypatch.setattr(bench, "QUERIES_DIR", queries)

    code = bench.main(
        [
            "--data",
            str(реестр_с_кодом.data_dir),
            "--config",
            "Пример",
            "--extension",
            "НетТакого",
            "--sets",
            "procedures",
        ]
    )

    output = capsys.readouterr().out
    assert code == 2
    assert "расширения НетТакого" in output
    assert "=== procedures ===" not in output


@pytest.mark.parametrize("failure", ["set", "baseline"])
def test_cli_файловая_ошибка_набора_или_baseline_атомарна_и_без_пути(
    tmp_path, реестр_с_кодом, monkeypatch, capsys, failure
):
    queries = tmp_path / "queries"
    queries.mkdir()
    suite_path = _набор(
        queries / "procedures.json",
        [{"query": "Сложить", "expected": ["Сложить"]}],
    )
    baseline = tmp_path / "baseline.json"
    baseline.write_text("{}", encoding="utf-8")
    save = tmp_path / "save.json"
    monkeypatch.setattr(bench, "QUERIES_DIR", queries)
    monkeypatch.setattr(
        bench,
        "build_index",
        lambda *_args, **_kwargs: (
            реестр_с_кодом,
            реестр_с_кодом.resolve("Пример"),
        ),
    )
    real_read_text = Path.read_text
    failed_path = suite_path if failure == "set" else baseline

    def read_text(path, *args, **kwargs):
        if path == failed_path:
            raise PermissionError(13, "Permission denied", "/private/secret/file")
        return real_read_text(path, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", read_text)
    args = [
        "--config",
        "Пример",
        "--sets",
        "procedures",
        "--save",
        str(save),
    ]
    if failure == "baseline":
        args += ["--baseline", str(baseline)]

    code = bench.main(args)

    output = capsys.readouterr().out
    assert code == 2
    assert "Стенд не запущен" in output
    assert "/private/" not in output
    assert "Permission denied" not in output
    assert "=== procedures ===" not in output
    assert not save.exists()


def test_cli_ошибка_save_не_печатает_отчёт_и_не_оставляет_temp(
    tmp_path, реестр_с_кодом, monkeypatch, capsys
):
    queries = tmp_path / "queries"
    queries.mkdir()
    _набор(
        queries / "procedures.json",
        [{"query": "Сложить", "expected": ["Сложить"]}],
    )
    save = tmp_path / "private-report.json"
    monkeypatch.setattr(bench, "QUERIES_DIR", queries)
    monkeypatch.setattr(
        bench,
        "build_index",
        lambda *_args, **_kwargs: (
            реестр_с_кодом,
            реестр_с_кодом.resolve("Пример"),
        ),
    )
    real_replace = Path.replace

    def replace(path, target):
        if Path(target) == save:
            raise PermissionError(
                13, "Permission denied", "/private/secret/report.json"
            )
        return real_replace(path, target)

    monkeypatch.setattr(Path, "replace", replace)

    code = bench.main(
        [
            "--config",
            "Пример",
            "--sets",
            "procedures",
            "--save",
            str(save),
        ]
    )

    output = capsys.readouterr().out
    assert code == 2
    assert "Стенд не запущен" in output
    assert "/private/" not in output
    assert "Permission denied" not in output
    assert "=== procedures ===" not in output
    assert "прогон записан" not in output
    assert not save.exists()
    assert list(tmp_path.glob(".*.tmp")) == []


def test_cli_check_notes_возвращает_1_при_смене_машинного_места(
    tmp_path, реестр_с_кодом, monkeypatch, capsys
):
    queries = tmp_path / "queries"
    queries.mkdir()
    _набор(
        queries / "procedures.json",
        [
            {
                "query": "Сложить",
                "expected": ["Сложить"],
                "expected_rank": 6,
            }
        ],
    )
    monkeypatch.setattr(bench, "QUERIES_DIR", queries)
    monkeypatch.setattr(
        bench,
        "build_index",
        lambda *_args, **_kwargs: (
            реестр_с_кодом,
            реестр_с_кодом.resolve("Пример"),
        ),
    )

    code = bench.main(
        ["--config", "Пример", "--sets", "procedures", "--check-notes"]
    )

    output = capsys.readouterr().out
    assert code == 1
    assert "ожидалось 7" in output
    assert "место 1" in output


def test_expected_разрешается_без_полного_обхода_search_docs_и_при_retry(
    tmp_path, реестр_с_кодом, monkeypatch
):
    path = _набор(
        tmp_path / "bounded.json",
        [{"query": "Сложить", "expected": ["Сложить"]}],
    )
    loaded = реестр_с_кодом.resolve("Пример").modules

    class ЗапрещёнПолныйОбход(dict):
        def __len__(self):
            # Имитирует большой рабочий индекс без выделения памяти в тесте.
            return 100_000

        def __iter__(self):
            raise AssertionError("полный обход search docs запрещён")

        def keys(self):
            raise AssertionError("полный обход search docs запрещён")

        def items(self):
            raise AssertionError("полный обход search docs запрещён")

        def values(self):
            raise AssertionError("полный обход search docs запрещён")

    loaded.поиск.docs = ЗапрещёнПолныйОбход(loaded.поиск.docs)
    проверки = iter((False, True))
    monkeypatch.setattr(
        bench,
        "_procedure_snapshot_is_current",
        lambda *_args: next(проверки),
    )

    report = bench.run_procedures(реестр_с_кодом, path, config="Пример")

    assert report.hit1 == 1


def test_tracked_procedure_set_работает_на_синтетическом_export_без_чтения_тел(
    tmp_path, реестр_из_кода, monkeypatch
):
    suite = bench.load_curated("modules-procedures")
    names = [expected for case in suite.cases for expected in case.expected]
    root = tmp_path / "tracked"
    module = root / "CommonModules" / "ПубличныйСтенд" / "Ext"
    module.mkdir(parents=True)
    module.joinpath("Module.bsl").write_text(
        "\n".join(
            f"// {case.query}.\nПроцедура {case.expected[0]}() Экспорт\n"
            "КонецПроцедуры"
            for case in suite.cases
        ),
        encoding="utf-8",
    )
    registry = реестр_из_кода(root)

    def body_read_forbidden(*_args, **_kwargs):
        raise AssertionError("стенд не читает тела и сигнатуры")

    monkeypatch.setattr(modules_index, "read_bsl", body_read_forbidden)

    report = bench.run_procedures(registry, suite, config="Пример")

    assert report.total == 3
    assert all(result.rank is None or 0 <= result.rank < 10 for result in report.results)
    assert all("::" not in name and "/" not in name and "\\" not in name for name in names)
    assert all("." not in name for name in names)
    assert [case.expected_miss for case in suite.cases] == [True, True, False]
    assert [case.expected_rank for case in suite.cases] == [None, None, 6]


@pytest.mark.parametrize("extension", [None, "Доп"], ids=["modules", "extension"])
def test_смена_поколения_повторяет_весь_набор_на_одном_поисковом_индексе(
    tmp_path,
    реестр_из_кода,
    архив_кода,
    monkeypatch,
    extension,
):
    registry = реестр_из_кода(
        _корпус(tmp_path / "generation-old", suffix="Доп" if extension else ""),
        extension=extension,
    )
    path = _набор(
        tmp_path / "generation.json",
        [
            {"query": "ПроверитьОстатки", "expected": ["ПроверитьОстатки"]},
            {
                "query": "ПроверитьДоступность",
                "expected": ["ПроверитьДоступность"],
            },
        ],
    )
    новый_корень = _корпус(
        tmp_path / "generation-new",
        suffix="Доп" if extension else "",
    )
    новый = архив_кода(новый_корень, extension=extension)
    real_search = SearchIndex.search
    calls: list[tuple[int, str]] = []

    def search(index, query, *args, **kwargs):
        calls.append((id(index), query))
        if len(calls) == 1:
            registry.add_modules(новый, configuration="Пример")
        return real_search(index, query, *args, **kwargs)

    monkeypatch.setattr(SearchIndex, "search", search)

    report = bench.run_procedures(
        registry,
        path,
        config="Пример",
        extension=extension,
    )

    assert report.hit1 == 2
    assert [query for _, query in calls] == [
        "ПроверитьОстатки",
        "ПроверитьДоступность",
        "ПроверитьОстатки",
        "ПроверитьДоступность",
    ]
    assert len({index_id for index_id, _ in calls}) == 2


def test_смена_identity_конфигурации_повторяет_набор(tmp_path, реестр_с_кодом, monkeypatch):
    path = _набор(
        tmp_path / "configuration-race.json",
        [{"query": "Сложить", "expected": ["Сложить"]}],
    )
    replacement = tmp_path / "replacement"
    replacement.mkdir()
    export = write_export(
        replacement,
        build_configuration(name="Пример", version="2.0"),
    )
    real_search = SearchIndex.search
    calls = 0

    def search(index, query, *args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            реестр_с_кодом.add_configuration(export)
        return real_search(index, query, *args, **kwargs)

    monkeypatch.setattr(SearchIndex, "search", search)

    report = bench.run_procedures(реестр_с_кодом, path, config="Пример")

    assert report.hit1 == 1
    assert calls == 2


def test_remove_во_время_набора_не_возвращает_устаревший_отчёт(
    tmp_path, реестр_с_кодом, monkeypatch
):
    path = _набор(
        tmp_path / "remove-race.json",
        [{"query": "Сложить", "expected": ["Сложить"]}],
    )
    real_search = SearchIndex.search
    removed = False

    def search(index, query, *args, **kwargs):
        nonlocal removed
        if not removed:
            removed = True
            реестр_с_кодом.remove("Пример")
        return real_search(index, query, *args, **kwargs)

    monkeypatch.setattr(SearchIndex, "search", search)

    with pytest.raises(RegistryError) as caught:
        bench.run_procedures(реестр_с_кодом, path, config="Пример")

    message = str(caught.value)
    assert "не загруж" in message.lower()
    assert "/private/" not in message


@pytest.mark.parametrize("extension", [None, "Доп"], ids=["modules", "extension"])
def test_две_смены_поколения_дают_стабильную_ошибку_без_частичного_отчёта(
    tmp_path,
    реестр_из_кода,
    архив_кода,
    monkeypatch,
    extension,
):
    suffix = "Доп" if extension else ""
    registry = реестр_из_кода(
        _корпус(tmp_path / "twice-old", suffix=suffix),
        extension=extension,
    )
    path = _набор(
        tmp_path / "twice.json",
        [
            {"query": "ПроверитьОстатки", "expected": ["ПроверитьОстатки"]},
            {
                "query": "ПроверитьДоступность",
                "expected": ["ПроверитьДоступность"],
            },
        ],
    )
    archives = [
        архив_кода(_корпус(tmp_path / f"twice-{n}", suffix=suffix), extension=extension)
        for n in (1, 2)
    ]
    real_search = SearchIndex.search
    calls = 0

    def search(index, query, *args, **kwargs):
        nonlocal calls
        calls += 1
        if calls in (1, 3):
            registry.add_modules(archives[0 if calls == 1 else 1], configuration="Пример")
        return real_search(index, query, *args, **kwargs)

    monkeypatch.setattr(SearchIndex, "search", search)

    with pytest.raises(RegistryError) as caught:
        bench.run_procedures(
            registry,
            path,
            config="Пример",
            extension=extension,
        )

    assert "изменился.*дважды" not in str(caught.value)
    assert "дважды" in str(caught.value)
    assert "/private/" not in str(caught.value)
    assert calls == 4
