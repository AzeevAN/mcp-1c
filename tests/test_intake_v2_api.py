"""RED-контракты HTTP API двухфазных операций intake V2."""

from __future__ import annotations

import base64
import importlib
import io
import json
import os
import sys
import random
import threading
import time
import zipfile
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest
from starlette.applications import Starlette

from conftest import build_configuration, write_export, живой_клиент
from mcp1c.dashboard_runtime import DASHBOARD_ON, routes
from mcp1c.intake_v2 import DurableCandidateStore, ExportIdentity, LayerKind
from mcp1c.intake_v2_lifecycle import CandidateCatalog, IntakeLifecycle, LifecycleError
from mcp1c.intake_v2_operations import IntakeCoordinator
from mcp1c.intake_v2_transport import BrowserStagingStore
from mcp1c.registry import Registry
from mcp1c.source_modes import ActivationMode, ActivationStatus
from test_intake_v2_collector import _configuration
from test_intake_v2_extensions import _materialized, _stage_active_base


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


def _archive(name: str = "DemoConfiguration", version: str = "1.0") -> bytes:
    payload = io.BytesIO()
    with zipfile.ZipFile(payload, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("Configuration.xml", _configuration(name, version))
    return payload.getvalue()


def _write_archive(
    path: Path, name: str = "DemoConfiguration", version: str = "1.0"
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(_archive(name, version))


def _write_extension_archive(path: Path, name: str = "DemoExtension") -> None:
    descriptor = (
        '<MetaDataObject xmlns="http://v8.1c.ru/8.3/MDClasses">'
        "<Configuration><Properties>"
        f"<Name>{name}</Name><Version>1.0</Version>"
        "<NamePrefix>Demo_</NamePrefix>"
        "<ObjectBelonging>Adopted</ObjectBelonging>"
        "<ConfigurationExtensionPurpose>AddOn</ConfigurationExtensionPurpose>"
        "<CompatibilityMode></CompatibilityMode>"
        "</Properties><ChildObjects/></Configuration></MetaDataObject>"
    ).encode()
    payload = io.BytesIO()
    with zipfile.ZipFile(payload, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("Configuration.xml", descriptor)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(payload.getvalue())


def test_extension_full_требует_родителя_и_проходит_preview_confirm(tmp_path):
    IntakeApiConflict = _symbol("IntakeApiConflict")
    registry = Registry(tmp_path / "data")
    source = tmp_path / "base"
    source.mkdir()
    registry.add_configuration(
        write_export(
            source,
            build_configuration(name="DemoConfiguration"),
        ),
        keep_source=False,
    )
    _write_extension_archive(registry.incoming_dir / "extension.zip")
    service = _service(registry)
    snapshot = service.snapshot()
    candidate = snapshot["candidates"][0]

    assert candidate["source_kind"] == "extension"
    assert candidate["requires_parent"] is True
    assert candidate["actions"] == ["update_full"]
    with pytest.raises(IntakeApiConflict, match="родител"):
        service.start(candidate["id"], "update_full")

    with pytest.raises(IntakeApiConflict, match="active B_FULL"):
        service.start(
            candidate["id"],
            "update_full",
            parent_configuration="DemoConfiguration",
        )

    _collection_value, base = _materialized(tmp_path, "api-extension-parent")
    registry.publish_generation(_stage_active_base(registry, base))

    work = service.start(
        candidate["id"],
        "update_full",
        job_id="job-extension",
        parent_configuration="DemoConfiguration",
    )
    service.prepare(work)
    preview = service.job_payload(work.job_id)["preview"]
    assert preview["identity"] == {
        "source_kind": "extension",
        "configuration_name": "",
        "extension_name": "DemoExtension",
        "parent_configuration": "DemoConfiguration",
    }
    assert preview["no_op"] is False

    committed = service.confirm(work.job_id)
    assert committed["commit"]["no_op"] is False
    identity = ExportIdentity.extension(
        "DemoExtension",
        parent_configuration="DemoConfiguration",
    )
    assert registry.active_generation_pointer(identity) is not None

    activation = registry.active_activation(identity)
    assert activation is not None
    assert activation.status is ActivationStatus.ACTIVE
    assert activation.mode is ActivationMode.B_FULL


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


def _mark_request_as_old_update(service, job_id: str) -> None:
    """Смоделировать сохранённый прежней версией durable request."""
    path = service.lifecycle.operations.requests_dir / f"{job_id}.json"
    record = json.loads(path.read_text(encoding="utf-8"))
    record["payload"]["action"] = "update"
    path.write_text(json.dumps(record, ensure_ascii=False), encoding="utf-8")


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


def test_legacy_цель_не_предлагает_и_не_запускает_частичное_обновление(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("ADMIN_TOKEN", "admin-token")
    registry = Registry(tmp_path / "data")
    source = tmp_path / "base"
    source.mkdir()
    registry.add_configuration(
        write_export(source, build_configuration(name="DemoConfiguration")),
        keep_source=False,
    )
    _write_archive(registry.incoming_dir / "candidate.zip")
    service = _service(registry)
    client = _client(registry, service)

    candidate = client.get(
        "/api/v1/sources/intake",
        headers={"x-api-token": "admin-token"},
    ).json()["candidates"][0]
    rejected = client.post(
        "/api/v1/sources/intake/start",
        headers={"x-api-token": "admin-token"},
        json={"candidate_id": candidate["id"], "action": "update"},
    )

    assert candidate["actions"] == ["update_full"]
    assert rejected.status_code == 409
    assert "полное обновление" in rejected.json()["error"]
    assert service.lifecycle.operations.records.list_jobs() == ()


def test_native_source_b_не_предлагает_и_не_запускает_новое_частичное_обновление(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("ADMIN_TOKEN", "admin-token")
    registry = Registry(tmp_path / "data")
    _write_archive(registry.incoming_dir / "candidate.zip")
    service = _service(registry)
    initial = service.snapshot()["candidates"][0]
    create = service.start(initial["id"], "create", job_id="job-create-native")
    service.prepare(create)
    service.confirm(create.job_id)

    client = _client(registry, service)
    candidate = client.get(
        "/api/v1/sources/intake",
        headers={"x-api-token": "admin-token"},
    ).json()["candidates"][0]
    rejected = client.post(
        "/api/v1/sources/intake/start",
        headers={"x-api-token": "admin-token"},
        json={"candidate_id": candidate["id"], "action": "update"},
    )

    assert candidate["actions"] == ["update_full"]
    assert rejected.status_code == 409
    assert "полное обновление" in rejected.json()["error"]
    assert {job.job_id for job in service.lifecycle.operations.records.list_jobs()} == {
        "job-create-native"
    }


def test_старый_native_partial_preview_читается_но_не_публикуется(tmp_path):
    IntakeApiConflict = _symbol("IntakeApiConflict")
    registry = Registry(tmp_path / "data")
    _write_archive(registry.incoming_dir / "candidate.zip")
    service = _service(registry)
    candidate = service.snapshot()["candidates"][0]
    create = service.start(candidate["id"], "create", job_id="job-create-native")
    service.prepare(create)
    service.confirm(create.job_id)
    before = registry.active_generation_pointer(
        registry.generation_view("DemoConfiguration").identity
    )

    old_job_id = "job-old-partial-preview"
    old = service.start(candidate["id"], "update_full", job_id=old_job_id)
    service.prepare(old)
    _mark_request_as_old_update(service, old_job_id)

    stale = service.job_payload(old_job_id)
    assert stale["state"] == "failed"
    assert stale["preview"] is None
    with pytest.raises(IntakeApiConflict, match="полное обновление"):
        service.confirm(old_job_id)
    assert registry.active_generation_pointer(
        registry.generation_view("DemoConfiguration").identity
    ) == before


def test_старый_legacy_preview_не_ломает_snapshot_и_возвращает_конфликт(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("ADMIN_TOKEN", "admin-token")
    registry = Registry(tmp_path / "data")
    source = tmp_path / "base"
    source.mkdir()
    registry.add_configuration(
        write_export(source, build_configuration(name="DemoConfiguration")),
        keep_source=False,
    )
    _write_archive(registry.incoming_dir / "candidate.zip")
    service = _service(registry)
    client = _client(registry, service)
    candidate = service.snapshot()["candidates"][0]
    job_id = "job-stale-legacy-preview"
    old = service.start(candidate["id"], "update_full", job_id=job_id)
    service.prepare(old)
    _mark_request_as_old_update(service, job_id)

    snapshot = client.get(
        "/api/v1/sources/intake",
        headers={"x-api-token": "admin-token"},
    )
    status = client.get(
        f"/api/v1/sources/intake/jobs/{job_id}",
        headers={"x-api-token": "admin-token"},
    )
    confirm = client.post(
        "/api/v1/sources/intake/confirm",
        headers={"x-api-token": "admin-token"},
        json={"job_id": job_id},
    )

    assert snapshot.status_code == 200
    stale = next(item for item in snapshot.json()["jobs"] if item["job_id"] == job_id)
    assert stale["state"] == "failed"
    assert stale["stage"] == "failed"
    assert "полное обновление" in stale["error"]
    assert stale["preview"] is None
    assert status.status_code == 200
    assert status.json()["job"] == stale
    assert confirm.status_code == 409
    assert "полное обновление" in confirm.json()["error"]
    assert registry.active_generation_pointer(
        registry.generation_view("DemoConfiguration").identity
    ) is None


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


def test_browser_candidate_удаляется_только_после_завершения_job(tmp_path, monkeypatch):
    monkeypatch.setenv("ADMIN_TOKEN", "admin-token")
    registry = Registry(tmp_path / "data")
    service = _service(registry)
    client = _client(registry, service)
    uploaded = client.post(
        "/api/v1/sources/intake/upload",
        headers={"x-api-token": "admin-token"},
        files={"file": ("configuration.zip", _archive())},
    )
    assert uploaded.status_code == 201
    candidate_id = uploaded.json()["candidate"]["id"]
    endpoint = "/api/v1/sources/intake/candidates/delete"

    assert client.post(endpoint, json={"candidate_id": candidate_id}).status_code in {401, 403}
    service.lifecycle.operations.create_job("job-active", candidate_id)
    active = client.post(
        endpoint,
        headers={"x-api-token": "admin-token"},
        json={"candidate_id": candidate_id},
    )
    assert active.status_code == 409
    assert service.lifecycle.browser.load(candidate_id)

    service.lifecycle.operations.fail("job-active", RuntimeError("синтетическая ошибка"))
    removed = client.post(
        endpoint,
        headers={"x-api-token": "admin-token"},
        json={"candidate_id": candidate_id},
    )
    assert removed.status_code == 200
    assert removed.json() == {"deleted": candidate_id}
    assert service.lifecycle.browser.candidate_ids() == ()
    assert service.lifecycle.operations.records.list_jobs() == ()
    assert service.snapshot()["candidates"] == []

    _write_archive(registry.incoming_dir / "server.zip")
    incoming = next(
        item for item in service.snapshot()["candidates"]
        if item["transport"] == "incoming"
    )
    denied = client.post(
        endpoint,
        headers={"x-api-token": "admin-token"},
        json={"candidate_id": incoming["id"]},
    )
    assert denied.status_code == 409
    assert (registry.incoming_dir / "server.zip").is_file()

    second = client.post(
        "/api/v1/sources/intake/upload",
        headers={"x-api-token": "admin-token"},
        files={"file": ("second.zip", _archive())},
    )
    second_id = second.json()["candidate"]["id"]
    original_remove = service.lifecycle.catalog.remove
    failed_once = False

    def remove_with_one_failure(candidate: str) -> None:
        nonlocal failed_once
        if not failed_once:
            failed_once = True
            raise LifecycleError("синтетическая ошибка удаления каталога")
        original_remove(candidate)

    monkeypatch.setattr(service.lifecycle.catalog, "remove", remove_with_one_failure)
    payload = {"candidate_id": second_id}
    headers = {"x-api-token": "admin-token"}
    assert client.post(endpoint, headers=headers, json=payload).status_code == 422
    assert service.lifecycle.catalog.load(second_id)
    assert client.post(endpoint, headers=headers, json=payload).status_code == 200
    assert service.lifecycle.browser.candidate_ids() == ()


def test_browser_candidate_остаётся_в_списке_при_отказе_удалить_zip(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("ADMIN_TOKEN", "admin-token")
    registry = Registry(tmp_path / "data")
    service = _service(registry)
    client = _client(registry, service)
    headers = {"x-api-token": "admin-token"}
    uploaded = client.post(
        "/api/v1/sources/intake/upload",
        headers=headers,
        files={"file": ("configuration.zip", _archive())},
    )
    assert uploaded.status_code == 201
    candidate_id = uploaded.json()["candidate"]["id"]
    service.lifecycle.operations.create_job("job-failed-delete", candidate_id)
    service.lifecycle.operations.fail(
        "job-failed-delete", RuntimeError("синтетическая ошибка")
    )
    payload_path = service.lifecycle.browser.payloads_dir / f"{candidate_id}.upload"
    original_unlink = Path.unlink
    failed_once = False

    def unlink_with_failure(path, *args, **kwargs):
        nonlocal failed_once
        if path == payload_path and not failed_once:
            failed_once = True
            raise OSError("синтетический отказ удаления ZIP")
        return original_unlink(path, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", unlink_with_failure)
    endpoint = "/api/v1/sources/intake/candidates/delete"
    request = {"candidate_id": candidate_id}
    assert client.post(endpoint, headers=headers, json=request).status_code == 422
    assert candidate_id in {
        item["id"] for item in service.snapshot()["candidates"]
    }
    restarted = _service(registry)
    assert (
        restarted.lifecycle.operations.records.load_job("job-failed-delete").error
        == "синтетическая ошибка"
    )
    restarted_client = _client(registry, restarted)
    assert restarted_client.post(endpoint, headers=headers, json=request).status_code == 200
    assert not payload_path.exists()
    assert restarted.lifecycle.operations.records.list_jobs() == ()


def test_production_lifecycle_читает_browser_xml_как_incoming(tmp_path):
    IntakeApiService = _symbol("IntakeApiService")
    registry = Registry(tmp_path / "data")
    service = IntakeApiService.for_registry(
        registry, config_sources_root=tmp_path / "sources"
    )
    # Как у реального шаблона этикетки: около 230:1, ниже предела 512:1.
    xml = (
        b"<Template><Data>"
        + b"A" * (2 * 1024 * 1024)
        + base64.b64encode(random.Random(1).randbytes(6000))
        + b"</Data></Template>"
    )
    path = "Catalogs/Demo/Templates/Label/Ext/Template.xml"
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("Configuration.xml", _configuration("DemoConfiguration"))
        archive.writestr(path, xml)
    raw = buffer.getvalue()
    candidate = service.accept_upload("demo.zip", io.BytesIO(raw), expected_size=len(raw))
    discovered = service.lifecycle.catalog.load(candidate["id"])
    with service.lifecycle._open(discovered.locator) as tree:
        with tree.open(path) as stream:
            assert stream.read() == xml


def test_default_service_игнорирует_удалённый_singleton(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("ADMIN_TOKEN", "admin-token")
    local = tmp_path / "private-mount" / "configuration.zip"
    _write_archive(local, "MountedConfiguration")
    исходный_архив = local.read_bytes()
    monkeypatch.setenv("MCP1C_CONFIG_SOURCE", str(local))
    registry = Registry(tmp_path / "data")
    client = живой_клиент(
        Starlette(routes=routes(registry, mode=DASHBOARD_ON))
    )

    response = client.get(
        "/api/v1/sources/intake", headers={"x-api-token": "admin-token"}
    )

    assert response.status_code == 200
    assert response.json()["candidates"] == []
    assert str(tmp_path) not in response.text
    assert local.read_bytes() == исходный_архив


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
    assert preview["preview"]["extension_impacts"] == {
        "total": 0,
        "items": [],
        "truncated": False,
    }
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
    assert service.snapshot()["candidates"] == []

    again = client.post(
        "/api/v1/sources/intake/confirm",
        headers={"x-api-token": "admin-token"},
        json={"job_id": job_id},
    )
    assert again.status_code == 200
    assert again.json()["job"]["commit"]["generation_id"] == generation_id

    uploaded_again = client.post(
        "/api/v1/sources/intake/upload",
        headers={"x-api-token": "admin-token"},
        files={"file": ("configuration.zip", _archive())},
    ).json()["candidate"]
    no_op_start = client.post(
        "/api/v1/sources/intake/start",
        headers={"x-api-token": "admin-token"},
        json={"candidate_id": uploaded_again["id"], "action": "update_full"},
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


def test_confirm_удаляет_тяжелый_work_но_сохраняет_idempotent_result(
    tmp_path,
):
    registry = Registry(tmp_path / "data")
    service = _service(registry)
    raw = _archive()
    candidate = service.accept_upload(
        "configuration.zip",
        io.BytesIO(raw),
        expected_size=len(raw),
    )
    work = service.start(
        candidate["id"],
        "create",
        job_id="job-confirm-cleanup",
    )
    service.prepare(work)
    operations = service.lifecycle.operations
    work_root = operations.work_dir / work.job_id
    request = operations.requests_dir / f"{work.job_id}.json"
    preview = operations.previews_dir / f"{work.job_id}.json"

    assert work_root.is_dir()
    assert request.is_file()
    assert preview.is_file()

    first = service.confirm(work.job_id)

    assert first["preview"] is None
    assert first["commit"]["generation_id"] == work.generation_id
    assert not work_root.exists()
    assert not request.exists()
    assert not preview.exists()
    assert (
        operations.commits_dir / f"{work.job_id}.json"
    ).is_file()
    assert service.lifecycle.browser.candidate_ids() == ()
    assert service.confirm(work.job_id) == first
    assert _service(registry).job_payload(work.job_id) == first
    IntakeApiConflict = _symbol("IntakeApiConflict")
    with pytest.raises(IntakeApiConflict, match="Опубликованную job"):
        service.discard(work.job_id)


def test_discard_удаляет_неопубликованный_preview_и_оставляет_candidate(
    tmp_path,
    monkeypatch,
):
    monkeypatch.setenv("ADMIN_TOKEN", "admin-token")
    registry = Registry(tmp_path / "data")
    service = _service(registry)
    raw = _archive()
    candidate = service.accept_upload(
        "configuration.zip",
        io.BytesIO(raw),
        expected_size=len(raw),
    )
    work = service.start(
        candidate["id"],
        "create",
        job_id="job-discard-preview",
    )
    service.prepare(work)
    operations = service.lifecycle.operations

    response = _client(registry, service).post(
        "/api/v1/sources/intake/discard",
        headers={"x-api-token": "admin-token"},
        json={"job_id": work.job_id},
    )

    assert response.status_code == 200
    assert response.json() == {"discarded": work.job_id}
    assert not (operations.work_dir / work.job_id).exists()
    for directory in (
        operations.requests_dir,
        operations.previews_dir,
        operations.commits_dir,
        operations.records.jobs_dir,
    ):
        assert not (directory / f"{work.job_id}.json").exists()
    snapshot = service.snapshot()
    assert [item["id"] for item in snapshot["candidates"]] == [candidate["id"]]
    assert snapshot["jobs"] == []


def test_snapshot_не_падает_если_discard_удалил_перечисленную_job(
    tmp_path,
    monkeypatch,
):
    registry = Registry(tmp_path / "data")
    service = _service(registry)
    raw = _archive()
    candidate = service.accept_upload(
        "configuration.zip",
        io.BytesIO(raw),
        expected_size=len(raw),
    )
    work = service.start(
        candidate["id"],
        "create",
        job_id="job-concurrent-discard",
    )
    service.prepare(work)

    records = service.lifecycle.operations.records
    # Список строится по scan_jobs (нечитаемые записи не роняют его); строгий
    # list_jobs внутри discard тоже идёт через scan_jobs, поэтому блокируется
    # только первый вызов — тот, что делает snapshot.
    real_scan_jobs = records.scan_jobs
    jobs_listed = threading.Event()
    continue_snapshot = threading.Event()

    def blocked_scan_jobs():
        jobs = real_scan_jobs()
        if not jobs_listed.is_set():
            jobs_listed.set()
            assert continue_snapshot.wait(5)
        return jobs

    monkeypatch.setattr(records, "scan_jobs", blocked_scan_jobs)
    snapshot_result: list[dict[str, object]] = []
    snapshot_errors: list[Exception] = []

    def read_snapshot():
        try:
            snapshot_result.append(service.snapshot())
        except Exception as error:  # pragma: no cover - проверяется ниже
            snapshot_errors.append(error)

    snapshot_thread = threading.Thread(target=read_snapshot)
    snapshot_thread.start()
    assert jobs_listed.wait(5)

    # Если lifecycle-lock свободен, принудительно завершаем discard до чтения
    # payload. При атомарном снимке discard дождётся его окончания, и оба
    # допустимых порядка остаются детерминированными.
    lock_was_free = service.lifecycle._lock.acquire(blocking=False)
    if lock_was_free:
        service.lifecycle._lock.release()

    discard_errors: list[Exception] = []
    discard_done = threading.Event()

    def discard_preview():
        try:
            service.discard(work.job_id)
        except Exception as error:  # pragma: no cover - проверяется ниже
            discard_errors.append(error)
        finally:
            discard_done.set()

    discard_thread = threading.Thread(target=discard_preview)
    discard_thread.start()
    if lock_was_free:
        assert discard_done.wait(5)
    continue_snapshot.set()

    snapshot_thread.join(5)
    discard_thread.join(5)
    assert not snapshot_thread.is_alive()
    assert not discard_thread.is_alive()
    assert discard_errors == []
    assert snapshot_errors == []
    assert len(snapshot_result) == 1
    snapshot_jobs = snapshot_result[0]["jobs"]
    assert snapshot_jobs == [] or [item["job_id"] for item in snapshot_jobs] == [
        work.job_id
    ]
    assert service.snapshot()["jobs"] == []


def test_remove_конфигурации_чистит_все_derived_job_но_оставляет_incoming(
    tmp_path,
    monkeypatch,
):
    monkeypatch.setenv("ADMIN_TOKEN", "admin-token")
    registry = Registry(tmp_path / "data")
    incoming = registry.incoming_dir / "configuration.zip"
    _write_archive(incoming)
    incoming_extension = registry.incoming_dir / "extension.zip"
    _write_extension_archive(incoming_extension)
    service = _service(registry)
    candidate = next(
        item
        for item in service.snapshot()["candidates"]
        if item["source_kind"] == "configuration"
    )

    create = service.start(
        candidate["id"],
        "create",
        job_id="job-create",
    )
    service.prepare(create)
    committed = service.confirm(create.job_id)
    pointer = registry.active_generation_pointer(
        registry.generation_view("DemoConfiguration").identity
    )
    assert pointer is not None

    update = service.start(
        candidate["id"],
        "update_full",
        job_id="job-unpublished-update",
    )
    service.prepare(update)
    assert (service.lifecycle.operations.work_dir / update.job_id).is_dir()

    extension_candidate = next(
        item
        for item in service.snapshot()["candidates"]
        if item["source_kind"] == "extension"
    )
    extension = service.start(
        extension_candidate["id"],
        "update_full",
        job_id="job-unpublished-extension",
        parent_configuration="DemoConfiguration",
    )
    service.prepare(extension)

    client = _client(registry, service)
    removed = client.post(
        "/api/v1/sources/remove",
        headers={"x-api-token": "admin-token"},
        json={
            "id": "DemoConfiguration",
            "confirmation": "DemoConfiguration",
        },
    )

    assert removed.status_code == 200
    assert incoming.is_file()
    assert incoming_extension.is_file()
    assert registry.snapshot().configuration_names == ()
    assert not (registry.data_dir / pointer.root_path).exists()
    assert not (registry.data_dir / pointer.root_path).parent.exists()
    operations = service.lifecycle.operations
    for job_id in (create.job_id, update.job_id, extension.job_id):
        assert not (operations.work_dir / job_id).exists()
        for directory in (
            operations.requests_dir,
            operations.previews_dir,
            operations.commits_dir,
            operations.records.jobs_dir,
        ):
            assert not (directory / f"{job_id}.json").exists()
    refreshed = service.snapshot()
    assert refreshed["jobs"] == []
    configuration = next(
        item
        for item in refreshed["candidates"]
        if item["source_kind"] == "configuration"
    )
    assert configuration["actions"] == ["create"]


def test_remove_дочищает_job_если_registry_уже_снят_старой_версией(
    tmp_path,
    monkeypatch,
):
    monkeypatch.setenv("ADMIN_TOKEN", "admin-token")
    registry = Registry(tmp_path / "data")
    incoming = registry.incoming_dir / "configuration.zip"
    _write_archive(incoming)
    service = _service(registry)
    candidate = service.snapshot()["candidates"][0]
    create = service.start(
        candidate["id"],
        "create",
        job_id="job-old-server",
    )
    service.prepare(create)
    service.confirm(create.job_id)
    update = service.start(
        candidate["id"],
        "update_full",
        job_id="job-old-server-preview",
    )
    service.prepare(update)
    operations = service.lifecycle.operations

    # Воспроизводим прежнюю версию: Registry уже снят, intake не очищен.
    registry.remove("DemoConfiguration")
    registry.save()
    assert (operations.work_dir / update.job_id).is_dir()

    response = _client(registry, service).post(
        "/api/v1/sources/remove",
        headers={"x-api-token": "admin-token"},
        json={
            "id": "DemoConfiguration",
            "confirmation": "DemoConfiguration",
        },
    )

    assert response.status_code == 200
    assert response.json() == {"removed": "DemoConfiguration"}
    assert incoming.is_file()
    assert service.snapshot()["jobs"] == []
    assert not (operations.work_dir / update.job_id).exists()


def _загруженная(tmp_path, version: str, name: str = "DemoConfiguration") -> Registry:
    registry = Registry(tmp_path / "data")
    source = tmp_path / "base"
    source.mkdir()
    registry.add_configuration(
        write_export(source, build_configuration(name=name, version=version)),
        keep_source=False,
    )
    return registry


@pytest.mark.parametrize(
    ("загружена", "кандидат", "действия", "заблокировано"),
    [
        ("2.3.26.24", "3.0.14.178", [], True),
        ("3.0.14.178", "2.3.26.24", [], True),
        ("2.3.26.24", "2.3.27.1", ["update_full"], False),
        ("2.3.26.24", "", [], True),
        ("", "2.3.26.24", ["update_full"], False),
        ("", "", ["update_full"], False),
        ("20080620(0935)", "20090101(1000)", ["update_full"], False),
    ],
    ids=[
        "повышение", "понижение", "та_же_редакция", "номерная_в_пустую",
        "пустая_в_номерную", "обе_пустые", "обе_не_номерные",
    ],
)
def test_кандидат_несёт_действия_и_причину_запрета_по_редакции(
    tmp_path, загружена, кандидат, действия, заблокировано
):
    registry = _загруженная(tmp_path, загружена)
    _write_archive(registry.incoming_dir / "candidate.zip", version=кандидат)
    service = _service(registry)

    payload = service.snapshot()["candidates"][0]

    assert payload["actions"] == действия
    assert bool(payload["blocked_reason"]) is заблокировано
    if заблокировано:
        assert "DemoConfiguration" in payload["blocked_reason"]


def test_новая_конфигурация_получает_create_без_причины(tmp_path):
    registry = Registry(tmp_path / "data")
    _write_archive(registry.incoming_dir / "candidate.zip", version="3.0.1.1")

    payload = _service(registry).snapshot()["candidates"][0]

    assert payload["actions"] == ["create"]
    assert payload["blocked_reason"] == ""


def test_расширение_не_проверяется_на_редакцию(tmp_path):
    registry = _загруженная(tmp_path, "2.3.26.24")
    _write_extension_archive(registry.incoming_dir / "extension.zip")
    service = _service(registry)

    payload = service.snapshot()["candidates"][0]

    assert payload["source_kind"] == "extension"
    assert payload["actions"] == ["update_full"]
    assert payload["blocked_reason"] == ""


def test_start_update_full_при_смене_редакции_отклоняется_без_job(tmp_path):
    IntakeApiConflict = _symbol("IntakeApiConflict")
    registry = _загруженная(tmp_path, "2.3.26.24")
    _write_archive(registry.incoming_dir / "candidate.zip", version="3.0.14.178")
    service = _service(registry)
    candidate = service.snapshot()["candidates"][0]

    with pytest.raises(IntakeApiConflict, match="редакции 2.3.*редакции 3.0"):
        service.start(candidate["id"], "update_full")

    assert service.lifecycle.operations.records.list_jobs() == ()
    loaded = registry.snapshot().configurations["DemoConfiguration"]
    assert loaded.config.version == "2.3.26.24"


def test_http_start_при_смене_редакции_отдаёт_409_с_причиной(tmp_path, monkeypatch):
    monkeypatch.setenv("ADMIN_TOKEN", "admin-token")
    registry = _загруженная(tmp_path, "2.3.26.24")
    _write_archive(registry.incoming_dir / "candidate.zip", version="3.0.14.178")
    service = _service(registry)
    client = _client(registry, service)
    headers = {"x-api-token": "admin-token"}

    candidate = client.get("/api/v1/sources/intake", headers=headers).json()[
        "candidates"
    ][0]
    rejected = client.post(
        "/api/v1/sources/intake/start",
        headers=headers,
        json={"candidate_id": candidate["id"], "action": "update_full"},
    )

    assert candidate["actions"] == []
    assert candidate["blocked_reason"]
    assert rejected.status_code == 409
    assert rejected.json()["error"] == candidate["blocked_reason"]
    assert service.lifecycle.operations.records.list_jobs() == ()


def test_start_update_full_той_же_редакции_создаёт_job(tmp_path):
    registry = _загруженная(tmp_path, "2.3.26.24")
    _write_archive(registry.incoming_dir / "candidate.zip", version="2.3.27.1")
    service = _service(registry)
    candidate = service.snapshot()["candidates"][0]

    work = service.start(candidate["id"], "update_full", job_id="job-same-edition")

    assert work.job_id == "job-same-edition"
    assert {
        job.job_id for job in service.lifecycle.operations.records.list_jobs()
    } == {"job-same-edition"}


def _сервис_с_каталогом(tmp_path, registry: Registry, version: str):
    roots = tmp_path / "mounts"
    folder = roots / "config-0"
    folder.mkdir(parents=True)
    (folder / "Configuration.xml").write_bytes(
        _configuration("DemoConfiguration", version)
    )
    service = _symbol("IntakeApiService").for_registry(
        registry, config_sources_root=roots, directory_settle_seconds=0
    )
    service.bind_directory("DemoConfiguration", "config-0")
    return service


def test_каталог_при_смене_редакции_отдаёт_blocked_reason_и_start_отклоняется(
    tmp_path,
):
    IntakeApiConflict = _symbol("IntakeApiConflict")
    registry = _загруженная(tmp_path, "2.3.26.24")
    service = _сервис_с_каталогом(tmp_path, registry, "3.0.14.178")

    payload = service.refresh_directory("DemoConfiguration")

    assert payload["actions"] == []
    assert "редакции 3.0" in payload["blocked_reason"]
    with pytest.raises(IntakeApiConflict, match="редакции 2.3"):
        service.start(payload["id"], "update_full")
    assert service.lifecycle.operations.records.list_jobs() == ()


def test_каталог_той_же_редакции_предлагает_update_full(tmp_path):
    registry = _загруженная(tmp_path, "2.3.26.24")
    service = _сервис_с_каталогом(tmp_path, registry, "2.3.30.5")

    payload = service.refresh_directory("DemoConfiguration")

    assert payload["actions"] == ["update_full"]
    assert payload["blocked_reason"] == ""


M8_ADMIN = {"x-api-token": "admin-token"}
M8_INTAKE = "/api/v1/sources/intake"


def _m8_prepared(service, *, name="DemoConfiguration", version="1.0", action="create", job_id="m8-job", raw=None):
    candidate = service.accept_upload(
        "configuration.zip", io.BytesIO(_archive(name, version) if raw is None else raw), expected_size=None
    )
    work = service.start(candidate["id"], action, job_id=job_id)
    service.prepare(work)
    return candidate["id"], work.job_id


def _m8_count_payload_restore(monkeypatch):
    """Перехватить уже импортированные ссылки на две дорогие операции.

    Это бюджет чтения, прямо заданный M8, а не требование к новому API.
    Перехватываются также aliases: перенос импорта не обходит проверку.
    """
    watched = {"load_layer_payload", "configuration_from_base_layer"}
    calls = dict.fromkeys(watched, 0)

    originals = {
        getattr(module, name): name
        for module_name, module in tuple(sys.modules.items())
        if module_name.startswith("mcp1c.") and module is not None
        for name in watched
        if hasattr(module, name)
    }
    assert len(originals) == 2
    wrappers = {}
    for original, operation in originals.items():
        def counted(*args, _original=original, _operation=operation, **kwargs):
            calls[_operation] += 1
            return _original(*args, **kwargs)

        wrappers[original] = counted
    for module_name, module in tuple(sys.modules.items()):
        if module_name.startswith("mcp1c.") and module is not None:
            for name, value in tuple(vars(module).items()):
                for original, wrapper in wrappers.items():
                    if value is original:
                        monkeypatch.setattr(module, name, wrapper)
    return calls


@pytest.mark.parametrize("restart", [False, True])
@pytest.mark.parametrize("with_impacts", [False, True])
def test_m8_GET_preview_и_jobs_не_восстанавливают_payload(
    tmp_path, monkeypatch, restart, with_impacts
):
    monkeypatch.setenv("ADMIN_TOKEN", "admin-token")
    registry = Registry(tmp_path / "data")
    service = _service(registry)
    # Синтетический результат публичного расчёта relations позволяет отличить
    # сохранённый при prepare снимок от повторного расчёта при чтении.
    relations = (
        SimpleNamespace(
            extension="SyntheticExtension",
            target="Catalog.SyntheticTarget",
            state=SimpleNamespace(value="target_missing"),
        ),
    ) if with_impacts else ()
    monkeypatch.setattr(registry, "preview_extension_relations", lambda *_a, **_kw: relations)
    _candidate, job_id = _m8_prepared(service)
    expected_impacts = {
        "total": int(with_impacts),
        "items": [{
            "extension": "SyntheticExtension",
            "target": "Catalog.SyntheticTarget",
            "state": "target_missing",
        }] if with_impacts else [],
        "truncated": False,
    }
    if restart:
        service = _service(registry)

    relation_calls = []

    def counted_relations(*_args, **_kwargs):
        relation_calls.append(True)
        return relations

    monkeypatch.setattr(registry, "preview_extension_relations", counted_relations)
    restore_calls = _m8_count_payload_restore(monkeypatch)
    client = _client(registry, service)
    detail = client.get(f"{M8_INTAKE}/jobs/{job_id}", headers=M8_ADMIN)
    assert detail.status_code == 200
    assert restore_calls == {"load_layer_payload": 0, "configuration_from_base_layer": 0}
    assert relation_calls == []
    expected = detail.json()["job"]
    assert expected["state"] == "done"
    assert expected["commit"] is None
    assert expected["preview"]["action"] == "create"
    assert expected["preview"]["no_op"] is False
    assert expected["preview"]["extension_impacts"] == expected_impacts
    for endpoint in (f"{M8_INTAKE}/jobs", M8_INTAKE):
        response = client.get(endpoint, headers=M8_ADMIN)
        assert response.status_code == 200
        assert restore_calls == {"load_layer_payload": 0, "configuration_from_base_layer": 0}
        assert relation_calls == []
        assert response.json()["jobs"] == [expected]


@pytest.mark.parametrize("restart", [False, True])
def test_m8_confirm_проверяет_payload_bytes_и_сохраняет_active(tmp_path, monkeypatch, restart):
    monkeypatch.setenv("ADMIN_TOKEN", "admin-token")
    registry = Registry(tmp_path / "data")
    service = _service(registry)
    _m8_prepared(service, job_id="m8-create")
    service.confirm("m8-create")
    identity = ExportIdentity.configuration("DemoConfiguration")
    before = registry.active_generation_pointer(identity)
    _candidate, job_id = _m8_prepared(
        service, version="1.0.1", action="update_full", job_id="m8-tampered"
    )
    preview = service.lifecycle.operations.load_preview(job_id)
    payload = preview.materialized.payloads[LayerKind.BASE_STRUCTURE].manifest_path
    # JSON остаётся валидным: изменены физические bytes после prepare.
    payload.write_bytes(payload.read_bytes() + b" \n")
    if restart:
        service = _service(registry)
    client = _client(registry, service)
    response = client.post(
        f"{M8_INTAKE}/confirm", headers=M8_ADMIN, json={"job_id": job_id}
    )
    assert response.status_code == 422
    assert "preview" in response.json()["error"]
    assert registry.active_generation_pointer(identity) == before
    assert not (service.lifecycle.operations.commits_dir / f"{job_id}.json").exists()


def test_m8_compact_no_op_отклоняет_сменившийся_active_после_restart(tmp_path, monkeypatch):
    monkeypatch.setenv("ADMIN_TOKEN", "admin-token")
    registry = Registry(tmp_path / "data")
    service = _service(registry)
    raw = _archive()
    _m8_prepared(service, job_id="m8-initial", raw=raw)
    service.confirm("m8-initial")
    identity = ExportIdentity.configuration("DemoConfiguration")
    previous = registry.active_generation_pointer(identity)
    _candidate, job_id = _m8_prepared(
        service, action="update_full", job_id="m8-compact", raw=raw
    )
    preview = service.lifecycle.operations.load_preview(job_id)
    assert preview.plan.no_op is True
    assert not preview.materialized.payloads
    competitor = replace(registry.active_generation(identity), generation_id="m8-competitor")
    registry.publish_generation(
        registry.stage_generation(competitor, registry.generation_payload_sources(previous)),
        expected_previous=previous,
    )
    before = registry.active_generation_pointer(identity)
    restarted = _service(registry)
    client = _client(registry, restarted)
    detail = client.get(f"{M8_INTAKE}/jobs/{job_id}", headers=M8_ADMIN)
    assert detail.status_code == 200
    assert detail.json()["job"]["state"] == "failed"
    assert detail.json()["job"]["preview"] is None
    listed = client.get(f"{M8_INTAKE}/jobs", headers=M8_ADMIN)
    assert listed.status_code == 200
    assert next(row for row in listed.json()["jobs"] if row["job_id"] == job_id) == detail.json()["job"]
    confirmed = client.post(f"{M8_INTAKE}/confirm", headers=M8_ADMIN, json={"job_id": job_id})
    assert confirmed.status_code == 409
    assert registry.active_generation_pointer(identity) == before


@pytest.mark.parametrize("endpoint", [M8_INTAKE, f"{M8_INTAKE}/jobs"])
def test_m8_список_сериализует_последние_50_и_все_unreadable(tmp_path, monkeypatch, endpoint):
    monkeypatch.setenv("ADMIN_TOKEN", "admin-token")
    registry = Registry(tmp_path / "data")
    service = _service(registry)
    records = service.lifecycle.operations.records
    stamps = {}
    # FAILED с отсутствующим candidate не старше retention, без дорогих parse.
    now = time.time()
    for index in reversed(range(55)):
        job_id = f"m8-history-{index:02d}"
        service.lifecycle.operations.create_job(job_id, f"m8-candidate-{index:02d}")
        service.lifecycle.operations.fail(job_id, RuntimeError("синтетическая ошибка"))
        stamp = now - index // 2
        stamps[job_id] = stamp
        os.utime(records.jobs_dir / f"{job_id}.json", (stamp, stamp))
    broken = {f"m8-unreadable-{index}" for index in range(3)}
    for job_id in broken:
        path = records.jobs_dir / f"{job_id}.json"
        path.write_text("{не JSON", encoding="utf-8")
        os.utime(path, (now - 1000, now - 1000))
    expected = sorted(stamps, key=lambda job_id: (-stamps[job_id], job_id))[:50]
    calls = []
    original = service.job_payload

    def counted(job_id):
        calls.append(job_id)
        return original(job_id)

    monkeypatch.setattr(service, "job_payload", counted)
    response = _client(registry, service).get(endpoint, headers=M8_ADMIN)
    assert response.status_code == 200
    rows = response.json()["jobs"]
    assert [row["job_id"] for row in rows if row["job_id"] not in broken] == expected
    assert {row["job_id"] for row in rows if row["broken"]} == broken
    assert all(not row["discardable"] and row["error"] for row in rows if row["job_id"] in broken)
    assert calls == expected
    assert len(rows) == 53
    readable, unreadable = records.scan_jobs()
    assert len(readable) == 55
    assert len(unreadable) == 3


@pytest.mark.parametrize("case,remove", [
    ("old_commit_fresh_job", True),
    ("recent_commit_old_job", False),
    ("boundary_commit_old_job", False),
    ("invalid_commit_old_job", False),
    ("preview_without_commit", False),
    ("failed_without_commit", False),
    ("ready_without_commit", False),
])
def test_m8_refresh_чистит_только_DONE_с_valid_commit_старше_30_дней(
    tmp_path, monkeypatch, case, remove
):
    monkeypatch.setenv("ADMIN_TOKEN", "admin-token")
    # Точная граница должна быть воспроизводимой, без sleep и гонки часов.
    now = 2_000_000_000.0
    monkeypatch.setattr(time, "time", lambda: now)
    cutoff = now - 30 * 24 * 3600
    registry = Registry(tmp_path / "data")
    incoming = registry.incoming_dir / "retention.zip"
    _write_archive(incoming)
    service = _service(registry)
    candidate = service.snapshot()["candidates"][0]
    work = service.start(candidate["id"], "create", job_id="m8-retention")
    operations = service.lifecycle.operations
    if case == "failed_without_commit":
        operations.fail(work.job_id, RuntimeError("синтетическая ошибка"))
    elif case != "ready_without_commit":
        service.prepare(work)
    commit_path = operations.commits_dir / f"{work.job_id}.json"
    if case in {
        "old_commit_fresh_job", "recent_commit_old_job",
        "boundary_commit_old_job", "invalid_commit_old_job",
    }:
        service.confirm(work.job_id)
        if case == "invalid_commit_old_job":
            commit_path.write_text("{не JSON", encoding="utf-8")
        commit_stamp = now if case == "recent_commit_old_job" else cutoff
        if case in {"old_commit_fresh_job", "invalid_commit_old_job"}:
            commit_stamp -= 1
        os.utime(commit_path, (commit_stamp, commit_stamp))
    job_path = operations.records.jobs_dir / f"{work.job_id}.json"
    job_stamp = now if case == "old_commit_fresh_job" else cutoff - 1
    os.utime(job_path, (job_stamp, job_stamp))
    candidate_path = operations.records.candidates_dir / f"{candidate['id']}.json"
    candidate_before = candidate_path.read_bytes()
    zip_before = incoming.read_bytes()
    job_before = job_path.read_bytes()
    identity = ExportIdentity.configuration("DemoConfiguration")
    active_before = registry.active_generation_pointer(identity)

    response = _client(registry, service).get(M8_INTAKE, headers=M8_ADMIN)
    assert response.status_code == 200
    listed = {row["job_id"] for row in response.json()["jobs"]}
    assert (work.job_id not in listed) is remove
    assert job_path.exists() is (not remove)
    if remove:
        # Допускается удаление производных данных job, исходник и candidate живы.
        assert not commit_path.exists()
    else:
        assert job_path.read_bytes() == job_before
    assert candidate_path.read_bytes() == candidate_before
    assert incoming.read_bytes() == zip_before
    assert registry.active_generation_pointer(identity) == active_before


def test_m8_ошибка_relations_при_prepare_очищает_job_и_сохраняет_candidate(
    tmp_path, monkeypatch
):
    from mcp1c.intake_v2_operations import OperationError
    from mcp1c.registry import RegistryError

    registry = Registry(tmp_path / "data")
    service = _service(registry)
    candidate = service.accept_upload(
        "configuration.zip", io.BytesIO(_archive()), expected_size=None
    )
    work = service.start(candidate["id"], "create", job_id="m8-relations-failure")
    operations = service.lifecycle.operations
    candidate_path = operations.records.candidates_dir / f"{candidate['id']}.json"
    zip_path = service.lifecycle.browser.payloads_dir / f"{candidate['id']}.upload"
    candidate_before = candidate_path.read_bytes()
    zip_before = zip_path.read_bytes()
    identity = ExportIdentity.configuration("DemoConfiguration")
    active_before = registry.active_generation_pointer(identity)
    assert active_before is None

    def failed_relations(*_args, **_kwargs):
        raise RegistryError("синтетический отказ расчёта")

    monkeypatch.setattr(registry, "preview_extension_relations", failed_relations)
    # Ошибка должна произойти внутри prepare, до первой попытки читать job.
    with pytest.raises(OperationError, match="синтетический отказ расчёта"):
        service.prepare(work)

    job = operations.records.load_job(work.job_id)
    assert job.state.value == "failed"
    assert job.stage.value == "failed"
    assert "синтетический отказ расчёта" in job.error
    assert not (operations.previews_dir / f"{work.job_id}.json").exists()
    assert not (operations.commits_dir / f"{work.job_id}.json").exists()
    assert not (operations.work_dir / work.job_id).exists()
    assert candidate_path.read_bytes() == candidate_before
    assert zip_path.read_bytes() == zip_before
    assert registry.active_generation_pointer(identity) == active_before


def test_m8_старый_preview_без_impacts_читается_как_stale_но_confirm_сохраняется(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("ADMIN_TOKEN", "admin-token")
    registry = Registry(tmp_path / "data")
    service = _service(registry)
    candidate = service.accept_upload(
        "configuration.zip", io.BytesIO(_archive()), expected_size=None
    )
    work = service.start(candidate["id"], "create", job_id="m8-old-preview")
    # Прежняя lifecycle.prepare не получала callback расчёта impacts.
    service.lifecycle.prepare(
        work.job_id,
        action=work.action,
        active=work.active,
        generation_id=work.generation_id,
    )
    operations = service.lifecycle.operations
    job_path = operations.records.jobs_dir / f"{work.job_id}.json"
    preview_path = operations.previews_dir / f"{work.job_id}.json"
    assert "extension_impacts" not in json.loads(
        preview_path.read_text(encoding="utf-8")
    )["payload"]
    assert operations.records.load_job(work.job_id).state.value == "done"
    durable_paths = [
        job_path,
        preview_path,
        operations.requests_dir / f"{work.job_id}.json",
        operations.records.candidates_dir / f"{candidate['id']}.json",
        service.lifecycle.browser.payloads_dir / f"{candidate['id']}.upload",
        *(
            path for path in (operations.work_dir / work.job_id).rglob("*")
            if path.is_file()
        ),
    ]
    before = {
        path: (path.read_bytes(), path.stat().st_mtime_ns)
        for path in durable_paths
    }
    client = _client(registry, service)

    detail = client.get(f"{M8_INTAKE}/jobs/{work.job_id}", headers=M8_ADMIN)
    assert detail.status_code == 200
    stale = detail.json()["job"]
    assert stale["state"] == "failed"
    assert stale["stage"] == "failed"
    assert stale["preview"] is None
    assert "заново" in stale["error"]
    assert stale["discardable"] is True
    for endpoint in (f"{M8_INTAKE}/jobs", M8_INTAKE):
        listed = client.get(endpoint, headers=M8_ADMIN)
        assert listed.status_code == 200
        assert listed.json()["jobs"] == [stale]
    assert {
        path: (path.read_bytes(), path.stat().st_mtime_ns)
        for path in durable_paths
    } == before
    assert operations.records.load_job(work.job_id).state.value == "done"
    assert not (operations.commits_dir / f"{work.job_id}.json").exists()
    identity = ExportIdentity.configuration("DemoConfiguration")
    assert registry.active_generation_pointer(identity) is None

    # Старый status не запрещает штатный confirm с полной проверкой payload.
    confirmed = service.confirm(work.job_id)
    assert confirmed["commit"]["no_op"] is False
    assert confirmed["commit"]["generation_id"] == work.generation_id
    assert registry.active_generation_pointer(identity).generation_id == work.generation_id
    assert registry.snapshot().configuration_names == ("DemoConfiguration",)
