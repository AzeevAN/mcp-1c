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
    # Пропуск A назван в стартовых сообщениях, а не молчалив.
    assert any(
        message.startswith("bootstrap: ") and "пропущен" in message
        for message in messages
    ), messages
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
    messages = restarted.startup()
    # Отклонённые публикации могли оставить staging; его уборка — сообщение,
    # а не продолжение блокировки. Ни одна ошибка восстановления не скрывается.
    assert all(message.startswith("убрано временных поколений: ") for message in messages)
    assert restarted.active_generation(first.identity) == first
    restarted.add_syntax(syntax)
    restarted.save()


def _rows(registry: Registry) -> dict[str, dict]:
    payload = json.loads(registry.registry_path.read_text(encoding="utf-8"))
    return {raw["id"]: raw for raw in payload["sources"]}


def test_временный_сбой_restore_не_стирает_строки_registry(
    tmp_path, monkeypatch
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
    registry.save()
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
    after = _rows(restarted)
    assert after["Пример"] == before["Пример"]
    assert "Розница" in after

    monkeypatch.setattr(registry_module, "_sha256", подлинный)
    recovered = Registry(registry.data_dir)
    assert recovered.startup() == []
    assert "Пример" in recovered.configurations


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


def test_невалидный_utf8_registry_json_при_wal_закрывает_запись(tmp_path):
    registry, _first = _blocked_generation_registry(tmp_path)
    registry.registry_path.write_bytes(b'{"registry_version": 1, "x": "\xff"}')
    before = registry.registry_path.read_bytes()

    restarted = Registry(registry.data_dir)
    problems = restarted.startup()

    assert problems and "неоднознач" in problems[0]
    with pytest.raises(RegistryRecoveryBlocked):
        restarted.save()
    assert restarted.registry_path.read_bytes() == before


def test_startup_owner_удаляет_брошенный_staging_без_wal_только_в_generations(tmp_path):
    data = tmp_path / 'data'
    orphan = data / 'generations' / '.staging-orphan-123'
    orphan.mkdir(parents=True)
    (orphan / 'partial-payload').write_bytes(b'incomplete generation')
    preserved = data / 'generations' / 'preserved-generation'
    preserved.mkdir()
    (preserved / 'marker').write_bytes(b'keep generation')
    outside = data / '.staging-outside-generations'
    outside.mkdir()
    (outside / 'marker').write_bytes(b'keep outside')
    linked = data / 'generations' / '.staging-link'
    linked.symlink_to(outside, target_is_directory=True)
    registry = Registry(data)
    assert not registry.generation_recovery_path.exists()

    registry.startup()

    assert registry.role == 'owner'
    assert not orphan.exists()
    assert (preserved / 'marker').read_bytes() == b'keep generation'
    assert (outside / 'marker').read_bytes() == b'keep outside'
    assert linked.is_symlink()


def test_startup_не_удаляет_staging_на_который_ссылается_неразрешимый_wal(tmp_path):
    from mcp1c.intake_v2 import RecoveryPhase

    first, first_payloads = _manifest(tmp_path, 'l7-first')
    second, second_payloads = _manifest(tmp_path, 'l7-second', suffix='-changed')
    setup = Registry(tmp_path / 'data')
    setup.publish_generation(setup.stage_generation(first, first_payloads))
    previous = setup.active_generation_pointer(first.identity)
    assert previous is not None
    staged = setup.stage_generation(second, second_payloads)
    assert staged.root.name.startswith('.staging-')
    record = setup._generation_store.recovery_for(
        previous, staged, RecoveryPhase.POINTER_SWITCHED,
    )
    setup._generation_store.write_recovery(record)
    wal_before = setup.generation_recovery_path.read_bytes()
    registry_before = setup.registry_path.read_bytes()
    staged_files = {
        path.relative_to(staged.root): path.read_bytes()
        for path in staged.root.rglob('*') if path.is_file()
    }
    assert staged_files

    restarted = Registry(setup.data_dir)
    messages = restarted.startup()

    # POINTER_SWITCHED при всё ещё старом active pointer не допускает
    # произвольного выбора поколения: recovery блокирует запись и уборку.
    assert messages
    assert any('неоднознач' in message for message in messages)
    assert staged.root.is_dir()
    assert {
        path.relative_to(staged.root): path.read_bytes()
        for path in staged.root.rglob('*') if path.is_file()
    } == staged_files
    assert setup.generation_recovery_path.read_bytes() == wal_before
    assert setup.registry_path.read_bytes() == registry_before


def test_startup_guest_не_удаляет_staging_при_живом_owner(tmp_path):
    from test_registry_interprocess import _start_server, _stop

    data = tmp_path / 'data'
    data.mkdir()
    owner = _start_server(data)
    try:
        # Создаём после owner.startup, чтобы проверялся именно guest startup.
        orphan = data / 'generations' / '.staging-guest-visible-123'
        orphan.mkdir(parents=True)
        marker = orphan / 'partial-payload'
        marker.write_bytes(b'owner may still use this staging')
        guest = Registry(data)

        guest.startup()

        assert guest.role == 'guest'
        assert orphan.is_dir()
        assert marker.read_bytes() == b'owner may still use this staging'
    finally:
        _stop(owner)


def test_reload_сохраняет_живой_staging_и_убирает_его_после_освобождения(tmp_path):
    import gc

    manifest, payloads = _manifest(tmp_path, 'l7-live')
    registry = Registry(tmp_path / 'data')
    registry.startup()
    staged = registry.stage_generation(manifest, payloads)
    root = staged.root
    before = {
        path.relative_to(root): path.read_bytes()
        for path in root.rglob('*') if path.is_file()
    }
    assert before

    registry.startup()

    assert root.is_dir()
    assert {
        path.relative_to(root): path.read_bytes()
        for path in root.rglob('*') if path.is_file()
    } == before
    del staged
    gc.collect()

    registry.startup()

    assert not root.exists()


# R4 (ревью реализации аудита): подготовка поколения не держит
# `_generation_mutation_lock` на копировании и хешах, но каталог `.staging-*`
# защищён от уборки L7 с момента создания.

def _pause_staging_copy(registry, *, fail: bool = False):
    paused, resume = threading.Event(), threading.Event()
    store = registry._generation_store

    def pausing(original):
        def wrapper(*args, **kwargs):
            if not paused.is_set():
                paused.set()
                assert resume.wait(10), 'тест не отпустил staging'
                if fail:
                    raise OSError(errno.EIO, 'искусственный сбой staging')
            return original(*args, **kwargs)
        return wrapper

    store._stage_envelope = pausing(store._stage_envelope)
    store._copy_layer = pausing(store._copy_layer)
    return paused, resume


def _staging_dirs(registry):
    root = registry._generation_store.root
    return sorted(path for path in root.glob('.staging-*') if path.is_dir())


def test_r4_reload_не_ждёт_подготовку_поколения_и_не_трогает_её_staging(tmp_path):
    manifest, payloads = _manifest(tmp_path, 'r4-live')
    registry = Registry(tmp_path / 'data')
    registry.startup()
    paused, resume = _pause_staging_copy(registry)
    result: dict[str, object] = {}

    def stage():
        try:
            result['staged'] = registry.stage_generation(manifest, payloads)
        except BaseException as error:  # pragma: no cover - диагностика
            result['error'] = error

    stager = threading.Thread(target=stage)
    stager.start()
    try:
        assert paused.wait(10), 'staging не дошёл до копирования'
        staging = _staging_dirs(registry)
        assert len(staging) == 1
        reload_done = threading.Event()
        reloader = threading.Thread(
            target=lambda: (registry.startup(), reload_done.set())
        )
        reloader.start()
        assert reload_done.wait(10), 'reload ждал копирования staging'
        reloader.join(10)
        assert staging[0].is_dir(), 'уборка L7 удалила живой staging'
    finally:
        resume.set()
        stager.join(10)
    assert 'error' not in result, result
    staged = result['staged']
    assert staged.root == staging[0] and staged.root.is_dir()
    registry.publish_generation(staged)
    assert registry.active_generation_pointer(manifest.identity) is not None


def test_r4_после_сбоя_staging_остаток_убирается_следующим_стартом(tmp_path, monkeypatch):
    from mcp1c import intake_v2_registry

    manifest, payloads = _manifest(tmp_path, 'r4-failed')
    registry = Registry(tmp_path / 'data')
    registry.startup()
    paused, resume = _pause_staging_copy(registry, fail=True)
    resume.set()
    # Остаток должен пережить сбой, чтобы проверить снятие защиты.
    monkeypatch.setattr(intake_v2_registry.shutil, 'rmtree', lambda *_a, **_k: None)

    with pytest.raises(Exception):
        registry.stage_generation(manifest, payloads)
    leftovers = _staging_dirs(registry)
    assert len(leftovers) == 1
    monkeypatch.undo()

    registry.startup()

    assert not leftovers[0].exists()
