"""Self-restart включается только там, где процесс вернёт supervisor."""

from mcp1c.process_restart import RestartController
from mcp1c.registry import Registry
from mcp1c.server import build_server
from starlette.applications import Starlette
from starlette.testclient import TestClient


def test_завершение_процесса_не_оставляет_uvicorn_в_shutdown(monkeypatch):
    exited: list[int] = []
    monkeypatch.setattr("mcp1c.process_restart.os._exit", exited.append)

    from mcp1c.process_restart import _terminate_current_process

    _terminate_current_process()

    assert exited == [0]


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


def _l5_restart_client(tmp_path, monkeypatch):
    from mcp1c import dashboard_runtime
    from mcp1c.heavy_operations import HeavyOperations
    from test_dashboard_capabilities import _client, _login
    from test_runtime_config import _L1_ADMIN

    monkeypatch.delenv('API_TOKEN', raising=False)
    monkeypatch.setenv('ADMIN_TOKEN', _L1_ADMIN)
    gate = HeavyOperations()
    # Общий настоящий admission gate: не имитируем busy результатом exception.
    monkeypatch.setattr(dashboard_runtime, 'HeavyOperations', lambda: gate)
    client, store, restart = _client(tmp_path)
    _login(client, _L1_ADMIN)
    store.save(('reference',))
    scheduled = []
    monkeypatch.setattr(restart, 'terminate_after_response', lambda: scheduled.append(True))
    return client, restart, gate, scheduled


def test_restart_во_время_загрузки_даёт_409_и_не_резервирует_завершение(tmp_path, monkeypatch):
    client, restart, gate, scheduled = _l5_restart_client(tmp_path, monkeypatch)
    lease = gate.acquire()
    assert lease is not None
    try:
        response = client.post('/api/v1/server/restart', json={})
        assert response.status_code == 409
        message = response.json()['error'].lower()
        assert 'загруз' in message
        assert 'заверш' in message
        assert restart.requested is False
        assert scheduled == []
    finally:
        lease.release()

    # Отклонённый busy-запрос не отравляет следующий restart после завершения.
    following = client.post('/api/v1/server/restart', json={})
    assert following.status_code == 202
    assert following.json()['state'] == 'restarting'
    assert restart.requested is True
    assert scheduled == [True]


def test_restart_при_свободном_heavy_gate_сохраняет_прежний_ответ(tmp_path, monkeypatch):
    client, restart, _gate, scheduled = _l5_restart_client(tmp_path, monkeypatch)
    response = client.post('/api/v1/server/restart', json={})
    assert response.status_code == 202
    assert response.json() == {
        'state': 'restarting', 'runtime_id': restart.runtime_id,
        'reasons': ['capabilities'],
    }
    assert restart.requested is True
    assert scheduled == [True]

    # Принятый restart закрывает допуск до завершения процесса.
    following = client.post('/api/v1/sources/intake/start', json={})
    assert following.status_code == 409
    assert 'перезапуск' in following.json()['error'].lower()
