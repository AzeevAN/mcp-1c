"""Регрессии обработки недоверенного tracestate на настоящем MCP-транспорте."""

from time import perf_counter

from mcp.server._otel import OpenTelemetryMiddleware
from mcp.server.transport_security import TransportSecuritySettings
import pytest
from starlette.testclient import TestClient

from mcp1c.reference_provider import ReferenceService
from mcp1c.server import build_server


@pytest.fixture
def mcp_server(реестр_с_кодом, tmp_path):
    # Синтетический реестр и отсутствующая справка исключают чтение данных владельца.
    reference = ReferenceService.discover(
        tmp_path / "reference-data", embedded_path=tmp_path / "missing-reference.bundle"
    )
    return build_server(реестр_с_кодом, reference=reference, enabled_capabilities=())


def test_otel_middleware_отключён(mcp_server):
    assert not any(
        isinstance(middleware, OpenTelemetryMiddleware)
        for middleware in mcp_server.middleware
    )


def test_длинный_tracestate_не_блокирует(mcp_server):
    app = mcp_server.streamable_http_app(
        transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False)
    )
    body = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "initialize",
        "params": {
            "protocolVersion": "2025-06-18",
            "capabilities": {},
            "clientInfo": {"name": "synthetic-tracestate-test", "version": "1"},
            "_meta": {
                "traceparent": "00-0af7651916cd43dd8448eb211c80319c-b7ad6b7169203331-01",
                "tracestate": "a=b" + " " * 60_000 + "x",
            },
        },
    }
    headers = {
        "accept": "application/json, text/event-stream",
        "content-type": "application/json",
    }

    # Запуск транспорта не входит в бюджет обработки одного входящего запроса.
    with TestClient(app, base_url="http://localhost") as client:
        started = perf_counter()
        response = client.post("/mcp", json=body, headers=headers)
        elapsed = perf_counter() - started

    assert response.status_code == 200
    assert elapsed < 0.5, f"initialize с длинным tracestate обрабатывался {elapsed:.3f} с"
