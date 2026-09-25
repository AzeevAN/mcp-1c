"""RED-контракты HTTP API двухфазных операций intake V2."""

from __future__ import annotations

import importlib
import io
import json
import threading
import time
import zipfile
from pathlib import Path

import pytest
from starlette.applications import Starlette

from conftest import build_configuration, write_export, живой_клиент
from mcp1c.dashboard_runtime import DASHBOARD_ON, routes
from mcp1c.intake_v2 import DurableCandidateStore, ExportIdentity
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


def _archive(name: str = "DemoConfiguration") -> bytes:
    payload = io.BytesIO()
    with zipfile.ZipFile(payload, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("Configuration.xml", _configuration(name))
    return payload.getvalue()


def _write_archive(path: Path, name: str = "DemoConfiguration") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(_archive(name))


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
    assert client.post(endpoint, headers=headers, json=request).status_code == 200
    assert not payload_path.exists()


def test_production_lifecycle_читает_browser_xml_как_incoming(tmp_path):
    IntakeApiService = _symbol("IntakeApiService")
    registry = Registry(tmp_path / "data")
    service = IntakeApiService.for_registry(
        registry, config_sources_root=tmp_path / "sources"
    )
    xml = (
        b"<Template><Data>"
        + b"A" * (2 * 1024 * 1024)
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
    real_list_jobs = records.list_jobs
    jobs_listed = threading.Event()
    continue_snapshot = threading.Event()

    def blocked_list_jobs():
        jobs = real_list_jobs()
        jobs_listed.set()
        assert continue_snapshot.wait(5)
        return jobs

    monkeypatch.setattr(records, "list_jobs", blocked_list_jobs)
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
