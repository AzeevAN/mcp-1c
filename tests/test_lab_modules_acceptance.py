"""Обезличенный воспроизводимый замер финальной приёмки кода."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path


КОРЕНЬ_ПРОЕКТА = Path(__file__).resolve().parents[1]
СКРИПТ = КОРЕНЬ_ПРОЕКТА / "tools/lab/measure_modules_acceptance.py"


def test_lab_замер_печатает_только_агрегаты(реестр_с_кодом):
    env = dict(os.environ)
    env["PYTHONPATH"] = str(КОРЕНЬ_ПРОЕКТА / "src")
    result = subprocess.run(
        [sys.executable, str(СКРИПТ), "--data", str(реестр_с_кодом.data_dir)],
        cwd=КОРЕНЬ_ПРОЕКТА,
        env=env,
        text=True,
        capture_output=True,
        timeout=30,
    )

    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    assert payload["schema"] == "modules-acceptance-v2"
    assert payload["completed"] is True
    assert payload["sources_total"] == 1
    assert payload["sources"][0]["source_kind"] == "modules"
    assert payload["sources"][0]["locator_kinds"]["tree_bsl"] == 3
    assert payload["sources"][0]["forms"]["total"] == 1
    assert payload["call_resolution"]["bsl"]["total"] >= 0
    assert payload["call_resolution"]["metadata"]["total"] >= 0
    assert payload["flat_form_module_read"]["readable_containers"] == 0
    assert payload["flat_form_module_read"]["failed_containers"] == 0
    assert payload["elapsed_seconds"] >= 0
    assert payload["peak_rss_mib"] > 0
    assert "Пример" not in result.stdout
    assert str(реестр_с_кодом.data_dir) not in result.stdout
    assert result.stderr == ""


def test_lab_ошибка_тоже_остаётся_одним_обезличенным_json(tmp_path):
    env = dict(os.environ)
    env["PYTHONPATH"] = str(КОРЕНЬ_ПРОЕКТА / "src")
    missing = tmp_path / "нет-данных"
    result = subprocess.run(
        [sys.executable, str(СКРИПТ), "--data", str(missing)],
        cwd=КОРЕНЬ_ПРОЕКТА,
        env=env,
        text=True,
        capture_output=True,
        timeout=30,
    )

    assert result.returncode == 2
    assert json.loads(result.stdout) == {
        "schema": "modules-acceptance-v2",
        "completed": False,
        "error": "кодовые источники не найдены",
    }
    assert str(missing) not in result.stdout
    assert result.stderr == ""


def test_lab_повреждённое_локальное_состояние_не_печатает_traceback(tmp_path):
    env = dict(os.environ)
    env["PYTHONPATH"] = str(КОРЕНЬ_ПРОЕКТА / "src")
    data = tmp_path / "data"
    data.mkdir()
    (data / "dictionary.json").write_text("[]", encoding="utf-8")

    result = subprocess.run(
        [sys.executable, str(СКРИПТ), "--data", str(data)],
        cwd=КОРЕНЬ_ПРОЕКТА,
        env=env,
        text=True,
        capture_output=True,
        timeout=30,
    )

    assert result.returncode == 2
    assert json.loads(result.stdout) == {
        "schema": "modules-acceptance-v2",
        "completed": False,
        "error": "замер не завершён",
    }
    assert str(data) not in result.stdout
    assert result.stderr == ""
