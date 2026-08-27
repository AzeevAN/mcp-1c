"""Контракт обезличенного замера цены снимка ``get_object``."""

from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys

from tools.lab.measure_object_snapshot import measure_registry


def test_замер_сравнивает_ноль_одно_и_три_расширения(
    корень_кода, реестр_из_кода, архив_кода
):
    registry = реестр_из_кода(корень_кода)
    for name in ("ДопОдин", "ДопДва", "ДопТри"):
        registry.add_modules(
            архив_кода(корень_кода, extension=name),
            configuration="Пример",
        )

    report = measure_registry(registry, iterations=2, allocation_runs=1)

    assert report["schema"] == "object-snapshot-v1"
    assert report["completed"] is True
    assert report["corpus"]["available_extensions"] == 3
    assert [row["extensions"] for row in report["scenarios"]] == [0, 1, 3]
    for row in report["scenarios"]:
        for detail in ("fields", "full"):
            assert row[detail]["calls"] == 2
            assert row[detail]["latency"]["p50_ms"] >= 0
            assert row[detail]["traced_peak"]["p50_kib"] > 0
    serialized = json.dumps(report, ensure_ascii=False)
    for private in ("Пример", "ДопОдин", "ДопДва", "ДопТри"):
        assert private not in serialized


def test_замер_возвращает_карты_реестра_после_работы(
    корень_кода, реестр_из_кода, архив_кода
):
    registry = реестр_из_кода(корень_кода)
    for name in ("Первое", "Второе", "Третье"):
        registry.add_modules(
            архив_кода(корень_кода, extension=name),
            configuration="Пример",
        )
    before = (
        registry.configurations,
        registry.sources,
        registry.modules,
    )

    measure_registry(registry, iterations=1, allocation_runs=1)

    assert registry.configurations is before[0]
    assert registry.sources is before[1]
    assert registry.modules is before[2]


def test_cli_печатает_только_обезличенные_агрегаты(
    корень_кода, реестр_из_кода, архив_кода
):
    registry = реестр_из_кода(корень_кода)
    private_names = ("СкрытоеПервое", "СкрытоеВторое", "СкрытоеТретье")
    for name in private_names:
        registry.add_modules(
            архив_кода(корень_кода, extension=name),
            configuration="Пример",
        )
    registry.save()
    root = Path(__file__).parents[1]
    env = dict(os.environ)
    env["PYTHONPATH"] = str(root / "src")

    result = subprocess.run(
        [
            sys.executable,
            str(root / "tools/lab/measure_object_snapshot.py"),
            "--data",
            str(registry.data_dir),
            "--iterations",
            "1",
            "--allocation-runs",
            "1",
        ],
        cwd=root,
        env=env,
        text=True,
        capture_output=True,
        timeout=30,
    )

    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["completed"] is True
    assert result.stderr == ""
    assert "Пример" not in result.stdout
    assert str(registry.data_dir) not in result.stdout
    assert all(name not in result.stdout for name in private_names)
