"""Что отвечает `/health` — по нему судят о сервере, не глядя в интерфейс."""

from __future__ import annotations

from mcp1c.registry import Registry
from mcp1c.tools import health

from conftest import build_configuration, write_export, write_syntax


def каталог(tmp_path, имя: str = "incoming"):
    путь = tmp_path / имя
    путь.mkdir(parents=True, exist_ok=True)
    return путь


def test_health_показывает_только_поддерживаемые_источники(tmp_path):
    registry = Registry(tmp_path / "data")

    тело = health(registry, detailed=True)

    assert тело == {
        "status": "ok",
        "configurations_total": 0,
        "syntax_loaded": False,
        "configurations": [],
        "syntax": [],
    }


def test_справка_платформы_называет_свои_версии(tmp_path):
    registry = Registry(tmp_path / "data")
    registry.add_syntax(write_syntax(каталог(tmp_path)))

    тело = health(registry, detailed=True)

    assert тело["syntax_loaded"] is True
    assert тело["syntax"], "версии загруженных справок обязаны называться"


def test_без_токена_имена_конфигураций_не_отдаются(tmp_path):
    """Имена конфигураций — сведения о клиенте: кто у него внедрён и как
    называются доработки. Правило старое, проверяется вместе с остальным."""
    registry = Registry(tmp_path / "data")
    registry.add_configuration(write_export(каталог(tmp_path), build_configuration()))

    открытое = health(registry, detailed=False)

    assert открытое["configurations_total"] == 1
    assert "configurations" not in открытое
    assert "syntax" not in открытое
