"""Dashboard сохраняет capability-настройки, применяемые полным restart."""

from __future__ import annotations

import json
import os
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

import pytest
from starlette.applications import Starlette

from conftest import живой_клиент
from mcp1c.capabilities import CapabilityRuntime, CapabilitySettingsStore
from mcp1c.dashboard_runtime import DASHBOARD_ON, routes
from mcp1c.process_restart import RestartController
from mcp1c.reference_provider import ReferenceService
from mcp1c.registry import Registry


def _client(
    tmp_path,
    *,
    active=(),
    terminate=lambda: None,
    restart_enabled=True,
):
    registry = Registry(tmp_path / "data")
    store = CapabilitySettingsStore(registry.data_dir, fallback=active)
    capabilities = CapabilityRuntime(store, active=active)
    reference = ReferenceService.discover(registry.data_dir)
    restart = RestartController(
        enabled=restart_enabled,
        terminate=terminate,
        delay=0,
    )
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
        "runtime": {"self_restart": True},
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


def test_mutation_сохраняет_desired_и_возвращает_status(
    tmp_path,
    monkeypatch,
):
    monkeypatch.delenv("API_TOKEN", raising=False)
    monkeypatch.setenv("ADMIN_TOKEN", "admin-token")
    client, store, _restart = _client(tmp_path)
    _login(client)

    response = client.put(
        "/api/v1/capabilities",
        json={"enabled": ["diagnostics"]},
    )

    assert response.status_code == 200
    assert response.json() == {
        "available": ["diagnostics"],
        "active": [],
        "desired": ["diagnostics"],
        "pending_restart": True,
        "runtime": {"self_restart": True},
    }
    assert json.loads(store.path.read_text(encoding="utf-8")) == {
        "version": 1,
        "capabilities": {"enabled": ["diagnostics"]},
    }
    assert os.stat(store.path).st_mode & 0o777 == 0o600


def test_mutation_отключает_модуль_только_после_restart(tmp_path, monkeypatch):
    monkeypatch.delenv("API_TOKEN", raising=False)
    monkeypatch.setenv("ADMIN_TOKEN", "admin-token")
    client, store, _restart = _client(tmp_path, active=("diagnostics",))
    _login(client)

    response = client.put("/api/v1/capabilities", json={"enabled": []})

    assert response.status_code == 200
    assert response.json()["active"] == ["diagnostics"]
    assert response.json()["desired"] == []
    assert response.json()["pending_restart"] is True
    assert store.load() == ()


@pytest.mark.parametrize(
    "payload",
    (
        {},
        {"enabled": "diagnostics"},
        {"enabled": ["unknown"]},
        {"enabled": ["diagnostics", "diagnostics"]},
        {"enabled": [], "extra": True},
    ),
)
def test_mutation_отклоняет_неверный_body_без_записи(
    tmp_path,
    monkeypatch,
    payload,
):
    monkeypatch.delenv("API_TOKEN", raising=False)
    monkeypatch.setenv("ADMIN_TOKEN", "admin-token")
    client, store, _restart = _client(tmp_path)
    _login(client)

    response = client.put("/api/v1/capabilities", json=payload)

    assert response.status_code == 422
    assert store.path.exists() is False


def test_mutation_отклоняет_malformed_json_без_записи(tmp_path, monkeypatch):
    monkeypatch.delenv("API_TOKEN", raising=False)
    monkeypatch.setenv("ADMIN_TOKEN", "admin-token")
    client, store, _restart = _client(tmp_path)
    _login(client)

    response = client.put(
        "/api/v1/capabilities",
        content=b"{",
        headers={"content-type": "application/json"},
    )

    assert response.status_code == 422
    assert store.path.exists() is False


def test_mutation_отклоняет_повторный_root_key_без_записи(
    tmp_path,
    monkeypatch,
):
    monkeypatch.delenv("API_TOKEN", raising=False)
    monkeypatch.setenv("ADMIN_TOKEN", "admin-token")
    client, store, _restart = _client(tmp_path)
    _login(client)

    response = client.put(
        "/api/v1/capabilities",
        content=b'{"enabled":[],"enabled":["diagnostics"]}',
        headers={"content-type": "application/json"},
    )

    assert response.status_code == 422
    assert store.path.exists() is False


def test_status_публикует_запрет_self_restart(tmp_path, monkeypatch):
    monkeypatch.delenv("API_TOKEN", raising=False)
    monkeypatch.setenv("ADMIN_TOKEN", "admin-token")
    client, store, _restart = _client(tmp_path, restart_enabled=False)
    _login(client)
    store.save(("diagnostics",))

    response = client.get("/api/v1/capabilities")

    assert response.status_code == 200
    assert response.json()["runtime"] == {"self_restart": False}


def test_mutation_требует_admin_и_same_origin(tmp_path, monkeypatch):
    monkeypatch.setenv("API_TOKEN", "read-token")
    monkeypatch.setenv("ADMIN_TOKEN", "admin-token")
    client, store, _restart = _client(tmp_path)
    _login(client, "read-token")

    read_only = client.put(
        "/api/v1/capabilities",
        json={"enabled": ["diagnostics"]},
    )
    client.cookies.clear()
    _login(client)
    foreign_origin = client.put(
        "/api/v1/capabilities",
        json={"enabled": ["diagnostics"]},
        headers={"origin": "http://sibling.test"},
    )

    assert read_only.status_code == 403
    assert foreign_origin.status_code == 403
    assert store.path.exists() is False


def test_mutation_при_ошибке_записи_сохраняет_прежние_bytes(
    tmp_path,
    monkeypatch,
):
    import mcp1c.capabilities as capability_module

    monkeypatch.delenv("API_TOKEN", raising=False)
    monkeypatch.setenv("ADMIN_TOKEN", "admin-token")
    client, store, _restart = _client(tmp_path)
    _login(client)
    store.save(())
    original = store.path.read_bytes()
    monkeypatch.setattr(
        capability_module.os,
        "replace",
        lambda source, target: (_ for _ in ()).throw(OSError("synthetic")),
    )

    response = client.put(
        "/api/v1/capabilities",
        json={"enabled": ["diagnostics"]},
    )

    assert response.status_code == 409
    assert store.path.read_bytes() == original


def test_mutation_не_перезаписывает_повреждённые_settings(
    tmp_path,
    monkeypatch,
):
    monkeypatch.delenv("API_TOKEN", raising=False)
    monkeypatch.setenv("ADMIN_TOKEN", "admin-token")
    client, store, _restart = _client(tmp_path)
    _login(client)
    store.path.parent.mkdir(parents=True, exist_ok=True)
    store.path.write_bytes(b"{}")

    response = client.put(
        "/api/v1/capabilities",
        json={"enabled": ["diagnostics"]},
    )

    assert response.status_code == 409
    assert store.path.read_bytes() == b"{}"


def test_параллельные_mutation_возвращают_свой_status_и_целый_json(
    tmp_path,
    monkeypatch,
):
    monkeypatch.delenv("API_TOKEN", raising=False)
    monkeypatch.setenv("ADMIN_TOKEN", "admin-token")
    client, store, _restart = _client(tmp_path)
    _login(client)
    original_save = store.save
    barrier = Barrier(2)

    def simultaneous_save(enabled):
        barrier.wait(timeout=2)
        return original_save(enabled)

    monkeypatch.setattr(store, "save", simultaneous_save)

    def update(enabled):
        return client.put("/api/v1/capabilities", json={"enabled": enabled})

    with ThreadPoolExecutor(max_workers=2) as pool:
        enabled = pool.submit(update, ["diagnostics"])
        disabled = pool.submit(update, [])
        responses = (enabled.result(), disabled.result())

    assert [response.status_code for response in responses] == [200, 200]
    assert responses[0].json()["desired"] == ["diagnostics"]
    assert responses[1].json()["desired"] == []
    payload = json.loads(store.path.read_text(encoding="utf-8"))
    assert payload["capabilities"]["enabled"] in (["diagnostics"], [])


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
