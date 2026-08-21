"""Проверки самосогласованности публичного README."""

import re
from pathlib import Path


def test_счётчики_pytest_в_readme_совпадают():
    текст = (Path(__file__).parents[1] / "README.md").read_text(encoding="utf-8")
    статус = re.search(r"\| Тесты \|[^\n]*?, (\d+) \|", текст)
    раздел = re.search(r"python -m pytest\s+# (\d+) тест(?:а|ов)?", текст)

    assert статус is not None and раздел is not None
    assert статус.group(1) == раздел.group(1)


def test_список_исходников_называет_индексы_вызовов_и_форм():
    текст = (Path(__file__).parents[1] / "README.md").read_text(encoding="utf-8")
    дерево = текст.split("# 5. Как устроено", 1)[1].split("```", 2)[1]

    assert "modules_index.py" in дерево
    строка = next(line for line in дерево.splitlines() if "modules_index.py" in line)
    assert "вызов" in строка.lower()
    assert "форм" in строка.lower()
    assert "get_callers" in строка


def test_источник_кода_называет_обратный_поиск_и_события_форм():
    текст = (Path(__file__).parents[1] / "README.md").read_text(encoding="utf-8")
    раздел = текст.split("## Источники независимы", 1)[1].split("\n## ", 1)[0]
    строка = next(
        line
        for line in раздел.splitlines()
        if line.startswith("| Код конфигурации или расширения |")
    ).lower()

    assert "места вызовов" in строка
    assert "события форм" in строка
    assert "get_callers" in строка


def test_публичный_дизайн_языка_запросов_не_обещает_заглушку_индекса_кода():
    текст = (
        Path(__file__).parents[1] / "docs" / "query-language-design.md"
    ).read_text(encoding="utf-8")

    assert "Индекс модулей: не подключён" not in текст
    assert "состояние индекса кода" in текст
