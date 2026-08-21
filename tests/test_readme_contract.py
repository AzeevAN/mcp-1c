"""Проверки самосогласованности публичного README."""

import re
from pathlib import Path


def test_счётчики_pytest_в_readme_совпадают():
    текст = (Path(__file__).parents[1] / "README.md").read_text(encoding="utf-8")
    статус = re.search(r"\| Тесты \|[^\n]*?, (\d+) \|", текст)
    раздел = re.search(r"python -m pytest\s+# (\d+) тест(?:а|ов)", текст)

    assert статус is not None and раздел is not None
    assert статус.group(1) == раздел.group(1)
