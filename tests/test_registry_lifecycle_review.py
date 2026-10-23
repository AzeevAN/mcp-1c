"""Гонки restore с remove/publish и судьба перенесённых строк registry.json.

Данные синтетические, в ``tmp_path``; рабочий ``data/`` не нужен.
"""

from __future__ import annotations

import errno
import json
import threading

import pytest

from conftest import build_configuration, write_export, write_syntax
from mcp1c.intake_v2_registry import ExportIdentity, RecoveryBlocked
from mcp1c.registry import (
    Registry,
    RegistryRecoveryBlocked,
    SourceInUseError,
)
from mcp1c.source_modes import ActivationMode
from test_intake_v2_extensions import _stage_active_base
from test_intake_v2_legacy_cleanup import _native_generation
from test_intake_v2_registry import _manifest


def _rows(registry: Registry) -> dict[str, dict]:
    payload = json.loads(registry.registry_path.read_text(encoding="utf-8"))
    return {raw["id"]: raw for raw in payload["sources"]}


def _задержать_native_фазу(registry: Registry, monkeypatch):
    """Остановить restore внутри native-фазы, пока она держит замок публикации."""
    внутри = threading.Event()
    отпустить = threading.Event()
    подлинный = registry._generation_pointers_from_payload

    def задержка(payload):
        if threading.current_thread().name == "restore":
            внутри.set()
            assert отпустить.wait(10)
        return подлинный(payload)

    monkeypatch.setattr(registry, "_generation_pointers_from_payload", задержка)
    return внутри, отпустить


def _после(registry: Registry, monkeypatch, поток: threading.Thread):
    """Legacy-проход restore начинается только после завершения ``поток``."""
    подлинный = registry.add_configuration

    def add_configuration(*args, **kwargs):
        if threading.current_thread().name == "restore":
            поток.join(10)
        return подлинный(*args, **kwargs)

    monkeypatch.setattr(registry, "add_configuration", add_configuration)


def _запустить_restore(registry: Registry) -> tuple[threading.Thread, list]:
    результат: list = []

    def запустить():
        try:
            результат.extend(registry.startup())
        except BaseException as error:  # pragma: no cover - диагностика
            результат.append(error)

    поток = threading.Thread(target=запустить, name="restore")
    поток.start()
    return поток, результат


def test_remove_во_время_native_фазы_не_воскрешает_legacy_конфигурацию(
    tmp_path, monkeypatch
):
    incoming = tmp_path / "input"
    incoming.mkdir()
    original = Registry(tmp_path / "data")
    original.add_configuration(
        write_export(incoming, build_configuration(name="Пример"))
    )
    original.save()

    registry = Registry(original.data_dir)
    registry.startup()
    assert "Пример" in registry.configurations
    # Reload: память уже содержит Пример, restore поднимает строку заново.
    внутри, отпустить = _задержать_native_фазу(registry, monkeypatch)
    restore, результат = _запустить_restore(registry)
    assert внутри.wait(10)

    remover = threading.Thread(target=registry.remove, args=("Пример",))
    _после(registry, monkeypatch, remover)
    # remove ждёт замок публикации, который держит native-фаза.
    remover.start()
    отпустить.set()
    restore.join(20)
    assert not restore.is_alive()
    assert not any(isinstance(item, BaseException) for item in результат)

    assert "Пример" not in registry.configurations
    assert "Пример" not in _rows(registry)
    assert "Пример" not in _rows(Registry(registry.data_dir))


def test_публикация_b_во_время_native_фазы_не_откатывается_до_a_only(
    tmp_path, monkeypatch
):
    external = tmp_path / "external"
    external.mkdir()
    archive = write_export(external, build_configuration(name="DemoConfiguration"))
    original = Registry(tmp_path / "data")
    # Внешний файл legacy cleanup не трогает: restore снова видит строку A.
    original.add_configuration(archive, keep_source=False)
    original.save()

    registry = Registry(original.data_dir)
    staged = _stage_active_base(registry, _native_generation(tmp_path, "full"))
    внутри, отпустить = _задержать_native_фазу(registry, monkeypatch)
    restore, результат = _запустить_restore(registry)
    assert внутри.wait(10)

    publisher = threading.Thread(target=registry.publish_generation, args=(staged,))
    _после(registry, monkeypatch, publisher)
    publisher.start()
    отпустить.set()
    restore.join(20)
    assert not restore.is_alive()
    assert not any(isinstance(item, BaseException) for item in результат)

    pointer = registry.active_generation_pointer(
        ExportIdentity.configuration("DemoConfiguration")
    )
    assert pointer.activation.mode is ActivationMode.B_FULL
    restarted = Registry(registry.data_dir)
    restarted.startup()
    assert restarted.active_generation_pointer(
        ExportIdentity.configuration("DemoConfiguration")
    ).activation.mode is ActivationMode.B_FULL


def test_прежний_вердикт_блокировки_держится_до_конца_restore(
    tmp_path, monkeypatch
):
    incoming = tmp_path / "input"
    incoming.mkdir()
    original = Registry(tmp_path / "data")
    original.add_configuration(
        write_export(incoming, build_configuration(name="Пример"))
    )
    original.save()
    before = original.registry_path.read_bytes()
    чужой = json.loads(before)
    чужой["registry_version"] = 999
    original.registry_path.write_text(json.dumps(чужой), encoding="utf-8")

    registry = Registry(original.data_dir)
    assert registry.startup()
    original.registry_path.write_bytes(before)

    внутри, отпустить = _задержать_native_фазу(registry, monkeypatch)
    restore, результат = _запустить_restore(registry)
    try:
        assert внутри.wait(10)
        # Файл уже исправлен, но память ещё неполна: запись из другого
        # потока не должна заменить registry.json частичным состоянием.
        with pytest.raises(RegistryRecoveryBlocked, match="идёт восстановление"):
            registry.save()
        assert registry.registry_path.read_bytes() == before
    finally:
        отпустить.set()
        restore.join(20)
    assert результат == []
    registry.save()
    assert "Пример" in _rows(registry)


def test_перенесённые_строки_видны_уборке_старта(tmp_path, monkeypatch):
    import mcp1c.registry as registry_module

    incoming = tmp_path / "input"
    incoming.mkdir()
    original = Registry(tmp_path / "data")
    configuration = original.add_configuration(
        write_export(incoming, build_configuration(name="Пример"))
    )
    syntax = original.add_syntax(write_syntax(tmp_path / "syntax-input"))
    original.save()
    syntax_index = original._absolute(syntax.stored_path)
    configuration_file = original._absolute(configuration.stored_path)
    cache_files = {
        path.name
        for path in original.cache_dir.iterdir()
        if path.name.startswith("Пример")
    }
    assert syntax_index.is_file() and configuration_file.is_file() and cache_files

    def сбой_тома(*_args, **_kwargs):
        raise OSError(errno.EIO, "синтетический сбой тома")

    monkeypatch.setattr(registry_module, "_sha256", сбой_тома)
    monkeypatch.setattr(registry_module, "load_syntax", сбой_тома)
    registry = Registry(original.data_dir)
    registry.startup()

    rows = _rows(registry)
    assert {"Пример", syntax.id} <= set(rows)
    assert syntax_index.is_file()
    assert configuration_file.is_file()
    assert cache_files <= {path.name for path in registry.cache_dir.iterdir()}
    assert configuration_file.resolve() not in {
        path.resolve() for path, _ in registry.orphan_sources()
    }
    with pytest.raises(SourceInUseError):
        registry.forget_source(configuration.stored_path)


def test_повреждённый_zip_не_считается_временным_сбоем(tmp_path):
    incoming = tmp_path / "input"
    incoming.mkdir()
    original = Registry(tmp_path / "data")
    configuration = original.add_configuration(
        write_export(incoming, build_configuration(name="Пример"))
    )
    original.save()
    stored = original._absolute(configuration.stored_path)
    stored.write_bytes(b"PK\x03\x04 not a zip")
    payload = json.loads(original.registry_path.read_text(encoding="utf-8"))
    import hashlib

    for raw in payload["sources"]:
        if raw["id"] == "Пример":
            raw["sha256"] = hashlib.sha256(stored.read_bytes()).hexdigest()
    original.registry_path.write_text(json.dumps(payload), encoding="utf-8")

    registry = Registry(original.data_dir)
    problems = registry.startup()

    assert any(problem.startswith("Пример:") for problem in problems)
    assert not any("временный сбой" in problem for problem in problems)
    assert "Пример" not in _rows(registry)


def test_recovery_blocked_внутри_публикации_закрывает_запись(
    tmp_path, monkeypatch
):
    first, first_payloads = _manifest(tmp_path, "generation-001")
    second, second_payloads = _manifest(tmp_path, "generation-002", suffix="-2")
    registry = Registry(tmp_path / "data")
    registry.publish_generation(registry.stage_generation(first, first_payloads))
    staged = registry.stage_generation(second, second_payloads)

    def отказ_записи(*_args, **_kwargs):
        raise OSError(errno.EIO, "синтетический отказ записи")

    def неоднозначно():
        raise RecoveryBlocked("синтетический неоднозначный WAL")

    monkeypatch.setattr(registry, "_write_registry_payload", отказ_записи)
    monkeypatch.setattr(registry, "recover_generation_publish", неоднозначно)
    with pytest.raises(RecoveryBlocked):
        registry.publish_generation(staged)
    monkeypatch.undo()

    with pytest.raises(RegistryRecoveryBlocked):
        registry.save()
