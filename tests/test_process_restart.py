"""Self-restart включается только там, где процесс вернёт supervisor."""

from mcp1c.process_restart import RestartController
from mcp1c.registry import Registry
from mcp1c.server import build_server
from starlette.applications import Starlette
from starlette.testclient import TestClient


def test_restart_по_умолчанию_выключен(monkeypatch):
    monkeypatch.delenv("MCP1C_ALLOW_SELF_RESTART", raising=False)

    controller = RestartController.from_environment()

    assert controller.enabled is False
    assert controller.reserve() is False


def test_restart_включается_только_точным_значением_1(monkeypatch):
    monkeypatch.setenv("MCP1C_ALLOW_SELF_RESTART", "true")
    assert RestartController.from_environment().enabled is False

    monkeypatch.setenv("MCP1C_ALLOW_SELF_RESTART", "1")
    controller = RestartController.from_environment()

    assert controller.enabled is True
    assert controller.reserve() is True
    assert controller.reserve() is False


def test_health_публикует_runtime_id_текущего_процесса(tmp_path):
    controller = RestartController(
        enabled=False,
        runtime_id="synthetic-runtime",
    )
    server = build_server(Registry(tmp_path), restart=controller)
    health_route = next(
        route for route in server._custom_starlette_routes
        if route.path == "/health"
    )
    client = TestClient(Starlette(routes=[health_route]))

    response = client.get("/health")

    assert response.status_code == 200
    assert response.json()["runtime_id"] == "synthetic-runtime"
