"""RED-контракты HTTP API двухфазных операций intake V2."""

from __future__ import annotations

import importlib
import io
import threading
import time
import zipfile
from pathlib import Path

import pytest
from starlette.applications import Starlette

from conftest import живой_клиент
from mcp1c.dashboard_runtime import DASHBOARD_ON, routes
from mcp1c.intake_v2 import DurableCandidateStore
from mcp1c.intake_v2_lifecycle import CandidateCatalog, IntakeLifecycle
from mcp1c.intake_v2_operations import IntakeCoordinator
from mcp1c.intake_v2_transport import BrowserStagingStore
from mcp1c.registry import Registry
from test_intake_v2_collector import _configuration


SUBJECT = "mcp1c.intake_v2_api"


def _symbol(name: str):
    try:
        module = importlib.import_module(SUBJECT)
    except ModuleNotFoundError as error:
        if error.name != SUBJECT:
            raise
        pytest.fail(f"RED: отсутствует модуль {SUBJECT} для контракта {name}")
    if not hasattr(module, name):
        pytest.fail(f"RED: в {SUBJECT} отсутствует контракт {name}")
    return getattr(module, name)


def _archive(name: str = "DemoConfiguration") -> bytes:
    payload = io.BytesIO()
    with zipfile.ZipFile(payload, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("Configuration.xml", _configuration(name))
    return payload.getvalue()


def _write_archive(path: Path, name: str = "DemoConfiguration") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(_archive(name))


def _service(registry: Registry):
    IntakeApiService = _symbol("IntakeApiService")
    root = registry.data_dir / "intake-v2-test"
    browser = BrowserStagingStore(root / "uploads")
    records = DurableCandidateStore(root / "records")
    lifecycle = IntakeLifecycle(
        CandidateCatalog(root / "catalog"),
        browser,
        IntakeCoordinator(root / "operations", records),
        incoming_root=registry.incoming_dir,
        directory_settle_seconds=0,
    )
    return IntakeApiService(registry, lifecycle)


def _client(registry: Registry, service):
    return живой_клиент(
        Starlette(routes=routes(registry, mode=DASHBOARD_ON, intake=service))
    )


def _wait_job(client, job_id: str, timeout: float = 10.0) -> dict:
    limit = time.monotonic() + timeout
    while time.monotonic() < limit:
        response = client.get(
            f"/api/v1/sources/intake/jobs/{job_id}",
            headers={"x-api-token": "admin-token"},
        )
        assert response.status_code == 200
        payload = response.json()["job"]
        if payload["state"] in {"done", "failed"}:
            return payload
        time.sleep(0.02)
    raise AssertionError("intake job не завершилась за отведённое время")


def test_refresh_требует_admin_и_не_раскрывает_серверный_путь(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("API_TOKEN", "read-token")
    monkeypatch.setenv("ADMIN_TOKEN", "admin-token")
    registry = Registry(tmp_path / "data")
    _write_archive(registry.incoming_dir / "candidate.zip")
    service = _service(registry)
    client = _client(registry, service)

    denied = client.get(
        "/api/v1/sources/intake", headers={"x-api-token": "read-token"}
    )
    response = client.get(
        "/api/v1/sources/intake", headers={"x-api-token": "admin-token"}
    )

    assert denied.status_code == 403
    assert response.status_code == 200
    payload = response.json()
    assert payload["api_version"] == "v1"
    assert payload["configuration_names"] == []
    assert payload["issues"] == []
    assert payload["jobs"] == []
    assert payload["groups"] == [
        {
            "source_kind": "configuration",
            "internal_name": "DemoConfiguration",
            "candidate_ids": [payload["candidates"][0]["id"]],
        }
    ]
    candidate = payload["candidates"][0]
    assert candidate["origin_name"] == "candidate.zip"
    assert candidate["actions"] == ["create"]
    assert candidate["requires_parent"] is False
    assert str(tmp_path) not in response.text
    assert registry.snapshot().configuration_names == ()


def test_browser_upload_сохраняет_candidate_но_не_запускает_parse(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("ADMIN_TOKEN", "admin-token")
    registry = Registry(tmp_path / "data")
    service = _service(registry)
    client = _client(registry, service)

    response = client.post(
        "/api/v1/sources/intake/upload",
        headers={"x-api-token": "admin-token"},
        files={"file": ("configuration.zip", _archive())},
    )

    assert response.status_code == 201
    candidate = response.json()["candidate"]
    assert candidate["transport"] == "browser"
    assert candidate["internal_name"] == "DemoConfiguration"
    assert candidate["actions"] == ["create"]
    assert registry.snapshot().configuration_names == ()
    assert service.lifecycle.operations.records.list_jobs() == ()
    assert list(registry.incoming_dir.glob("*")) == []

    restarted = _service(registry)
    snapshot = restarted.snapshot()
    assert [item["id"] for item in snapshot["candidates"]] == [candidate["id"]]

    invalid = client.post(
        "/api/v1/sources/intake/upload",
        headers={"x-api-token": "admin-token"},
        files={"file": ("broken.zip", b"not a zip")},
    )
    assert invalid.status_code == 422
    assert restarted.lifecycle.browser.candidate_ids() == (candidate["id"],)


def test_default_service_читает_только_настроенный_local_source(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("ADMIN_TOKEN", "admin-token")
    local = tmp_path / "private-mount" / "configuration.zip"
    _write_archive(local, "MountedConfiguration")
    monkeypatch.setenv("MCP1C_CONFIG_SOURCE", str(local))
    registry = Registry(tmp_path / "data")
    client = живой_клиент(
        Starlette(routes=routes(registry, mode=DASHBOARD_ON))
    )

    response = client.get(
        "/api/v1/sources/intake", headers={"x-api-token": "admin-token"}
    )

    assert response.status_code == 200
    assert [item["internal_name"] for item in response.json()["candidates"]] == [
        "MountedConfiguration"
    ]
    assert str(tmp_path) not in response.text
    assert local.read_bytes() == _archive("MountedConfiguration")


def test_create_строит_preview_и_публикует_только_после_confirm(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("ADMIN_TOKEN", "admin-token")
    registry = Registry(tmp_path / "data")
    service = _service(registry)
    client = _client(registry, service)
    uploaded = client.post(
        "/api/v1/sources/intake/upload",
        headers={"x-api-token": "admin-token"},
        files={"file": ("configuration.zip", _archive())},
    ).json()["candidate"]

    started = client.post(
        "/api/v1/sources/intake/start",
        headers={"x-api-token": "admin-token"},
        json={"candidate_id": uploaded["id"], "action": "create"},
    )
    assert started.status_code == 202
    job_id = started.json()["job"]["job_id"]
    preview = _wait_job(client, job_id)

    assert preview["state"] == "done"
    assert preview["stage"] == "done"
    assert preview["preview"]["action"] == "create"
    assert preview["preview"]["no_op"] is False
    assert {layer["decision"] for layer in preview["preview"]["layers"]} == {
        "apply"
    }
    assert preview["commit"] is None
    assert registry.snapshot().configuration_names == ()

    confirmed = client.post(
        "/api/v1/sources/intake/confirm",
        headers={"x-api-token": "admin-token"},
        json={"job_id": job_id},
    )
    assert confirmed.status_code == 200
    commit = confirmed.json()["job"]["commit"]
    assert commit["no_op"] is False
    generation_id = commit["generation_id"]
    assert registry.snapshot().configuration_names == ("DemoConfiguration",)

    again = client.post(
        "/api/v1/sources/intake/confirm",
        headers={"x-api-token": "admin-token"},
        json={"job_id": job_id},
    )
    assert again.status_code == 200
    assert again.json()["job"]["commit"]["generation_id"] == generation_id

    no_op_start = client.post(
        "/api/v1/sources/intake/start",
        headers={"x-api-token": "admin-token"},
        json={"candidate_id": uploaded["id"], "action": "update_full"},
    )
    no_op = _wait_job(client, no_op_start.json()["job"]["job_id"])
    assert no_op["preview"]["no_op"] is True
    assert no_op["preview"]["layers"]
    assert {layer["decision"] for layer in no_op["preview"]["layers"]} == {
        "preserve"
    }


def test_progress_и_preview_читаются_после_нового_service(tmp_path, monkeypatch):
    monkeypatch.setenv("ADMIN_TOKEN", "admin-token")
    registry = Registry(tmp_path / "data")
    first_service = _service(registry)
    first_client = _client(registry, first_service)
    candidate = first_client.post(
        "/api/v1/sources/intake/upload",
        headers={"x-api-token": "admin-token"},
        files={"file": ("configuration.zip", _archive())},
    ).json()["candidate"]
    job_id = first_client.post(
        "/api/v1/sources/intake/start",
        headers={"x-api-token": "admin-token"},
        json={"candidate_id": candidate["id"], "action": "create"},
    ).json()["job"]["job_id"]
    before = _wait_job(first_client, job_id)
    assert before["preview"] is not None

    restarted_service = _service(registry)
    restarted_client = _client(registry, restarted_service)
    after = restarted_client.get(
        f"/api/v1/sources/intake/jobs/{job_id}",
        headers={"x-api-token": "admin-token"},
    )

    assert after.status_code == 200
    assert after.json()["job"] == before


def test_готовая_job_с_утраченным_preview_не_выглядит_неизвестной(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("ADMIN_TOKEN", "admin-token")
    registry = Registry(tmp_path / "data")
    service = _service(registry)
    client = _client(registry, service)
    candidate = client.post(
        "/api/v1/sources/intake/upload",
        headers={"x-api-token": "admin-token"},
        files={"file": ("configuration.zip", _archive())},
    ).json()["candidate"]
    job_id = client.post(
        "/api/v1/sources/intake/start",
        headers={"x-api-token": "admin-token"},
        json={"candidate_id": candidate["id"], "action": "create"},
    ).json()["job"]["job_id"]
    _wait_job(client, job_id)
    (service.lifecycle.operations.previews_dir / f"{job_id}.json").unlink()

    status = client.get(
        f"/api/v1/sources/intake/jobs/{job_id}",
        headers={"x-api-token": "admin-token"},
    )
    confirm = client.post(
        "/api/v1/sources/intake/confirm",
        headers={"x-api-token": "admin-token"},
        json={"job_id": job_id},
    )

    assert status.status_code == 409
    assert confirm.status_code == 409
    assert "preview" in status.json()["error"]
    assert "preview" in confirm.json()["error"]


def test_start_не_принимает_путь_и_не_запускает_два_parse(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("ADMIN_TOKEN", "admin-token")
    registry = Registry(tmp_path / "data")
    service = _service(registry)
    client = _client(registry, service)
    candidate = client.post(
        "/api/v1/sources/intake/upload",
        headers={"x-api-token": "admin-token"},
        files={"file": ("configuration.zip", _archive())},
    ).json()["candidate"]

    rejected = client.post(
        "/api/v1/sources/intake/start",
        headers={"x-api-token": "admin-token"},
        json={
            "candidate_id": candidate["id"],
            "action": "create",
            "path": str(tmp_path / "private.zip"),
        },
    )
    assert rejected.status_code == 422
    assert "path" not in rejected.text

    entered = threading.Event()
    release = threading.Event()
    real_prepare = service.prepare

    def blocked_prepare(work):
        entered.set()
        assert release.wait(5)
        return real_prepare(work)

    monkeypatch.setattr(service, "prepare", blocked_prepare)
    first = client.post(
        "/api/v1/sources/intake/start",
        headers={"x-api-token": "admin-token"},
        json={"candidate_id": candidate["id"], "action": "create"},
    )
    assert first.status_code == 202
    assert entered.wait(5)
    second = client.post(
        "/api/v1/sources/intake/start",
        headers={"x-api-token": "admin-token"},
        json={"candidate_id": candidate["id"], "action": "create"},
    )
    assert second.status_code == 409
    assert "одна" in second.json()["error"]
    confirm_while_busy = client.post(
        "/api/v1/sources/intake/confirm",
        headers={"x-api-token": "admin-token"},
        json={"job_id": first.json()["job"]["job_id"]},
    )
    assert confirm_while_busy.status_code == 409
    release.set()
