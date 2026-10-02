"""Доступ к серверу: кто и что видит без токена.

Два уровня, разделённые по цене ошибки. `API_TOKEN` — чтение: инструменты
MCP, страницы дашборда, карточки. `ADMIN_TOKEN` — запись: загрузка и удаление
источников, правка словаря. Разделены потому, что токен чтения лежит в
конфиге каждого MCP-клиента и утекает вместе с ним, а прав удалять источники
у агента быть не должно.

Незаданный `API_TOKEN` оставляет всё открытым — как было до появления
авторизации. Выставление в сеть остаётся осознанным действием, а чистая
установка и локальная разработка не ломаются.
"""

from __future__ import annotations

import hmac

from starlette.applications import Starlette
from starlette.testclient import TestClient

from mcp1c.dashboard_runtime import DASHBOARD_ON, routes
from mcp1c.registry import Registry

from conftest import build_configuration, write_export


def client_for(tmp_path, **client_options) -> tuple[TestClient, Registry]:
    data_dir = tmp_path / "data"
    incoming = tmp_path / "incoming"
    data_dir.mkdir()
    incoming.mkdir()
    registry = Registry(data_dir)
    registry.add_configuration(write_export(incoming, build_configuration()))
    static_dir = tmp_path / "dist"
    static_dir.mkdir()
    (static_dir / "index.html").write_text(
        "<!doctype html><title>Дашборд</title><p>токен</p>", encoding="utf-8"
    )
    app = Starlette(
        routes=routes(registry, mode=DASHBOARD_ON, static_dir=static_dir)
    )
    base_url = str(client_options.get("base_url", "http://testserver")).rstrip("/")
    headers = dict(client_options.get("headers", {}))
    headers.setdefault("origin", base_url)
    client_options["headers"] = headers
    return TestClient(app, **client_options), registry


def test_без_api_token_чтение_открыто(tmp_path, monkeypatch):
    """Обратная совместимость: не задал — работает как раньше."""
    monkeypatch.delenv("API_TOKEN", raising=False)
    client, _ = client_for(tmp_path)

    response = client.get("/api/v1/dashboard/bootstrap")

    assert response.status_code == 200
    assert response.json()["summary"]["configurations"] == 1


def test_с_api_token_чтение_закрыто(tmp_path, monkeypatch):
    monkeypatch.setenv("API_TOKEN", "reader-token")
    client, _ = client_for(tmp_path)

    response = client.get("/api/v1/dashboard/bootstrap")

    assert response.status_code == 401
    assert "summary" not in response.json()


def test_токен_чтения_в_заголовке_открывает_доступ(tmp_path, monkeypatch):
    monkeypatch.setenv("API_TOKEN", "reader-token")
    client, _ = client_for(tmp_path)

    response = client.get(
        "/api/v1/dashboard/bootstrap", headers={"x-api-token": "reader-token"}
    )

    assert response.status_code == 200
    assert response.json()["summary"]["configurations"] == 1


def test_bearer_тоже_принимается(tmp_path, monkeypatch):
    """MCP-клиенты передают токен через `Authorization`."""
    monkeypatch.setenv("API_TOKEN", "reader-token")
    client, _ = client_for(tmp_path)

    response = client.get(
        "/api/v1/dashboard/bootstrap",
        headers={"authorization": "Bearer reader-token"},
    )

    assert response.status_code == 200


def test_неверный_токен_чтения_не_пускает(tmp_path, monkeypatch):
    monkeypatch.setenv("API_TOKEN", "reader-token")
    client, _ = client_for(tmp_path)

    assert client.get(
        "/api/v1/dashboard/bootstrap", headers={"x-api-token": "wrong-token"}
    ).status_code == 401


def test_вход_по_токену_чтения_даёт_сессию_без_прав_записи(tmp_path, monkeypatch):
    """Браузеру нужна сессия: заголовок в адресную строку не вписать."""
    monkeypatch.setenv("API_TOKEN", "reader-token")
    monkeypatch.setenv("ADMIN_TOKEN", "admin-token")
    client, registry = client_for(tmp_path)

    client.post("/login", data={"token": "reader-token"})

    assert client.get("/api/v1/dashboard/bootstrap").status_code == 200
    assert client.get("/api/v1/dashboard/bootstrap").json()["permissions"] == {
        "read": True,
        "admin": False,
    }
    отказ = client.post(
        "/api/v1/dictionary/aliases",
        json={
            "phrase": "склад",
            "targets": ["Справочник.Контрагенты"],
            "config": "ТестоваяКонфигурация",
        },
    )
    assert отказ.status_code == 403
    assert registry.dictionary.aliases == {}


def test_http_сессия_остаётся_httponly_но_не_получает_secure(tmp_path, monkeypatch):
    monkeypatch.setenv("API_TOKEN", "reader-token")
    client, _ = client_for(tmp_path)

    response = client.post(
        "/login", data={"token": "reader-token"}, follow_redirects=False
    )
    cookie = response.headers["set-cookie"]

    assert "HttpOnly" in cookie
    assert "SameSite=strict" in cookie
    assert "; Secure" not in cookie


def test_https_сессия_получает_secure(tmp_path, monkeypatch):
    monkeypatch.setenv("API_TOKEN", "reader-token")
    client, _ = client_for(tmp_path, base_url="https://mcp.example.test")

    response = client.post(
        "/login", data={"token": "reader-token"}, follow_redirects=False
    )

    assert "; Secure" in response.headers["set-cookie"]


def test_вход_по_админскому_токену_даёт_и_чтение_и_запись(tmp_path, monkeypatch):
    monkeypatch.setenv("API_TOKEN", "reader-token")
    monkeypatch.setenv("ADMIN_TOKEN", "admin-token")
    client, registry = client_for(tmp_path)

    client.post("/login", data={"token": "admin-token"})

    assert client.get("/api/v1/dashboard/bootstrap").status_code == 200
    ответ = client.post(
        "/api/v1/dictionary/aliases",
        json={
            "phrase": "склад",
            "targets": ["Справочник.Контрагенты"],
            "config": "ТестоваяКонфигурация",
        },
    )
    assert ответ.status_code == 200


def test_админский_токен_работает_и_как_токен_чтения(tmp_path, monkeypatch):
    """Иначе владельцу пришлось бы держать два заголовка вместо одного."""
    monkeypatch.setenv("API_TOKEN", "reader-token")
    monkeypatch.setenv("ADMIN_TOKEN", "admin-token")
    client, _ = client_for(tmp_path)

    assert client.get(
        "/api/v1/dashboard/bootstrap", headers={"x-api-token": "admin-token"}
    ).status_code == 200


def test_страница_входа_доступна_без_токена(tmp_path, monkeypatch):
    """Иначе войти неоткуда: форма входа сама была бы за авторизацией."""
    monkeypatch.setenv("API_TOKEN", "reader-token")
    monkeypatch.setenv("ADMIN_TOKEN", "admin-token")
    client, _ = client_for(tmp_path)

    response = client.get("/login")

    assert response.status_code == 200
    assert "токен" in response.text.lower()


def test_кириллический_токен_в_заголовке_не_роняет_сервер(tmp_path, monkeypatch):
    """Заголовки HTTP — latin-1, кириллицу в них не передать.

    Токен задаёт человек, и «секрет» кириллицей — первое, что он напишет.
    Через форму входа такой токен работает, через заголовок физически не
    доходит; сервер обязан ответить отказом, а не упасть. Родственная находка
    про `hmac.compare_digest` и не-ASCII уже записана в CHANGELOG.
    """
    monkeypatch.setenv("API_TOKEN", "секрет")
    client, _ = client_for(tmp_path)

    # Заголовок с кириллицей не собрать — клиент отвергнет его сам, поэтому
    # проверяем то, что достижимо: без заголовка отказ, через форму — вход.
    assert client.get("/api/v1/dashboard/bootstrap").status_code == 401
    client.post("/login", data={"token": "секрет"})
    assert client.get("/api/v1/dashboard/bootstrap").status_code == 200


def test_все_пути_авторизации_сравнивают_токены_constant_time(
    tmp_path, monkeypatch
):
    """Новый auth path не должен обходить общий безопасный контракт."""
    from mcp1c.server import build_server

    monkeypatch.setenv("API_TOKEN", "reader-token")
    monkeypatch.setenv("ADMIN_TOKEN", "admin-token")
    вызовы = []
    исходное_сравнение = hmac.compare_digest

    def записать_сравнение(given, expected):
        вызовы.append((given, expected))
        return исходное_сравнение(given, expected)

    monkeypatch.setattr(hmac, "compare_digest", записать_сравнение)
    dashboard_client, registry = client_for(tmp_path)
    server = build_server(registry)
    reload_client = TestClient(
        Starlette(routes=server._custom_starlette_routes[:2])
    )
    сценарии = [
        (
            "чтение",
            lambda: dashboard_client.get(
                "/api/v1/dashboard/bootstrap", headers={"x-api-token": "reader-token"}
            ),
            200,
        ),
        (
            "администрирование",
            lambda: dashboard_client.post(
                "/api/v1/dictionary/aliases",
                headers={"x-api-token": "admin-token"},
                json={
                    "phrase": "склад",
                    "targets": ["Справочник.Контрагенты"],
                    "config": "ТестоваяКонфигурация",
                },
            ),
            200,
        ),
        (
            "вход",
            lambda: dashboard_client.post(
                "/login", data={"token": "admin-token"}, follow_redirects=False
            ),
            303,
        ),
        (
            "ручная перезагрузка",
            lambda: reload_client.post(
                "/admin/reload", headers={"x-admin-token": "admin-token"}
            ),
            200,
        ),
    ]

    for название, запрос, статус in сценарии:
        до = len(вызовы)
        response = запрос()
        assert response.status_code == статус
        assert len(вызовы) > до, f"{название} обошла constant-time сравнение"


def test_mcp_эндпоинт_закрыт_токеном(tmp_path, monkeypatch):
    """Инструменты отдают структуру целиком — охраняются как страницы."""
    from starlette.responses import PlainTextResponse
    from starlette.routing import Route

    from mcp1c.server import mcp_guard

    monkeypatch.setenv("API_TOKEN", "reader-token")

    async def mcp(request):
        return PlainTextResponse("инструменты")

    async def health(request):
        return PlainTextResponse("ok")

    async def asset(request):
        return PlainTextResponse("статика")

    app = mcp_guard(
        Starlette(routes=[Route("/mcp", mcp, methods=["GET"]),
                          Route("/health", health, methods=["GET"]),
                          Route("/login", health, methods=["GET"]),
                          Route("/assets/app.js", asset, methods=["GET"])])
    )
    client = TestClient(app)

    assert client.get("/mcp").status_code == 401
    assert client.get("/mcp", headers={"x-api-token": "reader-token"}).status_code == 200
    # Healthcheck контейнера ходит без токена и обязан работать.
    assert client.get("/health").status_code == 200
    # Форма входа тоже: иначе войти из браузера неоткуда. Живая проверка
    # показала 401 там, где тесты дашборда давали 200 — они вешали маршруты
    # напрямую, без внешнего слоя.
    assert client.get("/login").status_code == 200
    # Без этой статики React-форма входа была бы пустой белой страницей.
    assert client.get("/assets/app.js").status_code == 200
    # Совпадение только точное: произвольный путь с таким префиксом не открыт.
    assert client.get("/login-extra").status_code == 401


def test_без_api_token_mcp_открыт(tmp_path, monkeypatch):
    from starlette.responses import PlainTextResponse
    from starlette.routing import Route

    from mcp1c.server import mcp_guard

    monkeypatch.delenv("API_TOKEN", raising=False)

    async def mcp(request):
        return PlainTextResponse("инструменты")

    client = TestClient(mcp_guard(Starlette(routes=[Route("/mcp", mcp, methods=["GET"])])))

    assert client.get("/mcp").status_code == 200


def test_браузер_получает_отказ_ссылкой_на_вход(tmp_path, monkeypatch):
    """JSON в адресной строке — тупик: войти неоткуда.

    Слой охраны общий для MCP и дашборда, поэтому отвечать одинаково нельзя:
    клиенту нужен JSON, человеку — страница со ссылкой на форму входа.
    Различаем по `Accept`, как это делает сам протокол MCP.
    """
    from starlette.responses import PlainTextResponse
    from starlette.routing import Route

    from mcp1c.server import mcp_guard

    monkeypatch.setenv("API_TOKEN", "reader-token")

    async def page(request):
        return PlainTextResponse("страница")

    client = TestClient(
        mcp_guard(Starlette(routes=[Route("/", page, methods=["GET"])])),
        follow_redirects=False,
    )

    браузер = client.get(
        "/sources?config=Пример",
        headers={"accept": "text/html,application/xhtml+xml"},
    )
    assert браузер.status_code == 303
    assert браузер.headers["location"] == (
        "/login?next=%2Fsources%3Fconfig%3D%25D0%259F%25D1%2580%25D0%25B8"
        "%25D0%25BC%25D0%25B5%25D1%2580"
    )

    клиент = client.get("/", headers={"accept": "application/json"})
    assert клиент.status_code == 401
    assert "X-Api-Token" in клиент.text


def test_сессия_истекает_через_ttl(tmp_path, monkeypatch):
    from mcp1c import dashboard_backend

    monkeypatch.setenv("API_TOKEN", "reader-token")
    client, _ = client_for(tmp_path)
    сейчас = dashboard_backend.time.monotonic()
    monkeypatch.setattr(dashboard_backend.time, "monotonic", lambda: сейчас)

    ответ = client.post("/login", data={"token": "reader-token"}, follow_redirects=False)
    assert f"Max-Age={dashboard_backend.SESSION_TTL_SECONDS}" in ответ.headers["set-cookie"]
    assert client.get("/api/v1/dashboard/bootstrap").status_code == 200

    monkeypatch.setattr(
        dashboard_backend.time,
        "monotonic",
        lambda: сейчас + dashboard_backend.SESSION_TTL_SECONDS + 1,
    )
    assert client.get("/api/v1/dashboard/bootstrap").status_code == 401
    assert dashboard_backend._SESSIONS == {}


def test_число_сессий_ограничено_и_вытесняет_старейшую(tmp_path, monkeypatch):
    from mcp1c import dashboard_backend

    monkeypatch.setenv("API_TOKEN", "reader-token")
    monkeypatch.setattr(dashboard_backend, "MAX_SESSIONS", 3)
    client, _ = client_for(tmp_path)

    cookies = []
    for _ in range(4):
        client.post("/login", data={"token": "reader-token"}, follow_redirects=False)
        cookies.append(client.cookies.get("mcp1c_session"))
        client.cookies.clear()

    assert len(dashboard_backend._SESSIONS) == 3
    client.cookies.set("mcp1c_session", cookies[0])
    assert client.get("/api/v1/dashboard/bootstrap").status_code == 401
    client.cookies.set("mcp1c_session", cookies[-1])
    assert client.get("/api/v1/dashboard/bootstrap").status_code == 200


def test_перебор_токена_ограничен_по_адресу(tmp_path, monkeypatch):
    from mcp1c import dashboard_backend

    monkeypatch.setenv("API_TOKEN", "reader-token")
    client, _ = client_for(tmp_path)
    сейчас = dashboard_backend.time.monotonic()
    monkeypatch.setattr(dashboard_backend.time, "monotonic", lambda: сейчас)

    # Считаются различные догадки: повтор одного токена перебором не является.
    for attempt in range(dashboard_backend.LOGIN_FAILURE_LIMIT):
        assert client.post("/login", data={"token": f"wrong-{attempt}"}).status_code == 403

    # Даже верный токен не проверяется, пока адрес исчерпал попытки:
    # иначе отказ подсказывал бы, что очередная догадка совпала.
    заблокирован = client.post(
        "/login", data={"token": "reader-token"}, follow_redirects=False
    )
    assert заблокирован.status_code == 429
    assert int(заблокирован.headers["retry-after"]) > 0
    assert dashboard_backend._SESSIONS == {}

    monkeypatch.setattr(
        dashboard_backend.time,
        "monotonic",
        lambda: сейчас + dashboard_backend.LOGIN_FAILURE_WINDOW_SECONDS + 1,
    )
    assert (
        client.post(
            "/login", data={"token": "reader-token"}, follow_redirects=False
        ).status_code
        == 303
    )


def test_https_proxy_ставит_secure_даже_при_http_схеме(tmp_path, monkeypatch):
    monkeypatch.setenv("API_TOKEN", "reader-token")
    monkeypatch.setenv("MCP1C_ACCESS", "https-proxy")
    client, _ = client_for(tmp_path)

    response = client.post(
        "/login", data={"token": "reader-token"}, follow_redirects=False
    )

    assert "; Secure" in response.headers["set-cookie"]


def test_spa_получает_csp_с_хешем_встроенного_скрипта(tmp_path, monkeypatch):
    import base64
    import hashlib

    monkeypatch.delenv("API_TOKEN", raising=False)
    client, _ = client_for(tmp_path)
    inline = "document.documentElement.dataset.theme = 'light';"
    (tmp_path / "dist" / "index.html").write_text(
        f"<!doctype html><script>{inline}</script>"
        '<script type="module" src="/assets/app.js"></script>',
        encoding="utf-8",
    )

    response = client.get("/")
    csp = response.headers["content-security-policy"]
    digest = base64.b64encode(hashlib.sha256(inline.encode()).digest()).decode()

    assert response.status_code == 200
    assert f"'sha256-{digest}'" in csp
    assert "script-src 'self'" in csp
    assert "'unsafe-inline'" not in csp.split("script-src", 1)[1].split(";", 1)[0]
    assert "frame-ancestors 'none'" in csp
    assert "object-src 'none'" in csp


def test_все_ответы_получают_защитные_заголовки():
    from starlette.responses import PlainTextResponse
    from starlette.routing import Route

    from mcp1c.server import security_headers

    async def page(request):
        return PlainTextResponse("ok")

    client = TestClient(security_headers(Starlette(routes=[Route("/", page)])))
    headers = client.get("/").headers

    assert headers["x-content-type-options"] == "nosniff"
    assert headers["x-frame-options"] == "DENY"
    assert headers["referrer-policy"] == "same-origin"


def test_сессии_чтения_не_вытесняют_сессию_администратора(tmp_path, monkeypatch):
    from mcp1c import dashboard_backend

    monkeypatch.setenv("API_TOKEN", "reader-token")
    monkeypatch.setenv("ADMIN_TOKEN", "admin-secret")
    monkeypatch.setattr(dashboard_backend, "MAX_SESSIONS", 3)
    client, _ = client_for(tmp_path)

    client.post("/login", data={"token": "admin-secret"}, follow_redirects=False)
    admin_cookie = client.cookies.get("mcp1c_session")
    client.cookies.clear()
    for _ in range(10):
        client.post("/login", data={"token": "reader-token"}, follow_redirects=False)
        client.cookies.clear()

    levels = [level for level, _ in dashboard_backend._SESSIONS.values()]
    assert levels.count(dashboard_backend.LEVEL_READ) == 3
    client.cookies.set("mcp1c_session", admin_cookie)
    assert dashboard_backend.admin_authorized(
        dashboard_backend.Request(
            {
                "type": "http",
                "headers": [(b"cookie", f"mcp1c_session={admin_cookie}".encode())],
            }
        )
    )
