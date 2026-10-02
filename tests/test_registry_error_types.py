"""Семантика ошибок реестра — в типе, а не в тексте сообщения.

Потребители (HTTP-статус дашборда, скрытие имён конфигураций в MCP) выбирают
поведение по подклассу ``RegistryError``. Тексты и статусы при этом прежние:
правка формулировки больше не меняет контракт API молча.
"""

from __future__ import annotations

import pytest
from starlette.applications import Starlette
from starlette.testclient import TestClient

from conftest import build_configuration, write_export
from mcp1c import tools
from mcp1c.dashboard_runtime import DASHBOARD_ON, routes
from mcp1c.registry import (
    AmbiguousConfiguration,
    ConfigurationNotLoaded,
    Registry,
    RegistryError,
    ReloadRequired,
    SourcesChanged,
    UnknownConfiguration,
)
from mcp1c.server import _safe_registry_error_message
from test_source_modes_v1 import _registry_with_pointer


def _registry(tmp_path, *names: str) -> Registry:
    incoming = tmp_path / "incoming"
    incoming.mkdir()
    registry = Registry(tmp_path / "data")
    for name in names:
        registry.add_configuration(
            write_export(incoming, build_configuration(name=name))
        )
    return registry


def test_подклассы_остаются_registry_error():
    for kind in (
        AmbiguousConfiguration,
        ConfigurationNotLoaded,
        ReloadRequired,
        SourcesChanged,
        UnknownConfiguration,
    ):
        assert issubclass(kind, RegistryError)


def test_выбор_конфигурации_различается_типом_с_прежним_текстом(tmp_path):
    registry = _registry(tmp_path, "Альфа", "Бета")

    with pytest.raises(AmbiguousConfiguration, match="^Загружено несколько"):
        registry.resolve(None)
    with pytest.raises(UnknownConfiguration, match="^Конфигурация не загружена: Гамма"):
        registry.resolve("Гамма")
    with pytest.raises(UnknownConfiguration, match="^Конфигурация не загружена: Гамма"):
        registry.generation_view("Гамма")
    with pytest.raises(ConfigurationNotLoaded, match="^configuration_not_loaded$"):
        registry.set_platform_version("Гамма", "8.3.24.1")
    with pytest.raises(ConfigurationNotLoaded, match="^configuration_not_loaded$"):
        registry.clear_platform_version("Гамма")


def test_reload_required_различается_типом(tmp_path):
    registry = _registry_with_pointer(tmp_path, None)

    with pytest.raises(ReloadRequired, match="^reload_required$"):
        registry.resolve("Розница")


def test_mcp_скрывает_имена_по_типу_а_не_по_тексту():
    скрыто = _safe_registry_error_message(UnknownConfiguration("любой текст"))
    assert "list_configurations" in скрыто
    assert "любой текст" not in скрыто
    assert "list_configurations" in _safe_registry_error_message(
        AmbiguousConfiguration("другой текст")
    )
    # Обычная ошибка с похожим началом — не выбор конфигурации.
    assert _safe_registry_error_message(
        RegistryError("Загружено несколько файлов")
    ) == "Загружено несколько файлов"


def test_дашборд_отдаёт_409_по_типу_sources_changed(tmp_path, monkeypatch):
    registry = Registry(tmp_path / "data")

    def changed(_registry):
        raise SourcesChanged("Новая формулировка конфликта.")

    monkeypatch.setattr(tools, "sources_snapshot", changed)
    app = Starlette(routes=routes(registry, mode=DASHBOARD_ON))
    with TestClient(app, raise_server_exceptions=False) as client:
        response = client.get("/api/v1/sources")

    assert response.status_code == 409
    assert response.json() == {"error": "Новая формулировка конфликта."}


def test_повторный_конфликт_снимков_в_tools_имеет_тип_sources_changed(
    tmp_path, monkeypatch
):
    registry = Registry(tmp_path / "data")
    monkeypatch.setattr(tools, "_sources_snapshot_is_current", lambda *a: False)

    with pytest.raises(SourcesChanged, match="^Источники изменились дважды;"):
        tools.sources_snapshot(registry)
