"""Dashboard сохраняет capability-настройки, применяемые полным restart."""

from __future__ import annotations

import pytest
from starlette.applications import Starlette

from conftest import живой_клиент
from mcp1c.capabilities import CapabilityRuntime, CapabilitySettingsStore
from mcp1c.dashboard_runtime import DASHBOARD_ON, routes
from mcp1c.process_restart import RestartController
from mcp1c.reference_provider import ReferenceService
from mcp1c.registry import Registry


def _client(tmp_path, *, active=(), terminate=lambda: None):
    registry = Registry(tmp_path / "data")
    store = CapabilitySettingsStore(registry.data_dir, fallback=active)
    capabilities = CapabilityRuntime(store, active=active)
    reference = ReferenceService.discover(registry.data_dir)
    restart = RestartController(enabled=True, terminate=terminate, delay=0)
    client = живой_клиент(
        Starlette(
            routes=routes(
                registry,
                mode=DASHBOARD_ON,
                reference=reference,
                restart=restart,
                capabilities=capabilities,
            )
        )
    )
    return client, store, restart


def _login(client, token="admin-token"):
    response = client.post("/login", data={"token": token}, follow_redirects=False)
    assert response.status_code == 303


def test_status_различает_active_desired_и_pending_restart(tmp_path, monkeypatch):
    monkeypatch.delenv("API_TOKEN", raising=False)
    monkeypatch.setenv("ADMIN_TOKEN", "admin-token")
    client, store, _restart = _client(tmp_path)
    _login(client)

    store.save(("diagnostics",))
    status = client.get("/api/v1/capabilities")

    assert status.status_code == 200
    assert status.json() == {
        "available": ["diagnostics"],
        "active": [],
        "desired": ["diagnostics"],
        "pending_restart": True,
    }
    assert store.load() == ("diagnostics",)


def test_capability_pending_разрешает_существующий_full_restart(
    tmp_path,
    monkeypatch,
):
    from threading import Event

    monkeypatch.delenv("API_TOKEN", raising=False)
    monkeypatch.setenv("ADMIN_TOKEN", "admin-token")
    terminated = Event()
    client, store, restart = _client(tmp_path, terminate=terminated.set)
    _login(client)
    store.save(("diagnostics",))

    response = client.post("/api/v1/server/restart", json={})

    assert response.status_code == 202
    assert response.json() == {
        "state": "restarting",
        "runtime_id": restart.runtime_id,
        "reasons": ["capabilities"],
    }
    assert terminated.wait(1)


def test_capability_status_требует_admin(
    tmp_path,
    monkeypatch,
):
    monkeypatch.setenv("API_TOKEN", "read-token")
    monkeypatch.setenv("ADMIN_TOKEN", "admin-token")
    client, _store, _restart = _client(tmp_path)
    _login(client, "read-token")
    read_only = client.get("/api/v1/capabilities")

    assert read_only.status_code == 403


def test_baseline_не_публикует_mutation_api(tmp_path, monkeypatch):
    monkeypatch.delenv("API_TOKEN", raising=False)
    monkeypatch.setenv("ADMIN_TOKEN", "admin-token")
    client, store, _restart = _client(tmp_path)
    _login(client)

    response = client.put(
        "/api/v1/capabilities",
        json={"enabled": ["diagnostics"]},
    )

    assert response.status_code == 405
    assert store.path.exists() is False


def test_повреждённые_settings_не_разрешают_restart(tmp_path, monkeypatch):
    monkeypatch.delenv("API_TOKEN", raising=False)
    monkeypatch.setenv("ADMIN_TOKEN", "admin-token")
    client, store, restart = _client(tmp_path)
    _login(client)
    store.path.parent.mkdir(parents=True, exist_ok=True)
    store.path.write_text("{}", encoding="utf-8")

    status = client.get("/api/v1/capabilities")
    response = client.post("/api/v1/server/restart", json={})

    assert status.status_code == 409
    assert response.status_code == 409
    assert restart.requested is False


def test_restart_читает_settings_вне_event_loop(tmp_path, monkeypatch):
    import asyncio

    monkeypatch.delenv("API_TOKEN", raising=False)
    monkeypatch.setenv("ADMIN_TOKEN", "admin-token")
    client, _store, _restart = _client(tmp_path)
    _login(client)

    def pending_restart(_self):
        with pytest.raises(RuntimeError, match="no running event loop"):
            asyncio.get_running_loop()
        return True

    monkeypatch.setattr(CapabilityRuntime, "pending_restart", pending_restart)

    response = client.post("/api/v1/server/restart", json={})

    assert response.status_code == 202
