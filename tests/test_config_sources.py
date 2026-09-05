"""Привязки каталогов: независимость, перезапуск и запрет подмены."""
from pathlib import Path
import os
import pytest
from conftest import build_configuration, write_export
from test_intake_v2_collector import _configuration
from mcp1c.intake_v2_api import IntakeApiService, IntakeApiConflict
from mcp1c.intake_v2 import ExportIdentity
from mcp1c.registry import Registry


def setup_sources(tmp_path):
    registry = Registry(tmp_path / "data")
    roots = tmp_path / "mounts"
    roots.mkdir()
    for i in range(5):
        name = f"Demo{i}"
        source = tmp_path / f"base{i}"
        source.mkdir()
        registry.add_configuration(write_export(source, build_configuration(name=name)), keep_source=False)
        folder = roots / f"config-{i}"
        folder.mkdir()
        (folder / "Configuration.xml").write_bytes(_configuration(name))
    service = IntakeApiService.for_registry(registry, config_sources_root=roots, directory_settle_seconds=0)
    return registry, roots, service


def test_five_bindings_restart_and_no_background_read(tmp_path, monkeypatch):
    registry, roots, service = setup_sources(tmp_path)
    for i in range(5):
        service.bind_directory(f"Demo{i}", f"config-{i}")
    restarted = IntakeApiService.for_registry(registry, config_sources_root=roots, directory_settle_seconds=0)
    monkeypatch.setattr(restarted.lifecycle, "_open", lambda *a: pytest.fail("GET не читает выгрузки"))
    state = restarted.directory_sources()
    assert state["roots"] == [f"config-{i}" for i in range(5)]
    assert state["bindings"] == {f"Demo{i}": f"config-{i}" for i in range(5)}
    assert not restarted.lifecycle.operations.records.list_jobs()


def test_bind_mismatch_missing_symlink_and_path_escape(tmp_path):
    registry, roots, service = setup_sources(tmp_path)
    def disk_state():
        # Чтение может изменить atime, но отказ не должен создавать даже пустые
        # записи, менять содержимое или mtime существующего реестра/выгрузки.
        return {
            str(path.relative_to(tmp_path)): (
                path.lstat().st_mode,
                path.lstat().st_mtime_ns,
                os.readlink(path) if path.is_symlink()
                else path.read_bytes() if path.is_file() else None,
            )
            for path in tmp_path.rglob("*")
        }

    def rejected_without_writes(configuration, root):
        before = disk_state()
        with pytest.raises(IntakeApiConflict):
            service.bind_directory(configuration, root)
        assert disk_state() == before

    for root in ("config-1", "missing", "../base0", "/tmp", "config-0/child"):
        rejected_without_writes("Demo0", root)
    rejected_without_writes("UnknownConfiguration", "config-0")
    (roots / "link").symlink_to(roots / "config-0", target_is_directory=True)
    assert "link" not in service.directory_sources()["roots"]
    rejected_without_writes("Demo0", "link")
    assert service.directory_sources()["bindings"] == {}
    assert not (registry.data_dir / "config-source-bindings.json").exists()
    assert not service.lifecycle.operations.records.list_jobs()
    # Ошибочная замена не должна портить уже сохранённую корректную привязку.
    service.bind_directory("Demo0", "config-0")
    rejected_without_writes("Demo0", "config-1")
    assert service.directory_sources()["bindings"] == {"Demo0": "config-0"}


def test_replacement_rejected_at_refresh_and_confirm(tmp_path):
    registry, roots, service = setup_sources(tmp_path)
    service.bind_directory("Demo0", "config-0")
    descriptor = roots / "config-0" / "Configuration.xml"
    descriptor.write_bytes(_configuration("Demo1"))
    with pytest.raises(IntakeApiConflict, match="конфигурац"):
        service.refresh_directory("Demo0")
    descriptor.write_bytes(_configuration("Demo0"))
    candidate = service.refresh_directory("Demo0")
    work = service.start(candidate["id"], "update_full")
    service.prepare(work)
    before = registry.active_generation_pointer(ExportIdentity.configuration("Demo0"))
    descriptor.write_bytes(_configuration("Demo1"))
    with pytest.raises(IntakeApiConflict):
        service.confirm(work.job_id)
    assert registry.active_generation_pointer(ExportIdentity.configuration("Demo0")) == before


def test_update_restart_noop_and_same_stat_change(tmp_path):
    registry, roots, service = setup_sources(tmp_path)
    service.bind_directory("Demo0", "config-0")
    candidate = service.refresh_directory("Demo0")
    work = service.start(candidate["id"], "update_full")
    service.prepare(work)
    restarted = IntakeApiService.for_registry(registry, config_sources_root=roots, directory_settle_seconds=0)
    assert restarted.confirm(work.job_id)["commit"] is not None
    candidate = restarted.refresh_directory("Demo0")
    work = restarted.start(candidate["id"], "update_full")
    restarted.prepare(work)
    assert restarted.job_payload(work.job_id)["preview"]["no_op"]
    descriptor = roots / "config-0" / "Configuration.xml"
    info = descriptor.stat()
    descriptor.write_bytes(descriptor.read_bytes().replace(b"1.0", b"2.0"))
    os.utime(descriptor, ns=(info.st_atime_ns, info.st_mtime_ns))
    with pytest.raises(IntakeApiConflict):
        restarted.confirm(work.job_id)


def test_unbind_invalidates_preview_without_deleting_source(tmp_path):
    registry, roots, service = setup_sources(tmp_path)
    service.bind_directory("Demo0", "config-0")
    candidate = service.refresh_directory("Demo0")
    work = service.start(candidate["id"], "update_full")
    service.prepare(work)
    service.unbind_directory("Demo0")
    with pytest.raises(IntakeApiConflict):
        service.confirm(work.job_id)
    assert (roots / "config-0" / "Configuration.xml").exists()


def test_http_admin_binding_refresh_and_validation(tmp_path, monkeypatch):
    from starlette.applications import Starlette
    from conftest import живой_клиент
    from mcp1c.dashboard_runtime import routes, DASHBOARD_ON
    monkeypatch.setenv("ADMIN_TOKEN", "admin-token")
    monkeypatch.setenv("API_TOKEN", "read-token")
    registry, roots, service = setup_sources(tmp_path)
    client = живой_клиент(Starlette(routes=routes(registry, mode=DASHBOARD_ON, intake=service)))
    url = "/api/v1/sources/directories"
    headers = {"x-api-token": "admin-token"}
    assert client.get(url).status_code == 403
    assert client.get(url, headers={"x-api-token": "read-token"}).status_code == 403
    assert client.get(url, headers=headers).json()["roots"] == [f"config-{i}" for i in range(5)]
    assert client.post(url + "/bind", json={"configuration": "Demo0", "source_id": "config-0"}).status_code == 403
    assert client.post(url + "/bind", headers=headers, json={"configuration": "Demo0", "source_id": "config-0", "path": "/tmp"}).status_code == 422
    assert client.post(url + "/bind", headers=headers, json={"configuration": "Demo0", "source_id": "config-1"}).status_code == 409
    assert client.post(url + "/bind", headers=headers, json={"configuration": "Demo0", "source_id": "config-0"}).status_code == 200
    candidate = client.post(url + "/refresh", headers=headers, json={"configuration": "Demo0"})
    assert candidate.status_code == 200
    assert candidate.json()["candidate"]["internal_name"] == "Demo0"
    assert str(tmp_path) not in candidate.text
    assert client.post(url + "/unbind", headers=headers, json={"configuration": "Demo0"}).status_code == 200
    service.bind_directory("Demo1", "config-1")
    removed = client.post("/api/v1/sources/remove", headers=headers,
                          json={"id": "Demo1", "confirmation": "Demo1"})
    assert removed.status_code == 200
    assert "Demo1" not in service.bindings.load()


def test_code_changes_are_visible_only_after_publication(tmp_path):
    from mcp1c.tools import get_procedure
    registry, roots, service = setup_sources(tmp_path)
    module = roots / "config-0" / "CommonModules" / "Demo" / "Ext" / "Module.bsl"
    module.parent.mkdir(parents=True)
    module.write_text('Процедура Проверка() Экспорт\n Сообщить("до");\nКонецПроцедуры', encoding="utf-8")
    service.bind_directory("Demo0", "config-0")

    def prepare():
        candidate = service.refresh_directory("Demo0")
        work = service.start(candidate["id"], "update_full")
        service.prepare(work)
        return work

    work = prepare()
    service.confirm(work.job_id)
    address = "ОбщийМодуль.Demo::Проверка"
    assert '"до"' in get_procedure(registry, address, config="Demo0")
    module.write_text('Процедура Проверка() Экспорт\n Сообщить("после");\nКонецПроцедуры', encoding="utf-8")
    work = prepare()
    assert not service.job_payload(work.job_id)["preview"]["no_op"]
    assert '"до"' in get_procedure(registry, address, config="Demo0")
    service.confirm(work.job_id)
    assert '"после"' in get_procedure(registry, address, config="Demo0")
    module.unlink()
    work = prepare()
    assert '"после"' in get_procedure(registry, address, config="Demo0")
    service.confirm(work.job_id)
    assert '"после"' not in get_procedure(registry, address, config="Demo0")
