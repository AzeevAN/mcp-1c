"""Перебор токенов ограничен одинаково для формы входа и заголовков.

Форма `/login` — не единственная точка сверки: `X-Api-Token`,
`Authorization: Bearer` и `X-Admin-Token` сверяются на `/mcp`, API дашборда и
`/admin/reload`. Ограничение только формы оставляло перебор заголовками без
предела, а сброс счётчика успешным входом давал держателю токена чтения
перебирать токен администратора бесконечно.
"""

from __future__ import annotations

import asyncio

import httpx2 as httpx
import pytest
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import PlainTextResponse
from starlette.routing import Route
from starlette.testclient import TestClient

from mcp1c import dashboard_backend as backend
from mcp1c.dashboard_runtime import DASHBOARD_ON, routes
from mcp1c.registry import Registry
from mcp1c.server import build_server, mcp_guard


# Короткие токены вне Docker угадываются перебором: для них блокировка
# жёсткая, верный токен под ней тоже получает 429.
READ = "reader-weak"
ADMIN = "admin-weak"

# Стойкие (как требует `--require-tokens`): под блокировкой их сверка не
# выдаёт ничего полезного, и владелец верного токена проходит.
STRONG_READ = "reader-token-" + "r" * 24
STRONG_ADMIN = "admin-token-" + "a" * 24


@pytest.fixture
def tokens(monkeypatch):
    monkeypatch.setenv("API_TOKEN", READ)
    monkeypatch.setenv("ADMIN_TOKEN", ADMIN)


@pytest.fixture
def strong_tokens(monkeypatch):
    monkeypatch.setenv("API_TOKEN", STRONG_READ)
    monkeypatch.setenv("ADMIN_TOKEN", STRONG_ADMIN)


def _mcp_client() -> TestClient:
    async def mcp(request):
        return PlainTextResponse("инструменты")

    return TestClient(
        mcp_guard(Starlette(routes=[Route("/mcp", mcp, methods=["GET"])]))
    )


def _request(address: str, token: str) -> Request:
    return Request(
        {
            "type": "http",
            "method": "GET",
            "path": "/",
            "headers": [(b"x-api-token", token.encode())],
            "client": (address, 1),
        }
    )


def test_перебор_заголовком_на_mcp_блокируется(tokens):
    client = _mcp_client()

    for attempt in range(backend.LOGIN_FAILURE_LIMIT):
        assert (
            client.get("/mcp", headers={"x-api-token": f"guess-{attempt}"}).status_code
            == 401
        )

    blocked = client.get("/mcp", headers={"authorization": f"Bearer {READ}"})
    assert blocked.status_code == 429
    assert int(blocked.headers["retry-after"]) > 0


def test_заголовки_и_форма_делят_один_счётчик(tokens, tmp_path):
    registry = Registry(tmp_path / "data")
    server = build_server(registry)
    client = TestClient(
        Starlette(
            routes=[
                *server._custom_starlette_routes,
                *routes(registry, mode=DASHBOARD_ON),
            ]
        )
    )
    half = backend.LOGIN_FAILURE_LIMIT // 2

    for attempt in range(half):
        assert (
            client.post("/admin/reload", headers={"x-admin-token": f"guess-{attempt}"})
            .status_code
            == 403
        )
    for attempt in range(backend.LOGIN_FAILURE_LIMIT - half):
        assert client.post("/login", data={"token": f"form-{attempt}"}).status_code == 403

    assert client.post("/admin/reload", headers={"x-admin-token": ADMIN}).status_code == 429
    assert (
        client.post("/login", data={"token": ADMIN}, follow_redirects=False).status_code
        == 429
    )


def test_успешный_вход_не_сбрасывает_счётчик(tokens):
    """Держатель токена чтения не перебирает токен администратора без предела."""
    for attempt in range(backend.LOGIN_FAILURE_LIMIT - 1):
        assert backend.check_token(_request("10.0.0.5", f"admin-{attempt}"), f"admin-{attempt}") == (None, None)

    assert backend.check_token(_request("10.0.0.5", READ), READ) == (backend.LEVEL_READ, None)
    assert backend.check_token(_request("10.0.0.5", "admin-last"), "admin-last") == (None, None)

    level, retry_after = backend.check_token(_request("10.0.0.5", ADMIN), ADMIN)
    assert level is None
    assert retry_after is not None


def test_повтор_одного_устаревшего_токена_не_блокирует_адрес(tokens):
    """За NAT Docker Desktop все клиенты — один адрес; клиент со старым
    токеном в цикле повторов не должен отрезать остальных."""
    client = _mcp_client()

    for _ in range(backend.LOGIN_FAILURE_LIMIT * 5):
        assert client.get("/mcp", headers={"x-api-token": "stale-token"}).status_code == 401

    assert client.get("/mcp", headers={"x-api-token": READ}).status_code == 200


def test_верный_токен_чтения_на_проверке_админа_не_считается_неудачей(tokens, tmp_path):
    client = TestClient(Starlette(routes=routes(Registry(tmp_path / "data"), mode=DASHBOARD_ON)))

    for _ in range(backend.LOGIN_FAILURE_LIMIT * 2):
        response = client.get(
            "/api/v1/dashboard/bootstrap", headers={"x-api-token": READ}
        )
        assert response.json()["permissions"]["admin"] is False

    assert backend._LOGIN_FAILURES == {}


def test_общий_потолок_останавливает_перебор_с_разных_адресов(tokens):
    """Подделанный X-Forwarded-For меняет адрес на каждую попытку."""
    assert backend.check_token(_request("192.168.0.10", READ), READ)[0] == backend.LEVEL_READ

    for attempt in range(backend.GLOBAL_LOGIN_FAILURE_LIMIT):
        token = f"guess-{attempt}"
        backend.check_token(_request(f"10.1.{attempt // 250}.{attempt % 250}", token), token)

    new_level, new_retry = backend.check_token(_request("172.31.0.1", READ), READ)
    known_level, known_retry = backend.check_token(_request("192.168.0.10", READ), READ)

    assert new_level is None and new_retry is not None
    # Адрес, уже входивший до волны перебора, продолжает работать.
    assert (known_level, known_retry) == (backend.LEVEL_READ, None)


def test_параллельные_придержанные_входы_не_дают_лишних_проб(tokens, tmp_path, monkeypatch):
    app = Starlette(routes=routes(Registry(tmp_path / "data"), mode=DASHBOARD_ON))
    total = backend.LOGIN_FAILURE_LIMIT + 5
    entered = 0
    gate = asyncio.Event()
    real_form = Request.form

    async def held_form(self, *args, **kwargs):
        nonlocal entered
        form = await real_form(self, *args, **kwargs)
        entered += 1
        if entered == total:
            gate.set()
        await gate.wait()
        return form

    monkeypatch.setattr(Request, "form", held_form)

    async def scenario():
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://testserver"
        ) as client:
            responses = await asyncio.gather(
                *(
                    client.post("/login", data={"token": f"parallel-{index}"})
                    for index in range(total)
                )
            )
        return sorted(response.status_code for response in responses)

    statuses = asyncio.run(scenario())

    assert statuses.count(403) == backend.LOGIN_FAILURE_LIMIT
    assert statuses.count(429) == total - backend.LOGIN_FAILURE_LIMIT


def _full_client(tmp_path) -> TestClient:
    registry = Registry(tmp_path / "data")
    server = build_server(registry)
    return TestClient(
        mcp_guard(
            Starlette(
                routes=[
                    *server._custom_starlette_routes,
                    *routes(registry, mode=DASHBOARD_ON),
                ]
            )
        )
    )


def test_стойкий_верный_токен_проходит_под_блокировкой_адреса(strong_tokens, tmp_path):
    """За NAT Docker все клиенты — один адрес: десять чужих догадок не
    должны отрезать владельцев верных токенов."""
    client = _mcp_client()
    for attempt in range(backend.LOGIN_FAILURE_LIMIT):
        assert (
            client.get("/mcp", headers={"x-api-token": f"guess-{attempt}"}).status_code
            == 401
        )

    assert client.get("/mcp", headers={"x-api-token": STRONG_READ}).status_code == 200
    assert (
        client.get("/mcp", headers={"authorization": f"Bearer {STRONG_ADMIN}"}).status_code
        == 200
    )
    before = dict(backend._LOGIN_FAILURES["testclient"])
    blocked = client.get("/mcp", headers={"x-api-token": "guess-new"})
    assert blocked.status_code == 429
    # Неверная догадка под блокировкой не продлевает её.
    assert backend._LOGIN_FAILURES["testclient"] == before


def test_стойкий_токен_проходит_форму_входа_под_блокировкой(strong_tokens, tmp_path):
    client = _full_client(tmp_path)
    for attempt in range(backend.LOGIN_FAILURE_LIMIT):
        assert client.post("/login", data={"token": f"form-{attempt}"}).status_code == 403
    assert client.post("/login", data={"token": "form-more"}).status_code == 429

    ok = client.post("/login", data={"token": STRONG_ADMIN}, follow_redirects=False)
    assert ok.status_code == 303


def test_стойкий_токен_проходит_общий_потолок(strong_tokens):
    for attempt in range(backend.GLOBAL_LOGIN_FAILURE_LIMIT):
        token = f"guess-{attempt}"
        backend.check_token(_request(f"10.1.{attempt // 250}.{attempt % 250}", token), token)

    assert backend.check_token(_request("172.31.0.1", STRONG_READ), STRONG_READ) == (
        backend.LEVEL_READ,
        None,
    )
    level, retry = backend.check_token(_request("172.31.0.1", "guess"), "guess")
    assert level is None and retry is not None


def test_слабый_токен_под_блокировкой_не_сверяется(monkeypatch):
    """Смешанный набор: стойкий токен администратора проходит, короткий
    токен чтения — нет, иначе блокировка перестала бы сдерживать его перебор."""
    monkeypatch.setenv("API_TOKEN", READ)
    monkeypatch.setenv("ADMIN_TOKEN", STRONG_ADMIN)
    for attempt in range(backend.LOGIN_FAILURE_LIMIT):
        token = f"guess-{attempt}"
        backend.check_token(_request("10.0.0.7", token), token)

    assert backend.check_token(_request("10.0.0.7", STRONG_ADMIN), STRONG_ADMIN)[0] == (
        backend.LEVEL_ADMIN
    )
    level, retry = backend.check_token(_request("10.0.0.7", READ), READ)
    assert level is None and retry is not None


@pytest.mark.parametrize(
    "headers",
    [
        {"sec-fetch-site": "cross-site"},
        {"origin": "https://evil.example"},
        {"origin": "null"},
        {"origin": "http://testserver:8080"},
    ],
)
def test_вход_с_чужого_сайта_отклоняется_без_учёта(strong_tokens, tmp_path, headers):
    client = _full_client(tmp_path)

    for attempt in range(backend.LOGIN_FAILURE_LIMIT * 2):
        response = client.post("/login", data={"token": f"x-{attempt}"}, headers=headers)
        assert response.status_code == 403

    assert backend._LOGIN_FAILURES == {}
    assert backend._GLOBAL_FAILURES == {}
    # Верный токен с чужой страницы тоже не обменивается на сессию.
    denied = client.post(
        "/login", data={"token": STRONG_ADMIN}, headers=headers, follow_redirects=False
    )
    assert denied.status_code == 403
    assert "set-cookie" not in denied.headers


@pytest.mark.parametrize(
    "headers",
    [
        {},
        {"origin": "http://testserver"},
        {"origin": "http://testserver:80"},
        {"sec-fetch-site": "same-origin", "origin": "http://testserver"},
        {"sec-fetch-site": "none"},
    ],
)
def test_вход_со_своей_страницы_и_без_заголовков_проходит(strong_tokens, tmp_path, headers):
    client = _full_client(tmp_path)

    assert client.post("/login", data={"token": "wrong"}, headers=headers).status_code == 403
    assert len(backend._LOGIN_FAILURES["testclient"]) == 1
    ok = client.post(
        "/login", data={"token": STRONG_READ}, headers=headers, follow_redirects=False
    )
    assert ok.status_code == 303


def test_перезагрузка_по_заголовку_админа_проходит_охрану_mcp(strong_tokens, tmp_path):
    """Документированный `curl -H 'X-Admin-Token: …' /admin/reload` при
    заданном API_TOKEN не должен упираться во внешний слой с 401."""
    client = _full_client(tmp_path)

    ok = client.post("/admin/reload", headers={"x-admin-token": STRONG_ADMIN})
    assert ok.status_code == 200, ok.text
    wrong = client.post("/admin/reload", headers={"x-admin-token": "wrong"})
    assert wrong.status_code == 401
    # Токен чтения в X-Admin-Token даёт чтение, но не перезагрузку.
    reader = client.post("/admin/reload", headers={"x-admin-token": STRONG_READ})
    assert reader.status_code == 403
