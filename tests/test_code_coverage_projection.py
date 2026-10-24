"""Независимые JSON-снимки покрытия и точное число строк полного журнала."""

from copy import deepcopy
from dataclasses import fields, replace
from types import SimpleNamespace

import pytest

from mcp1c import code_coverage, coverage_log, dashboard_runtime, tools


@pytest.fixture
def corpus(monkeypatch):
    full_problems = tuple(
        tools.CodeProblemRow("unknown_address", None, ordinal, "адрес не доказан")
        for ordinal in range(25)
    )
    counts = dict.fromkeys((field.name for field in fields(tools.CodeCoverage)), 0)
    counts.update(
        forms_total=3,
        form_structures_full=1,
        form_structures_partial=1,
        form_structures_unread=1,
        form_modules_read=3,
        unsupported_addresses=45,
        problem_categories=(("unknown_address", 45),),
        problems_total=45,
        problems=full_problems[:20],
    )
    coverage = tools.CodeCoverage(**counts)
    source = SimpleNamespace(
        id="Пример:modules", kind="modules", sha256="a" * 64,
        loaded_at="2026-01-01T00:00:00Z", selection_version=2,
        locator_generation=3, code_version="v1",
    )
    identity = SimpleNamespace(source_id="Пример:modules", source_sha256="b" * 64, generation=3)
    loaded = SimpleNamespace(source=source, каталог=SimpleNamespace(identity=identity))

    def aggregate(actual, *, include_problem_rows):
        assert actual is loaded
        assert include_problem_rows is False
        return replace(coverage, problems=())

    monkeypatch.setattr(tools, "_code_coverage", aggregate)
    monkeypatch.setattr(
        code_coverage, "get_code_coverage",
        lambda actual, *, include_problem_rows, all_code_problems: aggregate(
            actual, include_problem_rows=include_problem_rows
        ),
    )
    for module in (tools, code_coverage):
        monkeypatch.setattr(
            module, "_iter_code_problems", lambda actual: iter(reversed(full_problems))
        )
    return coverage, loaded


def test_json_снимки_не_меняют_друг_друга_и_исходное_покрытие(corpus):
    coverage, loaded = corpus
    canonical = coverage.to_dict()
    api = dashboard_runtime._coverage_payload(coverage)
    journal = coverage_log.build_payload(loaded)
    expected = deepcopy((canonical, api, journal))

    for group in ("modules", "procedures", "form_structures", "form_modules"):
        canonical[group]["total"] = -1
    canonical["limitations"]["categories"].clear()
    canonical["problems"][0]["reason"] = "изменено"
    canonical["problems"].clear()
    assert api == expected[1]
    assert journal == expected[2]
    assert coverage.to_dict() == expected[0]

    api["modules"]["total"] = -1
    api["form_structures"]["unreadable"] = -1
    api["problem_categories"][0]["count"] = -1
    assert journal == expected[2]
    assert coverage.to_dict() == expected[0]
    assert dashboard_runtime._coverage_payload(coverage) == expected[1]

    journal["coverage"]["limitations"]["categories"].clear()
    journal["coverage"]["form_structures"]["unavailable"] = -1
    journal["problems"][0]["reason"] = "изменено"
    assert coverage.to_dict() == expected[0]
    assert dashboard_runtime._coverage_payload(coverage) == expected[1]
    assert coverage_log.build_payload(loaded) == expected[2]


def test_полный_журнал_различает_вхождения_строки_и_ограниченный_список(corpus):
    coverage, loaded = corpus
    with_rows = coverage.to_dict()
    without_rows = replace(coverage, problems=()).to_dict()
    for group in ("modules", "procedures", "form_structures", "form_modules", "limitations"):
        assert with_rows[group] == without_rows[group]
    assert len(with_rows["problems"]) == 20
    assert without_rows["problems"] == []
    assert "problem_rows_total" not in without_rows["limitations"]

    payload = coverage_log.build_payload(loaded)
    assert payload["schema_version"] == 1
    assert payload["coverage"]["limitations"]["occurrences_total"] == 45
    assert payload["coverage"]["limitations"]["problem_rows_total"] == 25
    assert [problem["ordinal"] for problem in payload["problems"]] == list(range(25))
    assert sum(line.startswith("Проблема [") for line in tools.code_coverage_lines(coverage)) == 20
    assert tools.code_coverage_lines(coverage)[-1] == "Ещё проблем: 25"
