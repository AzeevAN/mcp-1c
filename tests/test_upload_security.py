"""Границы multipart-загрузки и cookie-authenticated записей."""

from __future__ import annotations

from io import BytesIO

import pytest
from starlette.applications import Starlette
from starlette import formparsers
from starlette.testclient import TestClient

from mcp1c import dashboard_backend as dashboard
from mcp1c.dashboard_runtime import DASHBOARD_ON, routes
from mcp1c.registry import Registry


BASE_URL = "https://mcp.example.test"
SAME_ORIGIN = {"origin": BASE_URL}


def client_for(tmp_path, *, same_origin: bool = True) -> tuple[TestClient, Registry]:
    registry = Registry(tmp_path / "data")
    headers = SAME_ORIGIN if same_origin else None
    client = TestClient(
        Starlette(routes=routes(registry, mode=DASHBOARD_ON)),
        base_url=BASE_URL,
        headers=headers,
    )
    return client, registry


def login(client: TestClient) -> None:
    response = client.post(
        "/login", data={"token": "admin-token"}, follow_redirects=False
    )
    assert response.status_code == 303


def test_multipart_отвергает_второй_file_part(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.setenv("ADMIN_TOKEN", "admin-token")
    client, registry = client_for(tmp_path)
    login(client)
    monkeypatch.setattr(
        dashboard,
        "_start_job",
        lambda *args, **kwargs: pytest.fail("обработчик не должен запускаться"),
    )

    response = client.post(
        "/api/v1/sources/upload",
        files=[
            ("file", ("выгрузка.zip", b"first")),
            ("extra", ("добавка.zip", b"second")),
        ],
    )

    assert response.status_code == 400
    assert registry.snapshot().configuration_names == ()


def test_multipart_останавливает_file_part_до_запуска_обработчика(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.setenv("ADMIN_TOKEN", "admin-token")
    monkeypatch.setattr(dashboard, "MAX_UPLOAD", 8)
    client, registry = client_for(tmp_path)
    login(client)

    class _TrackedTemporary(BytesIO):
        _rolled = False

        def __init__(self) -> None:
            super().__init__()
            self.max_written = 0

        def write(self, value: bytes) -> int:
            written = super().write(value)
            self.max_written = max(self.max_written, self.tell())
            return written

    временные: list[_TrackedTemporary] = []

    def temporary_file(*args, **kwargs) -> _TrackedTemporary:
        result = _TrackedTemporary()
        временные.append(result)
        return result

    monkeypatch.setattr(formparsers, "SpooledTemporaryFile", temporary_file)
    monkeypatch.setattr(
        dashboard,
        "_start_job",
        lambda *args, **kwargs: pytest.fail("файл уже превысил temp-лимит"),
    )

    response = client.post(
        "/api/v1/sources/upload",
        files={"file": ("выгрузка.zip", b"123456789")},
    )

    assert response.status_code == 413
    assert временные
    assert max(item.max_written for item in временные) <= dashboard.MAX_UPLOAD
    assert registry.snapshot().configuration_names == ()


_MUTATIONS = (
    ("/api/v1/sources/upload", {"files": {"file": ("выгрузка.zip", b"zip")}}),
    (
        "/api/v1/sources/intake/upload",
        {"files": {"file": ("выгрузка.zip", b"zip")}},
    ),
    (
        "/api/v1/sources/intake/start",
        {"json": {"candidate_id": "candidate", "action": "create"}},
    ),
    (
        "/api/v1/sources/intake/confirm",
        {"json": {"job_id": "job"}},
    ),
    ("/api/v1/sources/remove", {"json": {"id": "СинтетическаяКонфигурация"}}),
    ("/api/v1/sources/forget", {"json": {"path": "sources/test.zip"}}),
    ("/api/v1/sources/jobs/clear", {}),
    ("/api/v1/sources/incoming/parse", {"json": {"name": "выгрузка.zip"}}),
    (
        "/api/v1/dictionary/aliases",
        {"json": {"phrase": "проверка", "targets": ["Справочник.Объекты"]}},
    ),
    (
        "/api/v1/dictionary/aliases/remove",
        {"json": {"phrase": "проверка"}},
    ),
    (
        "/api/v1/dictionary/synonyms",
        {"json": {"words": ["проверка", "проверять"]}},
    ),
    (
        "/api/v1/dictionary/synonyms/remove",
        {"json": {"words": ["проверка", "проверять"]}},
    ),
    ("/logout", {}),
)


@pytest.mark.parametrize(("path", "kwargs"), _MUTATIONS)
def test_cookie_mutation_отвергает_чужой_origin(
    tmp_path, monkeypatch, path: str, kwargs: dict
) -> None:
    monkeypatch.setenv("ADMIN_TOKEN", "admin-token")
    client, registry = client_for(tmp_path)
    login(client)

    response = client.post(
        path,
        headers={"origin": "https://sibling.example.test"},
        follow_redirects=False,
        **kwargs,
    )

    assert response.status_code == 403
    assert registry.dictionary.aliases == {}
    assert registry.dictionary.synonym_groups == []


def test_cookie_mutation_без_origin_и_referer_отклоняется(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.setenv("ADMIN_TOKEN", "admin-token")
    client, registry = client_for(tmp_path, same_origin=False)
    login(client)

    response = client.post(
        "/api/v1/dictionary/aliases",
        json={"phrase": "проверка", "targets": ["Справочник.Объекты"]},
        follow_redirects=False,
    )

    assert response.status_code == 403
    assert registry.dictionary.aliases == {}


def test_cookie_mutation_принимает_same_origin(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("ADMIN_TOKEN", "admin-token")
    client, registry = client_for(tmp_path)
    login(client)

    response = client.post(
        "/api/v1/dictionary/aliases",
        json={"phrase": "проверка", "targets": ["Справочник.Объекты"]},
        follow_redirects=False,
    )

    assert response.status_code == 200
    assert registry.dictionary.aliases_for(None, with_builtin=False)["проверка"] == [
        "Справочник.Объекты"
    ]


def test_cookie_mutation_принимает_same_origin_referer(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.setenv("ADMIN_TOKEN", "admin-token")
    client, registry = client_for(tmp_path, same_origin=False)
    login(client)

    response = client.post(
        "/api/v1/dictionary/aliases",
        headers={"referer": f"{BASE_URL}/dictionary"},
        json={"phrase": "проверка", "targets": ["Справочник.Объекты"]},
        follow_redirects=False,
    )

    assert response.status_code == 200
    assert registry.dictionary.aliases_for(None, with_builtin=False)["проверка"] == [
        "Справочник.Объекты"
    ]


def test_явный_admin_token_не_зависит_от_cookie_csrf(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.setenv("ADMIN_TOKEN", "admin-token")
    client, registry = client_for(tmp_path)
    login(client)

    response = client.post(
        "/api/v1/dictionary/aliases",
        headers={
            "origin": "https://sibling.example.test",
            "x-api-token": "admin-token",
        },
        json={"phrase": "проверка", "targets": ["Справочник.Объекты"]},
        follow_redirects=False,
    )

    assert response.status_code == 200
    assert registry.dictionary.aliases_for(None, with_builtin=False)["проверка"] == [
        "Справочник.Объекты"
    ]
