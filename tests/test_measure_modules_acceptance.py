from types import SimpleNamespace

from tools.lab.measure_modules_acceptance import _call_metrics, _latency_summary


def test_call_metrics_не_смешивает_bsl_и_метаданные() -> None:
    toc = SimpleNamespace(
        модули=["ОбщийМодуль.Цель"],
        скомпилирован=lambda module: False,
        все=lambda: [
            SimpleNamespace(модуль="ОбщийМодуль.Цель", имя="Обработать")
        ]
    )
    loaded = SimpleNamespace(
        оглавление=toc,
        вызовы=SimpleNamespace(рёбер=10, разрешённых=6),
    )
    graph = SimpleNamespace(
        edges=[
            SimpleNamespace(
                kind="handler",
                target="общиймодуль.цель",
                via="обработать",
            ),
            SimpleNamespace(
                kind="method",
                target="ОбщийМодуль.Нет",
                via="Нет",
            ),
            SimpleNamespace(kind="owner", target="ОбщийМодуль.Цель", via=""),
        ]
    )

    result = _call_metrics(loaded, graph)

    assert result["bsl"] == {
        "total": 10,
        "resolved": 6,
        "resolved_percent": 60.0,
    }
    assert result["metadata"] == {
        "total": 2,
        "resolved": 1,
        "resolved_percent": 50.0,
        "unresolved_categories": {"module_missing": 1},
    }
    assert result["combined"] == {
        "total": 12,
        "resolved": 7,
        "resolved_percent": 58.33,
    }


def test_latency_summary_использует_nearest_rank() -> None:
    result = _latency_summary([1_000_000, 2_000_000, 3_000_000, 100_000_000])

    assert result == {
        "mean_ms": 26.5,
        "p50_ms": 2.0,
        "p95_ms": 100.0,
        "p99_ms": 100.0,
        "max_ms": 100.0,
    }
