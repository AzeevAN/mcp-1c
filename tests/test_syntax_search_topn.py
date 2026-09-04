"""Фильтр платформы не теряет подходящие элементы за предварительным top-N."""

import pytest

from conftest import build_syntax_registry
from mcp1c import cli
from mcp1c.dashboard_backend import _run_queries
from mcp1c.syntax_model import SyntaxItem
from mcp1c.tools import search_syntax
from test_dashboard_queries_api import _client


def items(hidden=25, available=6, *, kind="method", until=False):
    result = [
        SyntaxItem(
            id=f"obj/New{i}/Загрузить", kind=kind, name_ru="Загрузить",
            parent_ru=f"Новый{i}", description="Загрузить",
            since="8.3.1" if until else "8.3.27",
            until="8.3.3" if until else "",
        )
        for i in range(hidden)
    ]
    result.extend(
        SyntaxItem(
            id=f"obj/Old{i}/ЗагрузитьДанные", kind=kind,
            name_ru="ЗагрузитьДанные", parent_ru=f"Старый{i}",
            description="Загрузить данные", since="8.3.1",
        )
        for i in range(available)
    )
    return result


@pytest.mark.parametrize("surface", ["mcp", "cli", "http"])
def test_доступные_ниже_предварительных_окон_не_теряются(
    tmp_path, monkeypatch, capsys, surface,
):
    monkeypatch.delenv("API_TOKEN", raising=False)
    registry = build_syntax_registry(tmp_path, items(), "8.3.5.1570")
    context = registry.resolve()
    raw = context.syntax.index.search("Загрузить", limit=100)
    # Контроль исходных условий, без подмены поиска или его выдачи.
    assert len(raw) == 31
    assert all(not context.syntax_filter()(h.doc.payload) for h in raw[:25])
    expected = [h.doc.payload.address for h in raw if context.syntax_filter()(h.doc.payload)]
    assert len(expected) == 6
    if surface == "mcp":
        answer = search_syntax(registry, "Загрузить", limit=1)
        assert expected[0] in answer
        assert expected[1] not in answer
        assert "доступного ничего нет" not in answer
    elif surface == "cli":
        assert cli.main(["reg-search", "Загрузить", "--syntax", "--limit", "1", "--data", str(registry.data_dir)]) == 0
        answer = capsys.readouterr().out
        assert expected[0] in answer
        assert expected[1] not in answer
    else:
        with _client(registry, tmp_path) as client:
            response = client.post("/api/v1/queries", json={
                "config": context.name, "scope": "syntax", "phrases": ["Загрузить"],
            })
        assert response.status_code == 200
        result = response.json()["results"][0]
        assert len(result["hits"]) == 5
        assert len(result["hidden"]) <= 5
        assert [h.doc.payload.address for h in _run_queries(registry, context.name, "syntax", ["Загрузить"])[0][1]] == expected[:5]


@pytest.mark.parametrize("kind", ["method", "property"])
def test_mcp_kind_и_удалённые_методы_фильтруются_до_limit(tmp_path, kind):
    registry = build_syntax_registry(tmp_path, items(kind=kind, until=True), "8.3.5.1570")
    answer = search_syntax(registry, "Загрузить", kind=kind, limit=1)
    assert "Старый0.ЗагрузитьДанные" in answer
    assert "их уже нет" in answer
    other = "property" if kind == "method" else "method"
    assert "ничего не найдено" in search_syntax(registry, "Загрузить", kind=other)


def test_действительно_недоступное_не_становится_доступным(tmp_path):
    registry = build_syntax_registry(tmp_path, items(available=0), "8.3.5.1570")
    assert "доступного ничего нет" in search_syntax(registry, "Загрузить", limit=1)
    _, hits, hidden = _run_queries(registry, None, "syntax", ["Загрузить"])[0]
    assert hits == []
    assert len(hidden) == 5
    assert "ничего не найдено" in search_syntax(registry, "НесуществующийЗапрос")


def test_неизвестная_платформа_не_ограничивает_выдачу(tmp_path):
    registry = build_syntax_registry(tmp_path, items(), "")
    context = registry.resolve()
    expected = context.syntax.index.search("Загрузить", limit=5)
    _, hits, hidden = _run_queries(registry, None, "syntax", ["Загрузить"])[0]
    assert [h.doc.id for h in hits] == [h.doc.id for h in expected]
    assert hidden == []
    assert "Фактическая версия платформы неизвестна" in search_syntax(registry, "Загрузить")


def test_ручные_и_автоматические_запросы_сохраняют_ранжирование(tmp_path):
    methods = items()
    properties = items(kind="property")
    for item in properties:
        item.id = "property/" + item.id
    registry = build_syntax_registry(tmp_path, methods + properties, "8.3.5.1570")
    context = registry.resolve()
    queries = ["Загрузить", "Загрузить данные", "ЗагрузитьДанные", "НетТакогоИмени"]
    queries += sorted({item.full_ru for item in methods + properties})
    for query in queries:
        for kind in (None, "method", "property"):
            # Полная выдача с прежними весами — эталон, а не новый алгоритм.
            all_hits = context.syntax.index.search(query, limit=100, kinds=[kind] if kind else None)
            expected = [h.doc.payload.address for h in all_hits if context.syntax_filter()(h.doc.payload)]
            for limit in (1, 3, 10):
                response = search_syntax(registry, query, kind=kind, limit=limit)
                actual = [line.split("`")[1] for line in response.splitlines() if line.startswith("- `")]
                assert actual == expected[:limit], (query, kind, limit)
