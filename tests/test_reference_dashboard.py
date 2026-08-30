"""Dashboard устанавливает общую справку отдельно от источников Registry."""

from __future__ import annotations

from starlette.applications import Starlette

from conftest import живой_клиент
from mcp1c.dashboard_runtime import DASHBOARD_CLASSIC, DASHBOARD_SPA, routes
from mcp1c.reference_provider import ReferenceService
from mcp1c.registry import Registry

from reference_fixture import build_reference_database


def _client(registry: Registry, reference: ReferenceService):
    return живой_клиент(
        Starlette(
            routes=routes(
                registry,
                mode=DASHBOARD_SPA,
                reference=reference,
            )
        )
    )


def _login(client, token: str = "admin-token") -> None:
    response = client.post(
        "/login", data={"token": token}, follow_redirects=False
    )
    assert response.status_code == 303


def test_reference_status_скрывает_хеши_от_read_only(tmp_path, monkeypatch):
    monkeypatch.setenv("API_TOKEN", "read-token")
    monkeypatch.setenv("ADMIN_TOKEN", "admin-token")
    registry = Registry(tmp_path / "data")
    reference = ReferenceService.discover(registry.data_dir, allow_unsigned=True)
    client = _client(registry, reference)

    _login(client, "read-token")
    read_only = client.get("/api/v1/reference")
    client.cookies.clear()
    _login(client)
    admin = client.get("/api/v1/reference")

    assert read_only.status_code == 200
    assert read_only.json()["active"] == {
        "state": "missing",
        "ready": False,
        "message": "Каноническая база не загружена.",
    }
    assert admin.json()["active"]["signature"] == "not-checked"


def test_reference_upload_требует_admin(tmp_path, monkeypatch):
    monkeypatch.setenv("API_TOKEN", "read-token")
    monkeypatch.setenv("ADMIN_TOKEN", "admin-token")
    registry = Registry(tmp_path / "data")
    reference = ReferenceService.discover(registry.data_dir, allow_unsigned=True)
    client = _client(registry, reference)
    _login(client, "read-token")

    response = client.post(
        "/api/v1/reference/upload",
        files={"file": ("reference.sqlite3", b"synthetic")},
    )

    assert response.status_code == 403
    assert not reference.managed_path.exists()


def test_unsigned_upload_без_явного_экспериментального_режима_отклонён(
    tmp_path, monkeypatch
):
    monkeypatch.delenv("API_TOKEN", raising=False)
    monkeypatch.setenv("ADMIN_TOKEN", "admin-token")
    source = build_reference_database(tmp_path / "source.sqlite3")
    registry = Registry(tmp_path / "data")
    reference = ReferenceService.discover(registry.data_dir)
    client = _client(registry, reference)
    _login(client)

    response = client.post(
        "/api/v1/reference/upload",
        files={"file": ("reference.sqlite3", source.read_bytes())},
    )

    assert response.status_code == 422
    assert "подпис" in response.json()["error"].lower()
    assert not reference.managed_path.exists()


def test_valid_upload_остаётся_на_диске_и_активируется_после_restart(
    tmp_path, monkeypatch
):
    monkeypatch.delenv("API_TOKEN", raising=False)
    monkeypatch.setenv("ADMIN_TOKEN", "admin-token")
    source = build_reference_database(tmp_path / "source.sqlite3")
    registry = Registry(tmp_path / "data")
    reference = ReferenceService.discover(registry.data_dir, allow_unsigned=True)
    client = _client(registry, reference)
    _login(client)

    response = client.post(
        "/api/v1/reference/upload",
        files={"file": ("reference.sqlite3", source.read_bytes())},
    )

    assert response.status_code == 201
    payload = response.json()["reference"]
    assert payload["active"]["state"] == "missing"
    assert payload["pending"]["state"] == "pending_restart"
    assert reference.managed_path.is_file()
    assert reference.managed_path.stat().st_mode & 0o777 == 0o600

    restarted = ReferenceService.discover(registry.data_dir, allow_unsigned=True)
    assert restarted.status.state == "ready"
    assert restarted.status.index_cache == "hit"
    assert restarted.provider.search("образец")["results"][0]["id"] == "bsl/Example"


def test_invalid_upload_не_заменяет_прежнюю_базу(tmp_path, monkeypatch):
    monkeypatch.delenv("API_TOKEN", raising=False)
    monkeypatch.setenv("ADMIN_TOKEN", "admin-token")
    registry = Registry(tmp_path / "data")
    reference = ReferenceService.discover(registry.data_dir, allow_unsigned=True)
    reference.managed_path.parent.mkdir(parents=True)
    build_reference_database(reference.managed_path)
    original = reference.managed_path.read_bytes()
    reference = ReferenceService.discover(registry.data_dir, allow_unsigned=True)
    client = _client(registry, reference)
    _login(client)

    response = client.post(
        "/api/v1/reference/upload",
        files={"file": ("reference.sqlite3", b"not sqlite")},
    )

    assert response.status_code == 422
    assert reference.managed_path.read_bytes() == original
    assert reference.provider.search("образец")["results"][0]["id"] == "bsl/Example"


def test_external_path_делает_dashboard_upload_недоступным(tmp_path, monkeypatch):
    monkeypatch.delenv("API_TOKEN", raising=False)
    monkeypatch.setenv("ADMIN_TOKEN", "admin-token")
    external = build_reference_database(tmp_path / "external.sqlite3")
    registry = Registry(tmp_path / "data")
    reference = ReferenceService.discover(
        registry.data_dir,
        database_path=external,
        allow_unsigned=True,
    )
    client = _client(registry, reference)
    _login(client)

    response = client.post(
        "/api/v1/reference/upload",
        files={"file": ("reference.sqlite3", external.read_bytes())},
    )

    assert response.status_code == 409


def test_classic_показывает_статус_и_одну_общую_форму(tmp_path, monkeypatch):
    monkeypatch.delenv("API_TOKEN", raising=False)
    monkeypatch.setenv("ADMIN_TOKEN", "admin-token")
    registry = Registry(tmp_path / "data")
    reference = ReferenceService.discover(registry.data_dir, allow_unsigned=True)
    client = живой_клиент(
        Starlette(
            routes=routes(
                registry,
                mode=DASHBOARD_CLASSIC,
                reference=reference,
            )
        )
    )
    _login(client)

    page = client.get("/sources")

    assert page.status_code == 200
    assert "Локальная общая справка" in page.text
    assert "не загружена" in page.text
    assert "общей форме «Загрузить»" in page.text
    assert ".zip,.hbk,.json,.sqlite3" in page.text
    assert page.text.count("<input type=file") == 1


def test_classic_общая_форма_принимает_sqlite_без_javascript(tmp_path, monkeypatch):
    monkeypatch.delenv("API_TOKEN", raising=False)
    monkeypatch.setenv("ADMIN_TOKEN", "admin-token")
    source = build_reference_database(tmp_path / "source.sqlite3")
    registry = Registry(tmp_path / "data")
    reference = ReferenceService.discover(registry.data_dir, allow_unsigned=True)
    client = живой_клиент(
        Starlette(
            routes=routes(
                registry,
                mode=DASHBOARD_CLASSIC,
                reference=reference,
            )
        )
    )
    _login(client)

    response = client.post(
        "/sources",
        headers={"accept": "text/html"},
        files={"file": ("reference.sqlite3", source.read_bytes())},
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert response.headers["location"] == "/sources"
    page = client.get("/sources")
    assert "ожидает перезапуска" in page.text
