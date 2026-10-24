"""Проверки результатов сценариев приёмки на синтетических ответах API."""

import importlib.util
import http.client
from pathlib import Path
import sys

import pytest


@pytest.mark.parametrize(
    "transition_error",
    [
        http.client.RemoteDisconnected("процесс перезапускается"),
        http.client.IncompleteRead(b"{", 20),
        http.client.BadStatusLine("оборванный заголовок"),
    ],
    ids=["disconnected", "incomplete-body", "incomplete-status"],
)
def test_restart_waits_through_interrupted_http_response(
    scenarios, monkeypatch, transition_error
):
    class Stand:
        requested_restarts = 0
        ready_checks = 0

        def __init__(self):
            self.health = iter([
                {"runtime_id": "old"},
                {"runtime_id": "old"},
                transition_error,
                {"runtime_id": "new"},
            ])

        def json(self, method, path, body=None, *, admin=False):
            if method == "POST":
                assert path == "/api/v1/server/restart" and admin
                return {"state": "restarting"}
            assert method == "GET" and path == "/health"
            value = next(self.health)
            if isinstance(value, Exception):
                raise value
            return value

        def wait_ready(self):
            self.ready_checks += 1

    monkeypatch.setattr(scenarios.time, "sleep", lambda _: None)
    stand = Stand()

    assert scenarios._restart_api(stand) == {
        "old_runtime_id": "old", "runtime_id": "new"
    }
    assert stand.requested_restarts == 1
    assert stand.ready_checks == 1


def test_restart_does_not_accept_permanently_interrupted_http(
    scenarios, monkeypatch
):
    class Stand:
        requested_restarts = 0
        health_reads = 0

        def json(self, method, path, body=None, *, admin=False):
            if method == "POST":
                return {"state": "restarting"}
            self.health_reads += 1
            if self.health_reads == 1:
                return {"runtime_id": "old"}
            raise http.client.IncompleteRead(b"{", 20)

        def wait_ready(self):
            pytest.fail("Новый процесс не подтвердил готовность")

    clock = iter([0, 1, 241])
    monkeypatch.setattr(scenarios.time, "monotonic", lambda: next(clock))
    monkeypatch.setattr(scenarios.time, "sleep", lambda _: None)
    stand = Stand()

    with pytest.raises(AssertionError, match="не появился новый runtime_id"):
        scenarios._restart_api(stand)
    assert stand.requested_restarts == 1
    assert stand.health_reads == 2


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
