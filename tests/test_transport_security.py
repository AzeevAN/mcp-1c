"""Сетевая граница HTTP-сервера: Host/Origin, открытое чтение, доверие proxy.

Проверки DNS rebinding SDK закрывают только `/mcp`, а дашборд, `/health` и
служебные маршруты живут рядом в том же приложении. Поэтому граница проверяется
снаружи всего приложения — так, как его запускает `_run_streamable_http`.
"""

from __future__ import annotations

import sys
from types import SimpleNamespace

import pytest
from starlette.applications import Starlette
from starlette.responses import PlainTextResponse
from starlette.routing import Route
from starlette.testclient import TestClient

from mcp1c import server as server_module
from mcp1c.runtime_config import (
    HostConfigurationError,
    allowed_hosts,
    forwarded_allow_ips,
    host_check_required,
    is_loopback_bind,
)


def _app() -> Starlette:
    async def ok(request):
        return PlainTextResponse("данные")

    return Starlette(
        routes=[
            Route("/api/v1/dashboard/bootstrap", ok, methods=["GET"]),
            Route("/api/v1/queries", ok, methods=["POST"]),
            Route("/health", ok, methods=["GET"]),
        ]
    )


def _client(hosts, base_url="http://127.0.0.1:8000") -> TestClient:
    return TestClient(server_module.trusted_host_guard(_app(), hosts), base_url=base_url)


def test_rebinding_имя_не_читает_api_дашборда():
    client = _client(allowed_hosts({}), base_url="http://attacker.example:8000")

    response = client.get("/api/v1/dashboard/bootstrap")

    assert response.status_code == 421
    assert "данные" not in response.text
    assert "MCP1C_ALLOWED_HOSTS" in response.text


@pytest.mark.parametrize(
    "host",
    ["127.0.0.1:8000", "localhost:5001", "[::1]:8000", "localhost", "LOCALHOST."],
)
def test_loopback_host_пропускается_с_любым_портом(host):
    # TestClient не разбирает IPv6 в base_url, поэтому Host задаётся заголовком.
    client = _client(allowed_hosts({}))

    response = client.get("/api/v1/dashboard/bootstrap", headers={"host": host})

    assert response.status_code == 200


def test_публичный_адрес_и_явный_список_расширяют_допуск():
    hosts = allowed_hosts(
        {
            "MCP1C_PUBLIC_BASE_URL": "https://mcp.example.com",
            "MCP1C_ALLOWED_HOSTS": "192.168.0.5, host.docker.internal:5001",
        }
    )

    assert _client(hosts, "https://mcp.example.com").get("/health").status_code == 200
    assert _client(hosts, "http://192.168.0.5:80").get("/health").status_code == 200
    assert (
        _client(hosts, "http://host.docker.internal:5001").get("/health").status_code
        == 200
    )
    # Порт, указанный явно, сверяется точно.
    assert (
        _client(hosts, "http://host.docker.internal:6000").get("/health").status_code
        == 421
    )


def test_небезопасный_метод_с_чужим_origin_отклоняется():
    client = _client(allowed_hosts({}))

    чужой = client.post("/api/v1/queries", headers={"origin": "http://evil.example"})
    свой = client.post("/api/v1/queries", headers={"origin": "http://127.0.0.1:8000"})
    без_origin = client.post("/api/v1/queries")
    # Инспектор MCP на соседнем loopback-порту — тот же доверенный хост.
    соседний_порт = client.post(
        "/api/v1/queries", headers={"origin": "http://localhost:6274"}
    )
    чтение = client.get(
        "/api/v1/dashboard/bootstrap", headers={"origin": "http://evil.example"}
    )

    assert чужой.status_code == 403
    assert свой.status_code == 200
    assert без_origin.status_code == 200
    assert соседний_порт.status_code == 200
    assert чтение.status_code == 200


def test_без_списка_граница_пропускает_всё():
    client = _client(None, base_url="http://any-name.example")

    assert client.get("/api/v1/dashboard/bootstrap").status_code == 200


@pytest.mark.parametrize(
    "value",
    ["http://mcp.example.com", "mcp.example.com/path", "host:99999", "user@host", "*"],
)
def test_неверный_элемент_списка_хостов_останавливает_старт(value):
    with pytest.raises(HostConfigurationError):
        allowed_hosts({"MCP1C_ALLOWED_HOSTS": value})


def test_проверка_host_включена_без_токена_и_на_loopback():
    assert host_check_required("127.0.0.1", {}) is True
    assert host_check_required("0.0.0.0", {}) is True
    assert host_check_required("127.0.0.1", {"API_TOKEN": "x" * 32}) is True
    # Docker: bind 0.0.0.0, токен обязателен, клиенты приходят по любому имени.
    assert host_check_required("0.0.0.0", {"API_TOKEN": "x" * 32}) is False
    assert (
        host_check_required(
            "0.0.0.0", {"API_TOKEN": "x" * 32, "MCP1C_ALLOWED_HOSTS": "mcp.lan"}
        )
        is True
    )


@pytest.mark.parametrize(
    ("host", "expected"),
    [
        ("127.0.0.1", True),
        ("127.0.0.2", True),
        ("localhost", True),
        ("::1", True),
        ("[::1]", True),
        ("0.0.0.0", False),
        ("::", False),
        ("192.168.0.5", False),
        ("", False),
    ],
)
def test_loopback_bind_распознаётся(host, expected):
    assert is_loopback_bind(host) is expected


def test_доверенные_proxy_по_умолчанию_и_явным_списком():
    assert forwarded_allow_ips(False, {}) is None
    assert forwarded_allow_ips(True, {}) == "*"
    assert (
        forwarded_allow_ips(
            True, {"MCP1C_TRUSTED_PROXIES": "172.18.0.1, 10.0.0.0/8 ,::1"}
        )
        == "172.18.0.1,10.0.0.0/8,::1"
    )
    for bad in ("proxy.local", "10.0.0.0/33", "*"):
        with pytest.raises(HostConfigurationError):
            forwarded_allow_ips(True, {"MCP1C_TRUSTED_PROXIES": bad})
    # Список без режима доверия — ошибка настройки, а не молчаливый no-op.
    with pytest.raises(HostConfigurationError):
        forwarded_allow_ips(False, {"MCP1C_TRUSTED_PROXIES": "172.18.0.1"})


class _FakeRegistry:
    configurations = [object()]

    def __init__(self, data):
        self.data = data

    def startup(self):
        return []

    def snapshot(self):
        return self


def _patch_main(monkeypatch):
    параметры = {}
    monkeypatch.setattr(server_module, "Registry", _FakeRegistry)
    monkeypatch.setattr(
        server_module, "build_server", lambda registry, **kwargs: object()
    )
    monkeypatch.setattr(
        server_module,
        "_run_streamable_http",
        lambda server, **kwargs: параметры.update(kwargs),
    )
    return параметры


def test_сетевой_bind_без_api_token_не_стартует(tmp_path, monkeypatch):
    monkeypatch.delenv("API_TOKEN", raising=False)
    monkeypatch.setattr(
        server_module,
        "Registry",
        lambda data: pytest.fail("Registry не должен создаваться"),
    )

    with pytest.raises(SystemExit) as ошибка:
        server_module.main(["--data", str(tmp_path), "--host", "0.0.0.0"])

    assert ошибка.value.code == 2


def test_открытое_чтение_в_сети_только_явным_флагом(tmp_path, monkeypatch, capsys):
    monkeypatch.delenv("API_TOKEN", raising=False)
    monkeypatch.setenv("MCP1C_ALLOWED_HOSTS", "192.168.0.5")
    параметры = _patch_main(monkeypatch)

    assert (
        server_module.main(
            ["--data", str(tmp_path), "--host", "0.0.0.0", "--insecure-open-read"]
        )
        == 0
    )
    assert "192.168.0.5" in параметры["allowed_hosts"]
    assert "--insecure-open-read" in capsys.readouterr().err


def test_stdio_без_токена_не_требует_флага(tmp_path, monkeypatch):
    monkeypatch.delenv("API_TOKEN", raising=False)
    запуски = []
    monkeypatch.setattr(server_module, "Registry", _FakeRegistry)
    monkeypatch.setattr(
        server_module,
        "build_server",
        lambda registry, **kwargs: SimpleNamespace(run=lambda **kw: запуски.append(kw)),
    )

    assert (
        server_module.main(
            ["--data", str(tmp_path), "--host", "0.0.0.0", "--transport", "stdio"]
        )
        == 0
    )
    assert запуски == [{"transport": "stdio"}]


def test_docker_с_токенами_не_включает_проверку_host(tmp_path, monkeypatch):
    monkeypatch.setenv("API_TOKEN", "a" * 40)
    monkeypatch.setenv("ADMIN_TOKEN", "b" * 40)
    monkeypatch.delenv("MCP1C_ALLOWED_HOSTS", raising=False)
    параметры = _patch_main(monkeypatch)

    assert (
        server_module.main(
            ["--data", str(tmp_path), "--host", "0.0.0.0", "--require-tokens"]
        )
        == 0
    )
    assert параметры["allowed_hosts"] is None


def test_неверный_trusted_proxies_останавливает_старт(tmp_path, monkeypatch):
    monkeypatch.setenv("MCP1C_ACCESS", "https-proxy")
    monkeypatch.setenv("MCP1C_TRUSTED_PROXIES", "proxy.local")
    monkeypatch.setattr(
        server_module,
        "Registry",
        lambda data: pytest.fail("Registry не должен создаваться"),
    )

    with pytest.raises(SystemExit) as ошибка:
        server_module.main(["--data", str(tmp_path)])

    assert ошибка.value.code == 2


def test_https_proxy_передаёт_список_proxy_в_uvicorn(tmp_path, monkeypatch):
    monkeypatch.setenv("MCP1C_ACCESS", "https-proxy")
    monkeypatch.setenv("MCP1C_TRUSTED_PROXIES", "172.18.0.1")
    параметры = _patch_main(monkeypatch)

    assert server_module.main(["--data", str(tmp_path)]) == 0
    assert параметры["forwarded_allow_ips"] == "172.18.0.1"


def test_sdk_получает_явные_настройки_защиты(monkeypatch):
    вызовы = []
    приложения = []

    class FakeServer:
        def streamable_http_app(self, *, host, transport_security):
            приложения.append(transport_security)
            return _app()

    monkeypatch.setitem(
        sys.modules,
        "uvicorn",
        SimpleNamespace(run=lambda app, **kwargs: вызовы.append((app, kwargs))),
    )

    server_module._run_streamable_http(
        FakeServer(),
        host="127.0.0.1",
        port=8000,
        forwarded_allow_ips=None,
        allowed_hosts=allowed_hosts({}),
    )
    server_module._run_streamable_http(
        FakeServer(),
        host="0.0.0.0",
        port=8000,
        forwarded_allow_ips="*",
        allowed_hosts=None,
    )

    включено, выключено = приложения
    assert включено.enable_dns_rebinding_protection is True
    assert "localhost:*" in включено.allowed_hosts
    assert "http://localhost:*" in включено.allowed_origins
    assert выключено.enable_dns_rebinding_protection is False
    assert вызовы[0][1]["proxy_headers"] is False
    assert вызовы[0][1]["forwarded_allow_ips"] is None
    assert вызовы[1][1]["proxy_headers"] is True
    assert вызовы[1][1]["forwarded_allow_ips"] == "*"

    # Внешний слой стоит перед всем приложением, а не только перед `/mcp`.
    client = TestClient(вызовы[0][0], base_url="http://attacker.example:8000")
    assert client.get("/health").status_code == 421
