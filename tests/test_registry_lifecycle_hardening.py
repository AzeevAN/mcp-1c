"""Жизненный цикл реестра: reload, recovery, bootstrap и CLI-путь.

Все данные синтетические и живут в ``tmp_path``; рабочий ``data/`` не нужен.
"""

from __future__ import annotations

import errno
import json
import os
import threading
from pathlib import Path

import pytest

from conftest import build_configuration, write_export, write_syntax
from mcp1c.intake_v2_registry import ExportIdentity
from mcp1c.registry import Registry, RegistryError, RegistryRecoveryBlocked
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


def test_reload_ждёт_публикацию_поколения_и_не_откатывает_staging(
    tmp_path, monkeypatch
):
    first, first_payloads = _manifest(tmp_path, "generation-001")
    second, second_payloads = _manifest(
        tmp_path, "generation-002", suffix="-changed"
    )
    registry = Registry(tmp_path / "data")
    registry.publish_generation(registry.stage_generation(first, first_payloads))
    staged = registry.stage_generation(second, second_payloads)

    в_середине = threading.Event()
    продолжить = threading.Event()
    подлинная_сборка = registry._build_native_generation_runtime

    def остановленная_сборка(*args, **kwargs):
        # Останавливается только публикатор: WAL уже записан, staging ещё
        # не продвинут. Восстановление reload собирает runtime без паузы.
        if threading.current_thread().name == "publisher":
            в_середине.set()
            assert продолжить.wait(10)
        return подлинная_сборка(*args, **kwargs)

    monkeypatch.setattr(
        registry, "_build_native_generation_runtime", остановленная_сборка
    )
    ошибки: list[BaseException] = []

    def опубликовать():
        try:
            registry.publish_generation(staged)
        except BaseException as error:  # pragma: no cover - диагностика
            ошибки.append(error)

    publisher = threading.Thread(target=опубликовать, name="publisher")
    publisher.start()
    reload = threading.Thread(target=registry.startup, name="reload")
    try:
        assert в_середине.wait(10)
        reload.start()
        reload.join(0.5)

        # Reload ждёт публикацию, а не разбирает её живой WAL как аварийный.
        reload_ждал = reload.is_alive()
        staging_цел = staged.root.exists()
        wal_цел = registry.generation_recovery_path.exists()
    finally:
        продолжить.set()
        publisher.join(10)
    assert reload_ждал
    assert staging_цел and wal_цел
    reload.join(10)
    assert not publisher.is_alive() and not reload.is_alive()
    assert ошибки == []
    assert registry.active_generation(second.identity) == second

    restarted = Registry(registry.data_dir)
    assert restarted.startup() == []
    assert restarted.active_generation(second.identity) == second
    pointer = restarted.active_generation_pointer(second.identity)
    restarted._generation_store.verify(pointer)


def _холодный_реестр_с_кодом(tmp_path, архив_кода, корень_кода):
    incoming = tmp_path / "input"
    incoming.mkdir()
    registry = Registry(tmp_path / "data")
    registry.add_configuration(
        write_export(incoming, build_configuration(name="Пример"))
    )
    source = registry.add_modules(архив_кода(корень_кода), configuration="Пример")
    # Без кэша startup строит индекс кода фоном, источник в `loading`.
    for path in registry.cache_dir.glob("*modules*"):
        path.unlink()
    return registry, source


def _задержать_фоновую_сборку(registry, monkeypatch):
    начата = threading.Event()
    отпустить = threading.Event()
    подлинная = registry._построить_индекс_кода

    def сборка(*args, **kwargs):
        if threading.current_thread().name.startswith("modules:"):
            начата.set()
            assert отпустить.wait(10)
        return подлинная(*args, **kwargs)

    monkeypatch.setattr(registry, "_построить_индекс_кода", сборка)
    return начата, отпустить


def test_провал_reparse_во_время_холодной_сборки_не_оставляет_loading(
    tmp_path, архив_кода, корень_кода, monkeypatch
):
    registry, source = _холодный_реестр_с_кодом(
        tmp_path, архив_кода, корень_кода
    )
    restarted = Registry(registry.data_dir)
    начата, отпустить = _задержать_фоновую_сборку(restarted, monkeypatch)
    try:
        restarted.startup()
        assert начата.wait(10)
        assert not restarted.modules[source.id].готов

        def сбой_распаковки(*_args, **_kwargs):
            raise OSError(errno.EIO, "синтетический сбой распаковки")

        monkeypatch.setattr(restarted, "_extract_to_temp", сбой_распаковки)
        with pytest.raises(RegistryError, match="прежний разбор сохранён"):
            restarted.add_modules(архив_кода(корень_кода), configuration="Пример")
    finally:
        отпустить.set()
    assert restarted.wait_for_module_builds(timeout=30)

    # Отменённая reparse-ом холодная сборка перезапущена: источник готов.
    assert restarted.modules[source.id].готов
    assert restarted.sources[source.id].status == "ready"


def test_reload_пересобирает_застрявший_неготовый_код(
    tmp_path, архив_кода, корень_кода, monkeypatch
):
    registry, source = _холодный_реестр_с_кодом(
        tmp_path, архив_кода, корень_кода
    )
    restarted = Registry(registry.data_dir)
    подлинная = restarted._построить_индекс_кода
    отказов = {"осталось": 1}

    def сборка(*args, **kwargs):
        if (
            threading.current_thread().name.startswith("modules:")
            and отказов["осталось"]
        ):
            отказов["осталось"] -= 1
            raise OSError(errno.EIO, "синтетический сбой сборки")
        return подлинная(*args, **kwargs)

    monkeypatch.setattr(restarted, "_построить_индекс_кода", сборка)
    restarted.startup()
    assert restarted.wait_for_module_builds(timeout=30)
    assert not restarted.modules[source.id].готов
    assert restarted.sources[source.id].status == "error"

    restarted.startup()
    assert restarted.wait_for_module_builds(timeout=30)
    assert restarted.modules[source.id].готов


def test_cli_reg_add_проходит_страховку_startup(tmp_path, capsys):
    from mcp1c.cli import main

    registry, _first = _blocked_generation_registry(tmp_path)
    registry_before = registry.registry_path.read_bytes()
    wal_before = registry.generation_recovery_path.read_bytes()
    incoming = tmp_path / "cli-input"
    incoming.mkdir()
    archive = write_export(incoming, build_configuration(name="Другая"))

    code = main(["reg-add", str(archive), "--data", str(registry.data_dir)])

    assert code == 2
    assert "только для чтения" in capsys.readouterr().err
    assert registry.registry_path.read_bytes() == registry_before
    assert registry.generation_recovery_path.read_bytes() == wal_before

    registry.generation_recovery_path.unlink()
    assert main(["reg-add", str(archive), "--data", str(registry.data_dir)]) == 0
    assert "Другая" in _rows(Registry(registry.data_dir))


def _открытых_дескрипторов() -> int:
    return len(os.listdir("/dev/fd"))


@pytest.mark.skipif(not Path("/dev/fd").is_dir(), reason="нужен /dev/fd")
def test_отказ_открыть_исходник_не_оставляет_дескриптор(tmp_path, monkeypatch):
    incoming = tmp_path / "input"
    incoming.mkdir()
    archive = write_export(incoming, build_configuration(name="Пример"))
    registry = Registry(tmp_path / "data")
    подлинный_open = Path.open

    def отказ(self, *args, **kwargs):
        if self == archive:
            raise PermissionError(errno.EACCES, "синтетический отказ")
        return подлинный_open(self, *args, **kwargs)

    monkeypatch.setattr(Path, "open", отказ)
    before = _открытых_дескрипторов()
    for _ in range(5):
        with registry._source_file_operation(archive), pytest.raises(
            RegistryError, match="не удалось безопасно сохранить"
        ):
            registry._store_source(
                archive, "configurations", source_id="Пример", digest="0" * 64
            )
    assert _открытых_дескрипторов() == before
    assert not list((registry.sources_dir / "configurations").glob(".*.tmp"))


def test_снос_старого_кода_не_стирает_чужой_журнал_рокировки(
    tmp_path, архив_кода, корень_кода, monkeypatch
):
    incoming = tmp_path / "input"
    incoming.mkdir()
    registry = Registry(tmp_path / "data")
    registry.add_configuration(
        write_export(incoming, build_configuration(name="Пример"))
    )
    registry.add_modules(архив_кода(корень_кода), configuration="Пример")
    чужой = {"version": 1, "source_id": "Другая:modules", "temporary": "x"}
    подлинный_снос = registry._drop_modules_root

    def снос_с_чужой_операцией(корень):
        # Пока старый каталог сносится вне замков, другая операция кода
        # успевает записать свой журнал перед собственной рокировкой.
        registry._module_swap_path.write_text(
            json.dumps(чужой), encoding="utf-8"
        )
        return подлинный_снос(корень)

    monkeypatch.setattr(registry, "_drop_modules_root", снос_с_чужой_операцией)
    registry.add_modules(архив_кода(корень_кода), configuration="Пример")

    assert json.loads(
        registry._module_swap_path.read_text(encoding="utf-8")
    ) == чужой


def test_registry_другой_версии_не_затирается(tmp_path):
    data = tmp_path / "data"
    data.mkdir()
    registry_path = data / "registry.json"
    registry_path.write_text(
        json.dumps({"registry_version": 99, "sources": [{"id": "будущее"}]}),
        encoding="utf-8",
    )
    before = registry_path.read_bytes()

    registry = Registry(data)
    messages = registry.startup()

    assert any("версии 99" in message for message in messages)
    assert registry_path.read_bytes() == before
    with pytest.raises(RegistryRecoveryBlocked, match="версии 99"):
        registry.save()
    assert registry_path.read_bytes() == before


def test_startup_подметает_осиротевшие_schema_v1_base(tmp_path):
    data = tmp_path / "data"
    сирота = data / ".schema-v1-base-crash"
    сирота.mkdir(parents=True)
    (сирота / "base_structure.json").write_text("{}", encoding="utf-8")

    Registry(data).startup()

    assert not сирота.exists()


def test_недоступный_снимок_расширений_даёт_ошибку_контракта(tmp_path):
    from mcp1c.extension_runtime import (
        ExtensionRuntimeError,
        load_extension_runtime,
    )

    каталог = tmp_path / "snapshot.json"
    каталог.mkdir()
    with pytest.raises(ExtensionRuntimeError, match="недоступен"):
        load_extension_runtime(каталог)
