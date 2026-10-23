"""Сетевая граница HTTP-сервера: Host/Origin, открытое чтение, доверие proxy.

Проверки DNS rebinding SDK закрывают только `/mcp`, а дашборд, `/health` и
служебные маршруты живут рядом в том же приложении. Поэтому граница проверяется
снаружи всего приложения — так, как его запускает `_run_streamable_http`.
"""

from __future__ import annotations

import sys
from types import SimpleNamespace

import pytest

from test_runtime_config import _L1_ADMIN, _L1_API, _l1_no_tokens_in, _l1_weak
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
            "MCP1C_ALLOWED_HOSTS": "192.168.0.5, port-limited.example:5001",
        }
    )

    assert _client(hosts, "https://mcp.example.com").get("/health").status_code == 200
    assert _client(hosts, "http://192.168.0.5:80").get("/health").status_code == 200
    assert (
        _client(hosts, "http://port-limited.example:5001").get("/health").status_code
        == 200
    )
    # Порт, указанный явно, сверяется точно.
    assert (
        _client(hosts, "http://port-limited.example:6000").get("/health").status_code
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
    # В local контейнер с токеном также защищён от чужого имени сервера.
    assert host_check_required("0.0.0.0", {"API_TOKEN": "x" * 32}) is True
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

    def acquire_ownership(self, *_args, **_kwargs):
        return True

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


def test_docker_с_токенами_сохраняет_local_проверку_host(tmp_path, monkeypatch):
    monkeypatch.setenv("API_TOKEN", _L1_API)
    monkeypatch.setenv("ADMIN_TOKEN", _L1_ADMIN)
    monkeypatch.delenv("MCP1C_ALLOWED_HOSTS", raising=False)
    параметры = _patch_main(monkeypatch)

    assert (
        server_module.main(
            ["--data", str(tmp_path), "--host", "0.0.0.0", "--require-tokens"]
        )
        == 0
    )
    assert параметры["allowed_hosts"] == ("127.0.0.1", "localhost", "[::1]", "host.docker.internal")


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


@pytest.mark.parametrize("value", ["172.18.0.1/16", ",", " , ,"])
def test_trusted_proxies_отвергает_нестрогую_сеть_и_пустой_список(value):
    # Uvicorn разбирает сеть строго: `172.18.0.1/16` у него не сеть, а
    # литерал, и доверие молча выключилось бы.
    with pytest.raises(HostConfigurationError) as error:
        forwarded_allow_ips(True, {"MCP1C_TRUSTED_PROXIES": value})
    if "/" in value:
        assert "172.18.0.0/16" in str(error.value)


def test_неразбираемый_public_base_url_даёт_понятную_ошибку():
    with pytest.raises(HostConfigurationError, match="MCP1C_PUBLIC_BASE_URL"):
        allowed_hosts({"MCP1C_PUBLIC_BASE_URL": "http://[::1"})


def test_отказ_по_origin_подсказывает_переменную():
    client = _client(allowed_hosts({}))

    response = client.post("/api/v1/queries", headers={"origin": "http://evil.example"})

    assert response.status_code == 403
    assert "MCP1C_ALLOWED_HOSTS" in response.text


def _websocket(headers):
    called = []
    sent = []

    async def inner(scope, receive, send):
        called.append(scope["type"])

    async def receive():
        return {"type": "websocket.connect"}

    async def send(message):
        sent.append(message)

    import asyncio

    asyncio.run(
        server_module.trusted_host_guard(inner, ("localhost",))(
            {"type": "websocket", "path": "/ws", "headers": headers},
            receive,
            send,
        )
    )
    return called, sent


def test_websocket_с_чужим_origin_закрывается_до_приложения():
    called, sent = _websocket(
        [(b"host", b"localhost"), (b"origin", b"https://evil.example")],
    )
    assert called == []
    assert [message["type"] for message in sent] == ["websocket.close"]


def test_websocket_с_чужим_host_закрывается_до_приложения():
    called, sent = _websocket([(b"host", b"evil.example")])
    assert called == []
    assert [message["type"] for message in sent] == ["websocket.close"]


def test_websocket_своего_origin_доходит_до_приложения():
    called, sent = _websocket(
        [(b"host", b"localhost"), (b"origin", b"http://localhost")],
    )
    assert called == ["websocket"]




def _l1_main_environment(monkeypatch, *, api=_L1_API, admin=_L1_ADMIN,
                         access="local", proxies=""):
    for name, value in (("API_TOKEN", api), ("ADMIN_TOKEN", admin)):
        if value is None:
            monkeypatch.delenv(name, raising=False)
        else:
            monkeypatch.setenv(name, value)
    monkeypatch.setenv("MCP1C_ACCESS", access)
    monkeypatch.setenv("MCP1C_TRUSTED_PROXIES", proxies)
    monkeypatch.delenv("MCP1C_ALLOWED_HOSTS", raising=False)
    monkeypatch.delenv("MCP1C_PUBLIC_BASE_URL", raising=False)


def _l1_main_stubs(monkeypatch):
    http = _patch_main(monkeypatch)
    stdio = []
    monkeypatch.setattr(
        server_module, "build_server",
        lambda registry, **kwargs: SimpleNamespace(run=lambda **params: stdio.append(params)),
    )
    return http, stdio


@pytest.mark.parametrize("token_name", ["API_TOKEN", "ADMIN_TOKEN"])
@pytest.mark.parametrize("insecure_open_read", [False, True])
def test_network_http_отвергает_заданный_слабый_токен_без_require_tokens(
    tmp_path, monkeypatch, capsys, token_name, insecure_open_read
):
    environment = {"API_TOKEN": _L1_API, "ADMIN_TOKEN": _L1_ADMIN}
    environment[token_name] = "1234" if token_name == "API_TOKEN" else _l1_weak("periodic")
    _l1_main_environment(monkeypatch, api=environment["API_TOKEN"], admin=environment["ADMIN_TOKEN"])
    _l1_main_stubs(monkeypatch)
    monkeypatch.setattr(server_module, "Registry", lambda data: pytest.fail("Отказ нужен до Registry"))
    args = ["--data", str(tmp_path), "--host", "0.0.0.0"]
    if insecure_open_read:
        args.append("--insecure-open-read")

    with pytest.raises(SystemExit) as caught:
        server_module.main(args)

    assert caught.value.code == 2
    captured = capsys.readouterr()
    assert token_name in captured.err
    _l1_no_tokens_in(captured.out + captured.err, environment.values())


def test_open_read_без_api_не_разрешает_слабый_admin(tmp_path, monkeypatch, capsys):
    weak_admin = _l1_weak("periodic")
    _l1_main_environment(monkeypatch, api=None, admin=weak_admin)
    _l1_main_stubs(monkeypatch)
    monkeypatch.setattr(server_module, "Registry", lambda data: pytest.fail("Отказ нужен до Registry"))

    with pytest.raises(SystemExit) as caught:
        server_module.main(["--data", str(tmp_path), "--host", "0.0.0.0", "--insecure-open-read"])

    assert caught.value.code == 2
    captured = capsys.readouterr()
    assert "ADMIN_TOKEN" in captured.err
    _l1_no_tokens_in(captured.out + captured.err, (weak_admin,))


@pytest.mark.parametrize(
    ("admin_present", "require_tokens"), [(False, False), (True, False), (True, True)],
    ids=["api-only", "both-optional", "both-required"],
)
def test_network_стойкие_токены_разрешают_старт_с_необязательным_admin(
    tmp_path, monkeypatch, capsys, admin_present, require_tokens
):
    _l1_main_environment(monkeypatch, admin=_L1_ADMIN if admin_present else None)
    http, _stdio = _l1_main_stubs(monkeypatch)
    args = ["--data", str(tmp_path), "--host", "0.0.0.0"]
    if require_tokens:
        args.append("--require-tokens")

    assert server_module.main(args) == 0

    assert http["host"] == "0.0.0.0"
    captured = capsys.readouterr()
    _l1_no_tokens_in(captured.out + captured.err, (_L1_API, _L1_ADMIN))


@pytest.mark.parametrize(
    ("transport", "host"), [("streamable-http", "127.0.0.1"), ("stdio", "0.0.0.0")],
    ids=["loopback-http", "stdio"],
)
def test_local_и_stdio_без_require_tokens_сохраняют_слабые_токены(
    tmp_path, monkeypatch, capsys, transport, host
):
    weak_api, weak_admin = "1234", _l1_weak("periodic")
    _l1_main_environment(monkeypatch, api=weak_api, admin=weak_admin)
    http, stdio = _l1_main_stubs(monkeypatch)

    assert server_module.main([
        "--data", str(tmp_path), "--host", host, "--transport", transport,
    ]) == 0

    if transport == "stdio":
        assert stdio == [{"transport": "stdio"}]
    else:
        assert http["host"] == host
    captured = capsys.readouterr()
    _l1_no_tokens_in(captured.out + captured.err, (weak_api, weak_admin))


@pytest.mark.parametrize("transport", ["streamable-http", "stdio"])
def test_require_tokens_отвергает_предсказуемый_api_и_для_локального_транспорта(
    tmp_path, monkeypatch, capsys, transport
):
    weak_api = _l1_weak("periodic")
    _l1_main_environment(monkeypatch, api=weak_api)
    _l1_main_stubs(monkeypatch)
    monkeypatch.setattr(server_module, "Registry", lambda data: pytest.fail("Отказ нужен до Registry"))

    with pytest.raises(SystemExit) as caught:
        server_module.main(["--data", str(tmp_path), "--transport", transport, "--require-tokens"])

    assert caught.value.code == 2
    captured = capsys.readouterr()
    assert "API_TOKEN" in captured.err
    _l1_no_tokens_in(captured.out + captured.err, (weak_api, _L1_ADMIN))


@pytest.mark.parametrize(
    ("transport", "host", "proxies", "warned"),
    [
        ("streamable-http", "0.0.0.0", "", True),
        ("streamable-http", "0.0.0.0", "192.0.2.2", False),
        ("streamable-http", "127.0.0.1", "", False),
        ("stdio", "0.0.0.0", "", False),
    ],
    ids=["network-empty", "network-explicit", "loopback-empty", "stdio-empty"],
)
def test_https_proxy_предупреждает_о_пустом_trusted_только_в_сети(
    tmp_path, monkeypatch, capsys, transport, host, proxies, warned
):
    _l1_main_environment(monkeypatch, access="https-proxy", proxies=proxies)
    _l1_main_stubs(monkeypatch)

    assert server_module.main([
        "--data", str(tmp_path), "--host", host, "--transport", transport,
    ]) == 0

    captured = capsys.readouterr()
    if warned:
        assert "MCP1C_TRUSTED_PROXIES" in captured.err
        assert "предупреждение" in captured.err.lower()
    else:
        assert "MCP1C_TRUSTED_PROXIES" not in captured.err
    _l1_no_tokens_in(captured.out + captured.err, (_L1_API, _L1_ADMIN))




@pytest.mark.parametrize(
    ('mode', 'extra', 'host', 'status'),
    [
        ('local', {}, 'localhost:5001', 200),
        ('local', {}, '127.0.0.1:5001', 200),
        ('local', {}, '[::1]:5099', 200),
        ('local', {}, 'host.docker.internal:5001', 200),
        ('local', {}, 'evil.example', 421),
        ('local', {'MCP1C_PUBLIC_BASE_URL': 'https://mcp.example.com'}, 'mcp.example.com:9443', 200),
        ('local', {'MCP1C_ALLOWED_HOSTS': 'mcp.lan'}, 'mcp.lan:9123', 200),
        ('local', {'MCP1C_PUBLISHED_ADDRESS': '192.0.2.10'}, '192.0.2.10:5001', 200),
        ('http', {'MCP1C_PUBLISHED_ADDRESS': '2001:db8::10'}, '[2001:db8::10]:9123', 200),
        ('http', {'MCP1C_PUBLISHED_ADDRESS': 'localhost'}, 'localhost:9123', 200),
        ('http', {'MCP1C_PUBLISHED_ADDRESS': '0.0.0.0', 'MCP1C_ALLOWED_HOSTS': 'mcp.lan'}, '0.0.0.0:5001', 421),
        ('https-proxy', {'MCP1C_PUBLIC_BASE_URL': 'https://mcp.example.com'}, 'mcp.example.com', 200),
        ('https-proxy', {'MCP1C_PUBLIC_BASE_URL': 'https://mcp.example.com'}, '10.0.0.5', 421),
        ('https-proxy', {'MCP1C_PUBLIC_BASE_URL': 'https://mcp.example.com', 'MCP1C_PUBLISHED_ADDRESS': '192.0.2.10'}, '192.0.2.10:5001', 421),
    ],
)
def test_i2_реальная_http_граница_проверяет_выведенные_имена(mode, extra, host, status):
    environment = {'API_TOKEN': _L1_API, 'MCP1C_ACCESS': mode, **extra}
    assert host_check_required('0.0.0.0', environment)
    hosts = allowed_hosts(environment, bind_host='0.0.0.0')
    with _client(hosts) as client:
        response = client.get('/api/v1/dashboard/bootstrap', headers={'host': host})
        assert response.status_code == status
        # Healthcheck контейнера остаётся доступен при любой публикации.
        assert client.get('/health', headers={'host': '127.0.0.1:8000'}).status_code == 200


@pytest.mark.parametrize('mode', ['local', 'http', 'https-proxy'])
def test_i2_публикация_не_открывает_запись_чужому_origin(mode):
    environment = {
        'API_TOKEN': _L1_API, 'MCP1C_ACCESS': mode,
        'MCP1C_PUBLISHED_ADDRESS': '192.0.2.10',
        'MCP1C_PUBLIC_BASE_URL': 'https://mcp.example.com',
    }
    with _client(allowed_hosts(environment, bind_host='0.0.0.0')) as client:
        assert client.post('/api/v1/queries', headers={
            'host': 'mcp.example.com', 'origin': 'https://evil.example',
        }).status_code == 403
        assert client.post('/api/v1/queries', headers={
            'host': 'mcp.example.com', 'origin': 'https://mcp.example.com',
        }).status_code == 200


@pytest.mark.parametrize(
    ('mode', 'bind', 'extra', 'transport', 'guard', 'warn'),
    [
        ('local', '0.0.0.0', {}, 'streamable-http', True, False),
        ('http', '0.0.0.0', {'MCP1C_PUBLISHED_ADDRESS': '0.0.0.0'}, 'streamable-http', False, True),
        ('http', '0.0.0.0', {'MCP1C_PUBLISHED_ADDRESS': '192.0.2.10'}, 'streamable-http', True, False),
        ('http', '192.0.2.10', {}, 'streamable-http', True, False),
        ('https-proxy', '0.0.0.0', {}, 'streamable-http', False, True),
        ('https-proxy', '0.0.0.0', {'MCP1C_PUBLIC_BASE_URL': 'https://mcp.example.com'}, 'streamable-http', True, False),
        ('https-proxy', '0.0.0.0', {}, 'stdio', False, False),
    ],
)
def test_i2_main_передаёт_границу_и_предупреждает_об_отключении(
    tmp_path, monkeypatch, capsys, mode, bind, extra, transport, guard, warn,
):
    # Явный proxy устраняет независимое предупреждение о доверии forwarded-заголовкам.
    _l1_main_environment(monkeypatch, access=mode, proxies='192.0.2.2' if mode == 'https-proxy' else '')
    monkeypatch.delenv('MCP1C_PUBLISHED_ADDRESS', raising=False)
    for name, value in extra.items():
        monkeypatch.setenv(name, value)
    http, stdio = _l1_main_stubs(monkeypatch)
    assert server_module.main([
        '--data', str(tmp_path), '--host', bind, '--transport', transport,
    ]) == 0
    if transport == 'stdio':
        assert not http
        assert stdio == [{'transport': 'stdio'}]
    else:
        assert (http['allowed_hosts'] is not None) is guard
        if bind == '192.0.2.10':
            with _client(http['allowed_hosts']) as client:
                assert client.get('/health', headers={'host': bind + ':5099'}).status_code == 200
                assert client.get('/health', headers={'host': 'evil.example'}).status_code == 421
    captured = capsys.readouterr()
    # TLS и proxy diagnostics не подменяют предупреждение об отключённой Host-защите.
    host_warning = 'host' in captured.err.lower()
    assert host_warning is warn
    if warn:
        assert 'предупреждение' in captured.err.lower()
        if mode == 'https-proxy':
            assert 'MCP1C_PUBLIC_BASE_URL' in captured.err
    _l1_no_tokens_in(captured.out + captured.err, (_L1_API, _L1_ADMIN))


@pytest.mark.parametrize('mode', ['local', 'http', 'https-proxy'])
def test_i2_main_sdk_и_внешняя_граница_получают_одну_политику(tmp_path, monkeypatch, mode):
    _l1_main_environment(monkeypatch, access=mode, proxies='192.0.2.2' if mode == 'https-proxy' else '')
    monkeypatch.setenv('MCP1C_PUBLISHED_ADDRESS', '192.0.2.10')
    if mode == 'https-proxy':
        monkeypatch.setenv('MCP1C_PUBLIC_BASE_URL', 'https://mcp.example.com')
    settings, served = [], []

    class FakeServer:
        def streamable_http_app(self, *, host, transport_security):
            settings.append(transport_security)
            return _app()

    monkeypatch.setattr(server_module, 'Registry', _FakeRegistry)
    monkeypatch.setattr(server_module, 'build_server', lambda registry, **kwargs: FakeServer())
    monkeypatch.setitem(sys.modules, 'uvicorn', SimpleNamespace(
        run=lambda app, **kwargs: served.append(app),
    ))
    assert server_module.main(['--data', str(tmp_path), '--host', '0.0.0.0']) == 0
    assert len(settings) == len(served) == 1
    sdk = settings[0]
    assert sdk.enable_dns_rebinding_protection is True
    accepted = 'mcp.example.com' if mode == 'https-proxy' else '192.0.2.10'
    rejected = '192.0.2.10' if mode == 'https-proxy' else 'evil.example'
    assert accepted + ':*' in sdk.allowed_hosts
    assert 'https://' + accepted + ':*' in sdk.allowed_origins
    assert rejected not in sdk.allowed_hosts
    assert rejected + ':*' not in sdk.allowed_hosts
    with TestClient(served[0]) as client:
        assert client.get('/health', headers={'host': accepted + ':5099'}).status_code == 200
        assert client.get('/health', headers={'host': rejected}).status_code == 421
        assert client.post('/api/v1/queries', headers={
            'host': accepted, 'origin': 'https://' + rejected,
        }).status_code == 403
