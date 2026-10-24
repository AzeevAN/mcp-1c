"""Каждый JSON-маршрут дашборда закрыт для анонима, когда токены заданы.

Проверка доступа объявляется декоратором (`read_endpoint`/`admin_endpoint`).
Тест обходит весь список маршрутов, а не выборку: новый маршрут, забывший
декоратор, ловится здесь, а не в проде.
"""

from __future__ import annotations

import ast
import inspect
import re
import textwrap

import pytest
from starlette.applications import Starlette
from starlette.routing import Route
from starlette.testclient import TestClient

from mcp1c.dashboard_runtime import DASHBOARD_ON, routes
from mcp1c.registry import Registry

from conftest import build_configuration, write_export

# Сознательно открытые без токена точки.
_PUBLIC: set[str] = set()


def _app(tmp_path) -> tuple[Starlette, list[Route]]:
    data_dir = tmp_path / "data"
    incoming = tmp_path / "incoming"
    data_dir.mkdir()
    incoming.mkdir()
    registry = Registry(data_dir)
    registry.add_configuration(write_export(incoming, build_configuration()))
    static_dir = tmp_path / "dist"
    static_dir.mkdir()
    (static_dir / "index.html").write_text("<!doctype html>", encoding="utf-8")
    table = routes(registry, mode=DASHBOARD_ON, static_dir=static_dir)
    return Starlette(routes=table), table


def _concrete(path: str) -> str:
    return re.sub(r"\{[^}]+\}", "x", path)


def test_аноним_не_получает_ответ_ни_одного_json_маршрута(tmp_path, monkeypatch):
    monkeypatch.setenv("API_TOKEN", "reader-token")
    monkeypatch.setenv("ADMIN_TOKEN", "admin-token")
    app, table = _app(tmp_path)
    client = TestClient(app, headers={"origin": "http://testserver"})
    checked = []
    for route in table:
        if not isinstance(route, Route) or not route.path.startswith("/api/"):
            continue
        if route.path in _PUBLIC:
            continue
        for method in sorted((route.methods or {"GET"}) - {"HEAD"}):
            response = client.request(method, _concrete(route.path), json={})
            assert response.status_code in (401, 403), (
                method,
                route.path,
                response.status_code,
            )
            assert "error" in response.json(), (method, route.path)
            checked.append((method, route.path))
    assert len(checked) >= 30, checked


@pytest.mark.parametrize(
    "path",
    ["/api/v1/dashboard/bootstrap", "/api/v1/sources"],
)
def test_токен_чтения_проходит_декоратор(tmp_path, monkeypatch, path):
    monkeypatch.setenv("API_TOKEN", "reader-token")
    app, _ = _app(tmp_path)
    client = TestClient(app)

    response = client.get(path, headers={"x-api-token": "reader-token"})

    assert response.status_code == 200




@pytest.mark.parametrize("operation", ["set", "clear"])
def test_platform_mutation_без_admin_token_выключена(tmp_path, monkeypatch, operation):
    monkeypatch.setenv("API_TOKEN", "reader-token")
    monkeypatch.delenv("ADMIN_TOKEN", raising=False)
    app, _table = _app(tmp_path)
    body = {"configuration": "ТестоваяКонфигурация", "platform_version": "8.3.27.2130"}

    with TestClient(app, headers={"origin": "http://testserver"}) as client:
        response = client.post(
            f"/api/v1/sources/platform/{operation}", json=body,
            headers={"x-api-token": "reader-token"},
        )

    assert response.status_code == 404
    assert "ADMIN_TOKEN" in response.json()["error"]


@pytest.mark.parametrize("operation", ["set", "clear"])
def test_platform_mutation_чужой_origin_совпадает_с_admin_guard(tmp_path, monkeypatch, operation):
    monkeypatch.setenv("API_TOKEN", "reader-token")
    admin = "Zx4Pk8Lw2Ns6Hd9Vb3Tm7Rq1Gy5Cf0Ja8Ue2"
    monkeypatch.setenv("ADMIN_TOKEN", admin)
    app, _table = _app(tmp_path)

    with TestClient(app, headers={"origin": "http://testserver"}) as client:
        login = client.post("/login", data={"token": admin}, follow_redirects=False)
        assert login.status_code == 303
        foreign = {"origin": "http://sibling.test"}
        reference = client.post(
            "/api/v1/sources/remove", headers=foreign,
            json={"id": "ТестоваяКонфигурация", "confirmation": "ТестоваяКонфигурация"},
        )
        response = client.post(
            f"/api/v1/sources/platform/{operation}", headers=foreign,
            json={"configuration": "ТестоваяКонфигурация", "platform_version": "8.3.27.2130"},
        )

    assert reference.status_code == 403
    assert reference.json()["error"]
    assert response.status_code == 403
    assert response.headers["content-type"] == reference.headers["content-type"]
    assert response.content == reference.content


_I1_MUTATION_EXCEPTIONS = {
    ("/api/v1/queries", "POST", "dashboard_queries_run", "queries_run_api"):
        "Поиск не меняет состояние; доступ чтения объявлен read_endpoint.",
    ("/login", "POST", "dashboard_login", "login"):
        "Вход отдельно проверяет чужой сайт и токен до создания сессии.",
    ("/logout", "POST", "dashboard_logout", "logout"):
        "Выход закрывает сессию с явным csrf_denied.",
}


def _i1_ast_name(node):
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    return None


def test_все_mutation_routes_имеют_admin_guard_или_актуальное_исключение(tmp_path):
    _, table = _app(tmp_path)

    def calls(function, name):
        return any(isinstance(node, ast.Call) and _i1_ast_name(node.func) == name
                   for node in ast.walk(function))

    checked, used_exceptions, unguarded = [], set(), []
    for route in table:
        if not isinstance(route, Route):
            continue
        mutations = (route.methods or {"GET"}) & {"POST", "PUT", "PATCH", "DELETE"}
        if not mutations:
            continue
        original_endpoint = inspect.unwrap(route.endpoint)
        tree = ast.parse(textwrap.dedent(inspect.getsource(original_endpoint)))
        definitions = [node for node in tree.body
                       if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))]
        assert len(definitions) == 1, f"Неоднозначный endpoint {route.path}: {route.name}"
        endpoint = definitions[0]
        guarded = any(
            isinstance(decorator, ast.Call) and _i1_ast_name(decorator.func) == "admin_endpoint"
            and any(keyword.arg == "mutation" and isinstance(keyword.value, ast.Constant)
                    and keyword.value.value is True for keyword in decorator.keywords)
            for decorator in endpoint.decorator_list
        )
        for method in sorted(mutations):
            identity = (route.path, method, route.name, original_endpoint.__name__)
            checked.append(identity)
            if identity in _I1_MUTATION_EXCEPTIONS:
                used_exceptions.add(identity)
                if endpoint.name == "queries_run_api":
                    assert any(_i1_ast_name(item) == "read_endpoint" for item in endpoint.decorator_list)
                elif endpoint.name == "login":
                    assert calls(endpoint, "login_from_foreign_site")
                    assert calls(endpoint, "check_token")
                else:
                    assert calls(endpoint, "csrf_denied")
            elif not guarded:
                unguarded.append(identity)

    assert checked, "Таблица маршрутов не содержит ни одного изменяющего HTTP-метода"
    assert used_exceptions == set(_I1_MUTATION_EXCEPTIONS), "Список исключений устарел"
    assert not unguarded, f"Мутации без admin_endpoint(mutation=True): {unguarded}"
