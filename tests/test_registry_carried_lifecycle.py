"""Перенесённые строки реестра сохраняют права источника при сбоях тома.

Все архивы, поколения и журналы синтетические и живут в ``tmp_path``.
"""

from __future__ import annotations

import json

import pytest
from starlette.applications import Starlette

from conftest import build_configuration, write_export, write_syntax, живой_клиент
from mcp1c import dashboard_backend as dashboard
from mcp1c.dashboard_runtime import DASHBOARD_ON, routes
from mcp1c.intake_v2_api import IntakeApiConflict, IntakeApiService
from mcp1c.intake_v2_operations import OperationConflict
from mcp1c.registry import Registry, RegistryRecoveryBlocked
from mcp1c.source_modes import ActivationMode
from test_dashboard_admin_api import _login
from test_intake_v2_api import _write_archive
from test_intake_v2_extensions import _materialized, _stage_active_base


def _реестр(registry):
    return json.loads(registry.registry_path.read_text(encoding="utf-8"))


def _унести_корень(tmp_path, registry, pointer):
    root = registry.data_dir / pointer.root_path
    unavailable = tmp_path / "недоступный-корень"
    root.rename(unavailable)
    return root, unavailable


def _вернуть_корень(root, unavailable):
    root.parent.mkdir(parents=True, exist_ok=True)
    unavailable.rename(root)


def _запустить_после_удаления(registry):
    messages = registry.startup()
    assert all(
        message.startswith("убрано из кэша индексов: ")
        or message.startswith("убрано разобранных справок: ")
        or (
            message.startswith("исходных файлов: ")
            and message.endswith("удаляются вручную")
        )
        for message in messages
    ), messages


def test_source_a_снимает_временно_недоступное_native_расширение(tmp_path):
    _, base = _materialized(tmp_path, "base")
    _, extension = _materialized(
        tmp_path, "extension", configuration_name="DemoExtension", extension=True,
    )
    original = Registry(tmp_path / "data")
    original.publish_generation(_stage_active_base(original, base))
    pointer = original.publish_generation(
        original.stage_generation(extension.manifest, extension.payloads)
    )
    initial = original.resolve("DemoConfiguration", extension="DemoExtension")
    assert initial.extension is not None
    assert initial.extension_roles is not None and initial.extension_roles.ready
    root, unavailable = _унести_корень(tmp_path, original, pointer)

    registry = Registry(original.data_dir)
    assert registry.startup()
    assert pointer.to_dict() in _реестр(registry)["generation_manifests"]
    assert registry.active_generation_pointer(extension.manifest.identity) is None
    assert registry.resolve("DemoConfiguration").modules is not None
    source_a = tmp_path / "source-a"
    source_a.mkdir()
    registry.add_configuration(write_export(
        source_a, build_configuration(name="DemoConfiguration"),
    ))
    registry.save()
    assert registry.active_generation_pointer(base.manifest.identity).activation.mode is ActivationMode.A_ONLY

    # Доступность прежнего каталога не даёт права вернуть снятое расширение.
    _вернуть_корень(root, unavailable)
    restarted = Registry(registry.data_dir)
    messages = restarted.startup()
    assert all(
        message.startswith("убрано из кэша индексов: ") for message in messages
    ), messages
    assert all(
        row["identity"] != pointer.to_dict()["identity"]
        for row in _реестр(restarted)["generation_manifests"]
    )
    context = restarted.resolve("DemoConfiguration", extension="DemoExtension")
    assert restarted.active_generation_pointer(extension.manifest.identity) is None
    assert "DemoExtension" not in restarted.snapshot().extension_names("DemoConfiguration")
    assert context.extension is None
    assert context.modules is None
    assert context.roles is None
    assert context.extension_roles is None
    assert "DemoConfiguration:ext:DemoExtension" not in restarted.modules


@pytest.mark.parametrize("version", ["1.0", "2.0"], ids=["та-же-редакция", "другая-редакция"])
def test_create_не_заменяет_зарегистрированную_недоступную_базу(tmp_path, version):
    _, base = _materialized(tmp_path, "registered")
    original = Registry(tmp_path / "data")
    pointer = original.publish_generation(_stage_active_base(original, base))
    root, unavailable = _унести_корень(tmp_path, original, pointer)
    registry = Registry(original.data_dir)
    assert registry.startup()
    before = _реестр(registry)["generation_manifests"]
    assert before == [pointer.to_dict()]
    assert "DemoConfiguration" not in registry.snapshot().configuration_names

    _write_archive(registry.incoming_dir / "incoming.zip", version=version)
    service = IntakeApiService.for_registry(registry, directory_settle_seconds=0)
    candidate = service.snapshot()["candidates"][0]
    refused = False
    try:
        work = service.start(candidate["id"], "create")
        service.prepare(work)
        service.confirm(work.job_id)
    except IntakeApiConflict:
        refused = True

    # Прямой create тоже проверяется: отказ в интерфейсе не защищает commit.
    assert _реестр(registry)["generation_manifests"] == before
    assert refused, "CREATE должен отказать до замены зарегистрированного pointer"
    assert "create" not in candidate["actions"]
    _вернуть_корень(root, unavailable)
    restarted = Registry(registry.data_dir)
    assert restarted.startup() == []
    assert restarted.active_generation_pointer(base.manifest.identity) == pointer


def test_create_не_заменяет_carried_базу_появившуюся_после_preview(tmp_path):
    registry = Registry(tmp_path / "data")
    _write_archive(registry.incoming_dir / "incoming.zip")
    service = IntakeApiService.for_registry(registry, directory_settle_seconds=0)
    candidate = service.snapshot()["candidates"][0]
    assert candidate["actions"] == ["create"]
    work = service.start(candidate["id"], "create")
    service.prepare(work)

    # Preview принят при отсутствии базы. Другая загрузка успевает принять
    # поколение, после чего его том становится недоступным до confirm.
    _, base = _materialized(tmp_path, "registered-after-preview")
    pointer = registry.publish_generation(_stage_active_base(registry, base))
    root, unavailable = _унести_корень(tmp_path, registry, pointer)
    assert registry.restore()
    before = _реестр(registry)["generation_manifests"]
    assert before == [pointer.to_dict()]
    assert registry.active_generation_pointer(base.manifest.identity) is None
    assert "DemoConfiguration" not in registry.snapshot().configuration_names

    with pytest.raises(OperationConflict, match="active generation"):
        service.confirm(work.job_id)

    assert _реестр(registry)["generation_manifests"] == before
    _вернуть_корень(root, unavailable)
    restarted = Registry(registry.data_dir)
    assert restarted.startup() == []
    assert restarted.active_generation_pointer(base.manifest.identity) == pointer


@pytest.mark.parametrize("kind", ["configuration", "syntax"], ids=["source-a", "справка"])
def test_обычный_restore_сохраняет_ещё_не_записанный_legacy_источник(tmp_path, kind):
    registry = Registry(tmp_path / "data")
    registry.save()
    if kind == "configuration":
        incoming = tmp_path / "source-a"
        incoming.mkdir()
        source = registry.add_configuration(write_export(
            incoming, build_configuration(name="Пример"),
        ))
    else:
        source = registry.add_syntax(write_syntax(tmp_path / "syntax"))
    assert _реестр(registry)["sources"] == []

    assert registry.restore() == []

    assert source.id in registry.sources
    if kind == "configuration":
        assert source.id in registry.snapshot().configuration_names
    else:
        assert source.id in registry.syntax_versions
        assert registry.syntax is not None
    registry.save()
    assert source.id in {row["id"] for row in _реестр(registry)["sources"]}
    restarted = Registry(registry.data_dir)
    assert restarted.startup() == []
    assert source.id in restarted.sources
    if kind == "configuration":
        assert source.id in restarted.snapshot().configuration_names
    else:
        assert restarted.syntax is not None


@pytest.mark.parametrize("kind", ["configuration", "syntax"], ids=["source-a", "справка"])
def test_remove_после_видимой_записи_не_воскрешает_legacy_источник(
    tmp_path, monkeypatch, kind,
):
    registry = Registry(tmp_path / "data")
    if kind == "configuration":
        incoming = tmp_path / "source-a"
        incoming.mkdir()
        source = registry.add_configuration(write_export(
            incoming, build_configuration(name="Пример"),
        ))
    else:
        source = registry.add_syntax(write_syntax(tmp_path / "syntax"))
    registry.save()
    source_id = source.id
    assert source_id in {row["id"] for row in _реестр(registry)["sources"]}
    original_write = registry._write_json_durably
    failure = OSError("Синтетический отказ после замены registry.json")

    def сбой_после_замены(target, payload, **kwargs):
        result = original_write(target, payload, **kwargs)
        if target == registry.registry_path:
            raise failure
        return result

    monkeypatch.setattr(registry, "_write_json_durably", сбой_после_замены)
    with pytest.raises(OSError) as raised:
        registry.remove(source_id)
    assert raised.value is failure
    assert source_id not in {row["id"] for row in _реестр(registry)["sources"]}
    after_failure = registry.registry_path.read_bytes()
    monkeypatch.setattr(registry, "_write_json_durably", original_write)
    with pytest.raises(RegistryRecoveryBlocked):
        registry.save()
    assert registry.registry_path.read_bytes() == after_failure

    assert registry.restore() == []
    assert source_id not in registry.sources
    if kind == "configuration":
        assert source_id not in registry.snapshot().configuration_names
    else:
        assert source_id not in registry.syntax_versions
        assert registry.syntax is None
    registry.save()
    assert source_id not in {row["id"] for row in _реестр(registry)["sources"]}
    restarted = Registry(registry.data_dir)
    _запустить_после_удаления(restarted)
    assert source_id not in restarted.sources
    if kind == "configuration":
        assert source_id not in restarted.snapshot().configuration_names
    else:
        assert restarted.syntax is None


def test_http_remove_удаляет_carried_pointer_при_наличии_intake_job(
    tmp_path, monkeypatch,
):
    monkeypatch.setenv("ADMIN_TOKEN", "synthetic-admin-token")
    monkeypatch.delenv("API_TOKEN", raising=False)
    monkeypatch.setattr(dashboard, "JOBS", [])
    original = Registry(tmp_path / "data")
    service = IntakeApiService.for_registry(original, directory_settle_seconds=0)
    _write_archive(original.incoming_dir / "base.zip")
    candidate = service.snapshot()["candidates"][0]
    work = service.start(candidate["id"], "create")
    service.prepare(work)
    service.confirm(work.job_id)
    name = "DemoConfiguration"
    pointer = next(iter(original.snapshot().generations.values())).pointer
    assert service.has_configuration_jobs(name)
    root, unavailable = _унести_корень(tmp_path, original, pointer)

    registry = Registry(original.data_dir)
    assert registry.startup()
    assert pointer.to_dict() in _реестр(registry)["generation_manifests"]
    assert name not in registry.snapshot().configuration_names
    service = IntakeApiService.for_registry(registry, directory_settle_seconds=0)
    assert service.has_configuration_jobs(name)
    client = живой_клиент(Starlette(routes=routes(
        registry, mode=DASHBOARD_ON, intake=service,
    )))
    _login(client, "synthetic-admin-token")
    response = client.post("/api/v1/sources/remove", json={
        "id": name, "confirmation": name,
    })
    assert response.status_code == 200, response.text
    assert all(
        row["identity"] != pointer.to_dict()["identity"]
        for row in _реестр(registry).get("generation_manifests", [])
    )
    assert not service.has_configuration_jobs(name)

    _вернуть_корень(root, unavailable)
    restarted = Registry(registry.data_dir)
    _запустить_после_удаления(restarted)
    assert name not in restarted.snapshot().configuration_names
    assert restarted.active_generation_pointer(pointer.identity) is None
