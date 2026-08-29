"""Проверки самосогласованности публичных README и CONTRIBUTING."""

import os
import re
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).parents[1]


def test_счётчики_pytest_в_публичных_документах_совпадают_с_collection():
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    contributing = (ROOT / "CONTRIBUTING.md").read_text(encoding="utf-8")
    статус = re.search(r"\| Тесты \|[^\n]*?, (\d+) \|", readme)
    раздел = re.search(r"python -m pytest\s+# (\d+) тест(?:а|ов)?", readme)
    вклад = re.search(
        r"python -m pytest\s+# (\d+) тест(?:а|ов)?", contributing
    )

    assert статус is not None and раздел is not None and вклад is not None
    assert статус.group(1) == раздел.group(1) == вклад.group(1)

    env = {**os.environ, "PYTEST_ADDOPTS": ""}
    result = subprocess.run(
        [sys.executable, "-m", "pytest", "--collect-only"],
        cwd=ROOT,
        env=env,
        check=True,
        capture_output=True,
        text=True,
    )
    collected = re.search(r"(\d+) tests collected", result.stdout)

    assert collected is not None
    assert int(статус.group(1)) == int(collected.group(1))


def test_список_исходников_называет_индексы_вызовов_и_форм():
    текст = (ROOT / "docs" / "architecture.md").read_text(encoding="utf-8")
    дерево = текст.split("## Основные модули", 1)[1].split("```", 2)[1]

    assert "modules_index.py" in дерево
    строка = next(line for line in дерево.splitlines() if "modules_index.py" in line)
    assert "вызов" in строка.lower()
    assert "форм" in строка.lower()
    assert "get_callers" in строка


def test_источник_кода_называет_обратный_поиск_и_события_форм():
    текст = (ROOT / "docs" / "tools.md").read_text(encoding="utf-8")
    раздел = текст.split("## Источники независимы", 1)[1].split("\n## ", 1)[0]
    строки = [
        line.lower()
        for line in раздел.splitlines()
        if line.startswith(("| Код конфигурации |", "| Код расширения |"))
    ]

    assert len(строки) == 2
    assert "места вызовов" in строки[0]
    assert "события форм" in строки[0]
    assert "get_callers" in строки[0]
    assert "места вызовов" in строки[1]


def test_публичный_дизайн_языка_запросов_не_обещает_заглушку_индекса_кода():
    текст = (
        Path(__file__).parents[1] / "docs" / "query-language-design.md"
    ).read_text(encoding="utf-8")

    assert "Индекс модулей: не подключён" not in текст
    assert "состояние индекса кода" in текст


def test_readme_и_mcp_согласованы_о_версиях_языка_запросов():
    текст = (ROOT / "docs" / "tools.md").read_text(encoding="utf-8")
    контракт = (
        "В самом `shquery_ru.hbk` версии появления не записаны; известные границы\n"
        "заданы курируемой таблицей и при заданном `config` фильтруются по версии\n"
        "платформы конфигурации."
    )

    assert контракт in текст


def test_readme_описывает_все_ключи_и_домены_стенда():
    текст = (ROOT / "docs" / "operations.md").read_text(encoding="utf-8")
    раздел = текст.split("## Стенд `mcp1c.bench`", 1)[1]
    раздел = раздел.split("\n## ", 1)[0]

    for key in (
        "--data",
        "--sets",
        "--auto",
        "--config",
        "--extension",
        "--limit",
        "--save",
        "--baseline",
        "--check-notes",
    ):
        assert key in раздел
    assert "modules-procedures" in раздел
    assert all(domain in раздел for domain in ("syntax", "metadata", "procedures"))


def test_readme_фиксирует_воспроизводимый_baseline_шести_процедурных_запросов():
    текст = (ROOT / "docs" / "architecture.md").read_text(encoding="utf-8")
    раздел = текст.split("### Качество поиска", 1)[1].split("\n## ", 1)[0]
    строка = next(
        line for line in раздел.splitlines() if line.startswith("| Процедуры модулей |")
    )

    assert "| 6 | 0% | 0% | 0% | 16,7% | 0,024 | 0% |" in строка
    assert "2026-08-29" in раздел
    assert "--sets modules-procedures" in раздел
    assert "три ручных" in раздел.lower()
    assert re.search(
        r"вся таблица\s+снята.*точной командой ниже",
        раздел.lower(),
        re.DOTALL,
    )
    assert "порог приёмки" in раздел.lower()
