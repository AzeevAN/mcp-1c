"""Контракт обезличенного lab-замера конкурентного стемминга."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent


def test_lab_сравнивает_thread_local_и_lock_в_отдельных_процессах(
    корень_кода,
):
    result = subprocess.run(
        [
            str(ROOT / ".venv/bin/python"),
            str(ROOT / "tools/lab/measure_stemmer_concurrency.py"),
            "--modules-root",
            str(корень_кода),
            "--modules-root",
            str(корень_кода),
            "--repeat",
            "1",
        ],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr
    assert result.stderr == ""
    payload = json.loads(result.stdout)
    assert payload["schema"] == "stemmer-concurrency-v1"
    assert payload["completed"] is True
    assert payload["roots_total"] == 2
    assert payload["repeat"] == 1
    assert payload["equivalent_results"] is True
    assert set(payload["strategies"]) == {"thread-local", "lock"}
    assert all(
        row["stable_results"] is True
        and row["elapsed_seconds"]["p50"] >= 0
        and row["peak_rss_mib"]["p50"] > 0
        for row in payload["strategies"].values()
    )
