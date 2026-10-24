"""Граф не удерживает временные таблицы построения."""

from conftest import build_configuration
from mcp1c.graph import Edge, Graph


def test_graph_не_удерживает_временные_словари_после_построения():
    config = build_configuration()
    graph = Graph(config)

    # Непустая фикстура заставляет построить оба служебных словаря.
    # Проверяем доступные после сборки рёбра, не повторяя алгоритм Graph.
    expected = Edge(
        source="Документ.РеализацияТоваровУслуг",
        target="Справочник.Контрагенты",
        kind="attribute",
        via="Контрагент",
    )
    assert graph.config is config
    assert graph.edges == [expected]
    assert graph.outgoing(expected.source) == [expected]
    assert graph.incoming(expected.target) == [expected]
    assert graph.unresolved == {}

    оставшиеся = [
        name for name in ("_object_names", "_field_owners")
        if hasattr(graph, name)
    ]
    assert оставшиеся == [], "Служебные словари нужны только во время построения"
