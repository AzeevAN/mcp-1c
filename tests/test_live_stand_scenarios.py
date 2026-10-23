"""Проверки результатов сценариев приёмки на синтетических ответах API."""

import importlib.util
from pathlib import Path
import sys

import pytest


@pytest.fixture(scope="module")
def scenarios():
    path = Path(__file__).resolve().parents[1] / "tools/lab/live_stand_scenarios.py"
    spec = importlib.util.spec_from_file_location("live_stand_scenarios_test", path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize(
    ("before", "after", "accepted"),
    [
        ({"syntax-8.5.1.1529"},
         [{"id": "syntax-8.5.1.1529"}, {"id": "syntax-5.1.1529"}], False),
        (set(), [{"id": "syntax-8.5.1.1529"}], True),
        ({"syntax-8.5.1.1529"}, [{"id": "syntax-8.5.1.1529"}], True),
        (set(), [{"id": "syntax-5.1.1529"}], False),
        (set(), [{"id": "syntax-8.5.1.1529"}, {"id": "syntax-5.1.1529"}], False),
    ],
    ids=[
        "existing-expected-does-not-hide-new-wrong-id",
        "new-expected-id",
        "reused-expected-id",
        "missing-expected-id",
        "new-expected-and-unexpected-id",
    ],
)
def test_syntax_import_accepts_only_expected_addition(scenarios, before, after, accepted):
    if accepted:
        assert scenarios.check_syntax_import(before, after, "syntax-8.5.1.1529") is None
    else:
        with pytest.raises(AssertionError):
            scenarios.check_syntax_import(before, after, "syntax-8.5.1.1529")


@pytest.mark.parametrize(
    ("receipt", "accepted"),
    [
        ("Восстановлено источников: 2\n"
         "добавлено: syntax-8.5.1.1529  (syntax, платформа 8.5.1.1529, элементов 42)\n",
         True),
        ("добавлено: syntax-5.1.1529  (syntax, платформа 8.5.1.1529, элементов 42)\n",
         False),
        ("добавлено: syntax-8.5.1.1529  (syntax, платформа 5.1.1529, элементов 42)\n",
         False),
        ("Восстановлено источников: 2\nЗагрузка завершена.\n", False),
    ],
    ids=[
        "correct-receipt-after-startup-lines",
        "receipt-wrong-id-already-existed",
        "receipt-wrong-platform",
        "receipt-without-added-line",
    ],
)
def test_syntax_import_validates_cli_receipt_even_without_new_ids(
    scenarios, receipt, accepted
):
    before = {"syntax-8.5.1.1529", "syntax-5.1.1529"}
    after = [{"id": "syntax-8.5.1.1529"}, {"id": "syntax-5.1.1529"}]

    if accepted:
        assert scenarios.check_syntax_import(
            before, after, "syntax-8.5.1.1529", receipt=receipt
        ) is None
    else:
        with pytest.raises(AssertionError):
            scenarios.check_syntax_import(
                before, after, "syntax-8.5.1.1529", receipt=receipt
            )
