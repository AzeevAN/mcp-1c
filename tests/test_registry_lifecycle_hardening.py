"""Жизненный цикл реестра: reload, recovery, bootstrap и CLI-путь.

Все данные синтетические и живут в ``tmp_path``; рабочий ``data/`` не нужен.
"""

from __future__ import annotations

import errno
import json
from pathlib import Path

import pytest

from conftest import build_configuration, write_export, write_syntax
from mcp1c.intake_v2_registry import ExportIdentity
from mcp1c.registry import Registry, RegistryRecoveryBlocked
from mcp1c.source_modes import ActivationMode
from test_intake_v2_extensions import _stage_active_base
from test_intake_v2_legacy_cleanup import _native_generation
from test_intake_v2_registry import _manifest


def _mode(registry: Registry, name: str = "DemoConfiguration"):
    pointer = registry.active_generation_pointer(
        ExportIdentity.configuration(name)
    )
    assert pointer is not None and pointer.activation is not None
    return pointer.activation.mode


def test_bootstrap_той_же_a_не_откатывает_native_b_full(tmp_path):
    data = tmp_path / "data"
    (data / "bootstrap").mkdir(parents=True)
    write_export(data / "bootstrap", build_configuration(name="DemoConfiguration"))
    registry = Registry(data)
    registry.startup()
    assert "DemoConfiguration" in registry.sources

    registry.publish_generation(
        _stage_active_base(registry, _native_generation(tmp_path, "full"))
    )
    assert _mode(registry) is ActivationMode.B_FULL

    restarted = Registry(data)
    messages = restarted.startup()
    assert _mode(restarted) is ActivationMode.B_FULL, messages
    assert restarted.resolve("DemoConfiguration").modules is not None
    again = Registry(data)
    again.startup()
    assert _mode(again) is ActivationMode.B_FULL


def _blocked_generation_registry(tmp_path):
    """Реестр с WAL публикации, который нельзя однозначно разобрать."""
    first, first_payloads = _manifest(tmp_path, "generation-001")
    second, second_payloads = _manifest(
        tmp_path, "generation-002", suffix="-changed"
    )
    registry = Registry(tmp_path / "data")
    registry.publish_generation(registry.stage_generation(first, first_payloads))
    old_pointer = registry.active_generation_pointer(first.identity)
    staged = registry.stage_generation(second, second_payloads)
    registry.generation_recovery_path.write_text(
        json.dumps(
            {
                "previous": old_pointer.to_dict(),
                "staged": staged.pointer.to_dict(),
                "phase": "pointer_switched",
            }
        ),
        encoding="utf-8",
    )
    return registry, first


def test_битый_wal_поколения_закрывает_запись_до_восстановления(tmp_path):
    registry, first = _blocked_generation_registry(tmp_path)
    registry_before = registry.registry_path.read_bytes()
    wal_before = registry.generation_recovery_path.read_bytes()

    restarted = Registry(registry.data_dir)
    problems = restarted.startup()
    assert problems and "неоднознач" in problems[0]

    syntax = write_syntax(tmp_path / "incoming-syntax")
    with pytest.raises(RegistryRecoveryBlocked, match="только для чтения"):
        restarted.add_syntax(syntax)
    with pytest.raises(RegistryRecoveryBlocked, match="журнал публикации"):
        restarted.save()
    with pytest.raises(RegistryRecoveryBlocked):
        restarted.set_platform_version("DemoConfiguration", "8.3.27.2130")
    with pytest.raises(RegistryRecoveryBlocked):
        restarted.remove("DemoConfiguration")
    third, third_payloads = _manifest(tmp_path, "generation-003", suffix="-3")
    with pytest.raises(RegistryRecoveryBlocked):
        restarted.publish_generation(
            restarted.stage_generation(third, third_payloads)
        )

    assert restarted.registry_path.read_bytes() == registry_before
    assert restarted.generation_recovery_path.read_bytes() == wal_before

    # Флаг снимается только успешным восстановлением на следующем startup.
    restarted.generation_recovery_path.unlink()
    assert restarted.startup() == []
    assert restarted.active_generation(first.identity) == first
    restarted.add_syntax(syntax)
    restarted.save()


def test_битый_журнал_рокировки_закрывает_запись(
    tmp_path, архив_кода, корень_кода
):
    incoming = tmp_path / "input"
    incoming.mkdir()
    registry = Registry(tmp_path / "data")
    registry.add_configuration(
        write_export(incoming, build_configuration(name="Пример"))
    )
    source = registry.add_modules(архив_кода(корень_кода), configuration="Пример")
    registry._module_swap_path.write_text("[]", encoding="utf-8")
    registry_before = registry.registry_path.read_bytes()

    restarted = Registry(registry.data_dir)
    assert restarted.startup()
    assert source.id not in restarted.sources

    with pytest.raises(RegistryRecoveryBlocked, match="журнал рокировки"):
        restarted.add_syntax(write_syntax(tmp_path / "incoming-syntax"))
    with pytest.raises(RegistryRecoveryBlocked):
        restarted.save()
    with pytest.raises(RegistryRecoveryBlocked):
        restarted.add_modules(архив_кода(корень_кода), configuration="Пример")

    assert restarted.registry_path.read_bytes() == registry_before
    assert restarted._module_swap_path.read_text(encoding="utf-8") == "[]"

    restarted._module_swap_path.unlink()
    assert restarted.startup() == []
    assert source.id in restarted.sources
    restarted.save()


def test_журнал_рокировки_пишется_durable_writer(
    tmp_path, архив_кода, корень_кода, monkeypatch
):
    incoming = tmp_path / "input"
    incoming.mkdir()
    registry = Registry(tmp_path / "data")
    registry.add_configuration(
        write_export(incoming, build_configuration(name="Пример"))
    )
    записанные = []
    durable = registry._write_json_durably

    def spy(target, payload, **kwargs):
        записанные.append(target)
        return durable(target, payload, **kwargs)

    monkeypatch.setattr(registry, "_write_json_durably", spy)
    registry.add_modules(архив_кода(корень_кода), configuration="Пример")

    assert registry._module_swap_path in записанные
    assert not registry._module_swap_path.exists()


def _rows(registry: Registry) -> dict[str, dict]:
    payload = json.loads(registry.registry_path.read_text(encoding="utf-8"))
    return {raw["id"]: raw for raw in payload["sources"]}


def test_временный_сбой_restore_не_стирает_строки_registry(
    tmp_path, архив_кода, корень_кода, monkeypatch
):
    import mcp1c.registry as registry_module

    incoming = tmp_path / "input"
    incoming.mkdir()
    registry = Registry(tmp_path / "data")
    registry.add_configuration(
        write_export(incoming, build_configuration(name="Розница"))
    )
    registry.add_configuration(
        write_export(incoming, build_configuration(name="Пример"))
    )
    modules = registry.add_modules(архив_кода(корень_кода), configuration="Пример")
    before = _rows(registry)
    недоступный = registry._absolute(before["Пример"]["stored_path"])
    подлинный = registry_module._sha256

    def сбой_тома(path):
        if Path(path) == недоступный:
            raise OSError(errno.EIO, "синтетический сбой тома")
        return подлинный(path)

    monkeypatch.setattr(registry_module, "_sha256", сбой_тома)
    restarted = Registry(registry.data_dir)
    problems = restarted.startup()

    assert any("временный сбой" in problem for problem in problems)
    assert "Пример" not in restarted.configurations
    assert modules.id not in restarted.sources
    after = _rows(restarted)
    assert after["Пример"] == before["Пример"]
    assert after[modules.id] == before[modules.id]
    assert "Розница" in after

    monkeypatch.setattr(registry_module, "_sha256", подлинный)
    recovered = Registry(registry.data_dir)
    assert recovered.startup() == []
    assert recovered.wait_for_module_builds(timeout=30)
    assert "Пример" in recovered.configurations
    assert recovered.modules[modules.id].готов


def test_перенесённую_строку_можно_снять_явно(
    tmp_path, monkeypatch
):
    import mcp1c.registry as registry_module

    incoming = tmp_path / "input"
    incoming.mkdir()
    registry = Registry(tmp_path / "data")
    registry.add_configuration(
        write_export(incoming, build_configuration(name="Пример"))
    )
    registry.save()

    def сбой_тома(path):
        raise OSError(errno.EIO, "синтетический сбой тома")

    monkeypatch.setattr(registry_module, "_sha256", сбой_тома)
    restarted = Registry(registry.data_dir)
    restarted.startup()
    assert "Пример" in _rows(restarted)

    restarted.remove("Пример")
    assert "Пример" not in _rows(restarted)
