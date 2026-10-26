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
    def body(tracestate):
        return {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": "2025-06-18",
                "capabilities": {},
                "clientInfo": {"name": "synthetic-tracestate-test", "version": "1"},
                "_meta": {
                    "traceparent": "00-0af7651916cd43dd8448eb211c80319c-b7ad6b7169203331-01",
                    "tracestate": tracestate,
                },
            },
        }

    headers = {
        "accept": "application/json, text/event-stream",
        "content-type": "application/json",
    }

    def timed(tracestate):
        started = perf_counter()
        response = client.post("/mcp", json=body(tracestate), headers=headers)
        assert response.status_code == 200
        return perf_counter() - started

    # Запуск транспорта и первый запрос не входят в сравнение. Контроль —
    # тот же initialize с коротким tracestate на той же машине: скорость
    # машины сокращается. До фикса 60 000 пробелов давали ≈3,7 с против
    # миллисекунд у короткого; после — столько же, сколько короткий.
    with TestClient(app, base_url="http://localhost") as client:
        timed("a=b")
        control = min(timed("a=b") for _ in range(3))
        elapsed = timed("a=b" + " " * 60_000 + "x")

    assert elapsed < max(20 * control, 0.1), (
        f"initialize с длинным tracestate: {elapsed:.3f} с против {control:.3f} с у короткого"
    )
