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

from starlette.applications import Starlette
from starlette.testclient import TestClient

from mcp1c import dashboard
from mcp1c.registry import Registry

from conftest import build_configuration, write_export


def client_for(tmp_path, **client_options) -> tuple[TestClient, Registry]:
    data_dir = tmp_path / "data"
    incoming = tmp_path / "incoming"
    data_dir.mkdir()
    incoming.mkdir()
    registry = Registry(data_dir)
    registry.add_configuration(write_export(incoming, build_configuration()))
    app = Starlette(routes=dashboard.routes(registry))
    return TestClient(app, **client_options), registry


def test_без_api_token_чтение_открыто(tmp_path, monkeypatch):
    """Обратная совместимость: не задал — работает как раньше."""
    monkeypatch.delenv("API_TOKEN", raising=False)
    client, _ = client_for(tmp_path)

    assert client.get("/").status_code == 200
    assert "ТестоваяКонфигурация" in client.get("/").text


def test_с_api_token_чтение_закрыто(tmp_path, monkeypatch):
    monkeypatch.setenv("API_TOKEN", "reader-token")
    client, _ = client_for(tmp_path)

    response = client.get("/")

    assert response.status_code == 401
    # Имя конфигурации — уже сведение о клиенте, его быть не должно.
    assert "ТестоваяКонфигурация" not in response.text


def test_токен_чтения_в_заголовке_открывает_доступ(tmp_path, monkeypatch):
    monkeypatch.setenv("API_TOKEN", "reader-token")
    client, _ = client_for(tmp_path)

    response = client.get("/", headers={"x-api-token": "reader-token"})

    assert response.status_code == 200
    assert "ТестоваяКонфигурация" in response.text


def test_bearer_тоже_принимается(tmp_path, monkeypatch):
    """MCP-клиенты передают токен через `Authorization`."""
    monkeypatch.setenv("API_TOKEN", "reader-token")
    client, _ = client_for(tmp_path)

    response = client.get("/", headers={"authorization": "Bearer reader-token"})

    assert response.status_code == 200


def test_неверный_токен_чтения_не_пускает(tmp_path, monkeypatch):
    monkeypatch.setenv("API_TOKEN", "reader-token")
    client, _ = client_for(tmp_path)

    assert client.get("/", headers={"x-api-token": "wrong-token"}).status_code == 401


def test_вход_по_токену_чтения_даёт_сессию_без_прав_записи(tmp_path, monkeypatch):
    """Браузеру нужна сессия: заголовок в адресную строку не вписать."""
    monkeypatch.setenv("API_TOKEN", "reader-token")
    monkeypatch.setenv("ADMIN_TOKEN", "admin-token")
    client, registry = client_for(tmp_path)

    client.post("/login", data={"token": "reader-token"})

    assert client.get("/").status_code == 200
    # Читатель видит страницы, но не формы правки.
    assert "Завести псевдоним" not in client.get("/dictionary").text
    отказ = client.post(
        "/dictionary/alias",
        data={"phrase": "склад", "targets": "Справочник.Контрагенты"},
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

    assert client.get("/").status_code == 200
    ответ = client.post(
        "/dictionary/alias",
        data={"phrase": "склад", "targets": "Справочник.Контрагенты"},
        follow_redirects=False,
    )
    assert ответ.status_code == 303


def test_админский_токен_работает_и_как_токен_чтения(tmp_path, monkeypatch):
    """Иначе владельцу пришлось бы держать два заголовка вместо одного."""
    monkeypatch.setenv("API_TOKEN", "reader-token")
    monkeypatch.setenv("ADMIN_TOKEN", "admin-token")
    client, _ = client_for(tmp_path)

    assert client.get("/", headers={"x-api-token": "admin-token"}).status_code == 200


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
    assert client.get("/").status_code == 401
    client.post("/login", data={"token": "секрет"})
    assert client.get("/").status_code == 200


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

    app = mcp_guard(
        Starlette(routes=[Route("/mcp", mcp, methods=["GET"]),
                          Route("/health", health, methods=["GET"]),
                          Route("/login", health, methods=["GET"])])
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

    браузер = client.get("/", headers={"accept": "text/html,application/xhtml+xml"})
    assert браузер.status_code == 303
    assert браузер.headers["location"] == "/login"

    клиент = client.get("/", headers={"accept": "application/json"})
    assert клиент.status_code == 401
    assert "X-Api-Token" in клиент.text
