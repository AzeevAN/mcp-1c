"""Dashboard устанавливает общую справку отдельно от источников Registry."""

from __future__ import annotations

from starlette.applications import Starlette

from mcp1c.process_restart import RestartController

from conftest import живой_клиент
from mcp1c.dashboard_runtime import DASHBOARD_CLASSIC, DASHBOARD_SPA, routes
from mcp1c.reference_provider import ReferenceService
from mcp1c.registry import Registry

from reference_fixture import SyntheticReferenceSigner, build_reference_database


def _trusted_signer(monkeypatch) -> SyntheticReferenceSigner:
    signer = SyntheticReferenceSigner.generate()
    monkeypatch.setattr(
        "mcp1c.reference_provider.TRUSTED_REFERENCE_PUBLIC_KEYS",
        signer.verifier().public_keys,
    )
    return signer


def _artifact(tmp_path, source, signer, name="candidate.mcp1cref"):
    return signer.build(tmp_path / name, source)


def _installed_reference(registry, tmp_path, monkeypatch):
    signer = _trusted_signer(monkeypatch)
    source = build_reference_database(tmp_path / "source.sqlite3")
    signer.build(registry.data_dir / "reference" / "reference.mcp1cref", source)
    return ReferenceService.discover(registry.data_dir), signer, source


def _client(
    registry: Registry,
    reference: ReferenceService,
    restart: RestartController | None = None,
):
    return живой_клиент(
        Starlette(
            routes=routes(
                registry,
                mode=DASHBOARD_SPA,
                reference=reference,
                restart=restart,
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
    reference = ReferenceService.discover(registry.data_dir)
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
    reference = ReferenceService.discover(registry.data_dir)
    client = _client(registry, reference)
    _login(client, "read-token")

    response = client.post(
        "/api/v1/reference/upload",
        files={"file": ("reference.mcp1cref", b"synthetic")},
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
        files={"file": ("reference.mcp1cref", source.read_bytes())},
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
    signer = _trusted_signer(monkeypatch)
    artifact = _artifact(tmp_path, source, signer)
    registry = Registry(tmp_path / "data")
    reference = ReferenceService.discover(registry.data_dir)
    client = _client(registry, reference)
    _login(client)

    response = client.post(
        "/api/v1/reference/upload",
        files={"file": ("reference.mcp1cref", artifact.read_bytes())},
    )

    assert response.status_code == 201
    payload = response.json()["reference"]
    assert payload["active"]["state"] == "missing"
    assert payload["pending"]["state"] == "pending_restart"
    assert reference.managed_path.is_file()
    assert reference.managed_path.stat().st_mode & 0o777 == 0o600

    restarted = ReferenceService.discover(registry.data_dir)
    assert restarted.status.state == "ready"
    assert restarted.status.index_cache == "hit"
    assert restarted.provider.search("образец")["results"][0]["id"] == "bsl/Example"


def test_invalid_upload_не_заменяет_прежнюю_базу(tmp_path, monkeypatch):
    monkeypatch.delenv("API_TOKEN", raising=False)
    monkeypatch.setenv("ADMIN_TOKEN", "admin-token")
    signer = _trusted_signer(monkeypatch)
    registry = Registry(tmp_path / "data")
    source = build_reference_database(tmp_path / "source.sqlite3")
    managed = signer.build(
        registry.data_dir / "reference" / "reference.mcp1cref", source
    )
    original = managed.read_bytes()
    reference = ReferenceService.discover(registry.data_dir)
    client = _client(registry, reference)
    _login(client)

    response = client.post(
        "/api/v1/reference/upload",
        files={"file": ("reference.mcp1cref", b"not signed bundle")},
    )

    assert response.status_code == 422
    assert reference.managed_path.read_bytes() == original
    assert reference.provider.search("образец")["results"][0]["id"] == "bsl/Example"


def test_external_path_делает_dashboard_upload_недоступным(tmp_path, monkeypatch):
    monkeypatch.delenv("API_TOKEN", raising=False)
    monkeypatch.setenv("ADMIN_TOKEN", "admin-token")
    signer = _trusted_signer(monkeypatch)
    database = build_reference_database(tmp_path / "external.sqlite3")
    external = signer.build(tmp_path / "external.mcp1cref", database)
    registry = Registry(tmp_path / "data")
    reference = ReferenceService.discover(
        registry.data_dir,
        database_path=external,
    )
    client = _client(registry, reference)
    _login(client)

    response = client.post(
        "/api/v1/reference/upload",
        files={"file": ("reference.mcp1cref", external.read_bytes())},
    )

    assert response.status_code == 409


def test_delete_управляемой_базы_требует_admin(tmp_path, monkeypatch):
    monkeypatch.setenv("API_TOKEN", "read-token")
    monkeypatch.setenv("ADMIN_TOKEN", "admin-token")
    registry = Registry(tmp_path / "data")
    reference, _, _ = _installed_reference(registry, tmp_path, monkeypatch)
    client = _client(registry, reference)
    _login(client, "read-token")

    response = client.post(
        "/api/v1/reference/remove",
        json={"confirmation": "reference.mcp1cref"},
    )

    assert response.status_code == 403
    assert reference.managed_path.is_file()


def test_delete_оставляет_активные_ручки_до_restart(tmp_path, monkeypatch):
    monkeypatch.delenv("API_TOKEN", raising=False)
    monkeypatch.setenv("ADMIN_TOKEN", "admin-token")
    registry = Registry(tmp_path / "data")
    reference, _, _ = _installed_reference(registry, tmp_path, monkeypatch)
    client = _client(registry, reference)
    _login(client)

    response = client.post(
        "/api/v1/reference/remove",
        json={"confirmation": "reference.mcp1cref"},
    )

    assert response.status_code == 200
    payload = response.json()["reference"]
    assert payload["active"]["state"] == "ready"
    assert payload["pending"]["state"] == "pending_restart"
    assert payload["pending"]["action"] == "remove"
    assert payload["managed_file_present"] is False
    assert reference.provider.search("образец")["results"][0]["id"] == "bsl/Example"

    restarted = ReferenceService.discover(registry.data_dir)
    assert restarted.status.state == "missing"


def test_delete_требует_точное_подтверждение(tmp_path, monkeypatch):
    monkeypatch.delenv("API_TOKEN", raising=False)
    monkeypatch.setenv("ADMIN_TOKEN", "admin-token")
    registry = Registry(tmp_path / "data")
    reference, _, _ = _installed_reference(registry, tmp_path, monkeypatch)
    client = _client(registry, reference)
    _login(client)

    response = client.post(
        "/api/v1/reference/remove",
        json={"confirmation": "delete"},
    )

    assert response.status_code == 400
    assert reference.managed_path.is_file()


def test_delete_внешней_базы_из_dashboard_запрещён(tmp_path, monkeypatch):
    monkeypatch.delenv("API_TOKEN", raising=False)
    monkeypatch.setenv("ADMIN_TOKEN", "admin-token")
    signer = _trusted_signer(monkeypatch)
    database = build_reference_database(tmp_path / "external.sqlite3")
    external = signer.build(tmp_path / "external.mcp1cref", database)
    registry = Registry(tmp_path / "data")
    reference = ReferenceService.discover(
        registry.data_dir,
        database_path=external,
    )
    client = _client(registry, reference)
    _login(client)

    response = client.post(
        "/api/v1/reference/remove",
        json={"confirmation": "reference.mcp1cref"},
    )

    assert response.status_code == 409
    assert external.is_file()


def test_restart_выключен_без_явной_возможности(tmp_path, monkeypatch):
    monkeypatch.delenv("API_TOKEN", raising=False)
    monkeypatch.setenv("ADMIN_TOKEN", "admin-token")
    source = build_reference_database(tmp_path / "source.sqlite3")
    signer = _trusted_signer(monkeypatch)
    artifact = _artifact(tmp_path, source, signer)
    registry = Registry(tmp_path / "data")
    reference = ReferenceService.discover(registry.data_dir)
    client = _client(registry, reference)
    _login(client)
    client.post(
        "/api/v1/reference/upload",
        files={"file": ("reference.mcp1cref", artifact.read_bytes())},
    )

    response = client.post("/api/v1/server/restart", json={})

    assert response.status_code == 404


def test_restart_разрешён_только_для_pending_reference(tmp_path, monkeypatch):
    monkeypatch.delenv("API_TOKEN", raising=False)
    monkeypatch.setenv("ADMIN_TOKEN", "admin-token")
    registry = Registry(tmp_path / "data")
    reference = ReferenceService.discover(registry.data_dir)
    restart = RestartController(enabled=True, terminate=lambda: None, delay=0)
    client = _client(registry, reference, restart)
    _login(client)

    response = client.post("/api/v1/server/restart", json={})

    assert response.status_code == 409
    assert restart.requested is False


def test_restart_отвечает_до_завершения_процесса(tmp_path, monkeypatch):
    from threading import Event

    monkeypatch.delenv("API_TOKEN", raising=False)
    monkeypatch.setenv("ADMIN_TOKEN", "admin-token")
    source = build_reference_database(tmp_path / "source.sqlite3")
    signer = _trusted_signer(monkeypatch)
    artifact = _artifact(tmp_path, source, signer)
    registry = Registry(tmp_path / "data")
    reference = ReferenceService.discover(registry.data_dir)
    terminated = Event()
    restart = RestartController(enabled=True, terminate=terminated.set, delay=0)
    client = _client(registry, reference, restart)
    _login(client)
    uploaded = client.post(
        "/api/v1/reference/upload",
        files={"file": ("reference.mcp1cref", artifact.read_bytes())},
    )
    assert uploaded.status_code == 201

    response = client.post("/api/v1/server/restart", json={})

    assert response.status_code == 202
    assert response.json() == {
        "state": "restarting",
        "runtime_id": restart.runtime_id,
    }
    assert terminated.wait(1)
    assert restart.requested is True


def test_restart_требует_admin_и_same_origin(tmp_path, monkeypatch):
    monkeypatch.setenv("API_TOKEN", "read-token")
    monkeypatch.setenv("ADMIN_TOKEN", "admin-token")
    source = build_reference_database(tmp_path / "source.sqlite3")
    signer = _trusted_signer(monkeypatch)
    artifact = _artifact(tmp_path, source, signer)
    registry = Registry(tmp_path / "data")
    reference = ReferenceService.discover(registry.data_dir)
    restart = RestartController(enabled=True, terminate=lambda: None, delay=0)
    client = _client(registry, reference, restart)
    _login(client, "read-token")
    reference.install_candidate(artifact)

    read_only = client.post("/api/v1/server/restart", json={})
    client.cookies.clear()
    _login(client)
    foreign_origin = client.post(
        "/api/v1/server/restart",
        json={},
        headers={"origin": "http://sibling.test"},
    )

    assert read_only.status_code == 403
    assert foreign_origin.status_code == 403
    assert restart.requested is False


def test_повторный_restart_не_планируется_дважды(tmp_path, monkeypatch):
    monkeypatch.delenv("API_TOKEN", raising=False)
    monkeypatch.setenv("ADMIN_TOKEN", "admin-token")
    source = build_reference_database(tmp_path / "source.sqlite3")
    signer = _trusted_signer(monkeypatch)
    artifact = _artifact(tmp_path, source, signer)
    registry = Registry(tmp_path / "data")
    reference = ReferenceService.discover(registry.data_dir)
    restart = RestartController(enabled=True, terminate=lambda: None, delay=60)
    client = _client(registry, reference, restart)
    _login(client)
    reference.install_candidate(artifact)

    first = client.post("/api/v1/server/restart", json={})
    second = client.post("/api/v1/server/restart", json={})

    assert first.status_code == 202
    assert second.status_code == 409


def test_classic_показывает_статус_и_одну_общую_форму(tmp_path, monkeypatch):
    monkeypatch.delenv("API_TOKEN", raising=False)
    monkeypatch.setenv("ADMIN_TOKEN", "admin-token")
    registry = Registry(tmp_path / "data")
    reference = ReferenceService.discover(registry.data_dir)
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
    assert ".zip,.hbk,.json,.mcp1cref" in page.text
    assert page.text.count("<input type=file") == 1


def test_classic_общая_форма_принимает_подписанный_bundle_без_javascript(
    tmp_path, monkeypatch
):
    monkeypatch.delenv("API_TOKEN", raising=False)
    monkeypatch.setenv("ADMIN_TOKEN", "admin-token")
    source = build_reference_database(tmp_path / "source.sqlite3")
    signer = _trusted_signer(monkeypatch)
    artifact = _artifact(tmp_path, source, signer)
    registry = Registry(tmp_path / "data")
    reference = ReferenceService.discover(registry.data_dir)
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
        files={"file": ("reference.mcp1cref", artifact.read_bytes())},
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert response.headers["location"] == "/sources"
    page = client.get("/sources")
    assert "ожидает перезапуска" in page.text


def test_classic_удаляет_базу_и_предлагает_restart(tmp_path, monkeypatch):
    monkeypatch.delenv("API_TOKEN", raising=False)
    monkeypatch.setenv("ADMIN_TOKEN", "admin-token")
    registry = Registry(tmp_path / "data")
    reference, _, _ = _installed_reference(registry, tmp_path, monkeypatch)
    restart = RestartController(enabled=True, terminate=lambda: None, delay=60)
    client = живой_клиент(
        Starlette(
            routes=routes(
                registry,
                mode=DASHBOARD_CLASSIC,
                reference=reference,
                restart=restart,
            )
        )
    )
    _login(client)

    response = client.post(
        "/api/v1/reference/remove",
        headers={"accept": "text/html"},
        data={"confirmation": "reference.mcp1cref"},
        follow_redirects=False,
    )

    assert response.status_code == 303
    page = client.get("/sources")
    assert "База удалена и будет отключена" in page.text
    assert "Перезапустить сервер и применить изменение" in page.text
