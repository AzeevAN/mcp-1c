"""Чтение Registry не ждёт durable IO публикации и гостевого flock."""

from __future__ import annotations

import json
import os
import select
import subprocess
import sys
import threading
import time
from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path

import pytest

from conftest import write_syntax
from mcp1c.intake_v2 import RecoveryPhase
from mcp1c.registry import Registry, RegistryRecoveryBlocked
from test_intake_v2_generation import _m7_published, _stage_b_generation


def _worker(action, results, errors, finished):
    def run():
        try:
            results.append(action())
        except BaseException as error:
            errors.append(error)
        finally:
            finished.set()

    # Даже дефект порядка замков не должен удерживать процесс pytest после FAIL.
    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    return thread


def _snapshot_reader(registry, results, errors, finished):
    def read():
        started = time.monotonic()
        snapshot = registry.snapshot()
        return snapshot, time.monotonic() - started

    return _worker(read, results, errors, finished)


def _join_threads(threads):
    for thread in threads:
        thread.join(timeout=10)
    assert not any(thread.is_alive() for thread in threads), "Потоки проверки не завершились"


@pytest.mark.parametrize("target", ["registry-json", "final-wal"])
def test_m9_snapshot_читает_старое_поколение_во_время_commit_fsync(
    tmp_path, monkeypatch, target
):
    registry, materialized, old_pointer, _root = _m7_published(tmp_path)
    old_snapshot = registry.snapshot()
    old_disk = registry.registry_path.read_bytes()
    identity = materialized.manifest.identity
    key = identity.grouping_key
    name = identity.configuration_name
    manifest = replace(materialized.manifest, generation_id="m9-next")
    staged = _stage_b_generation(registry, manifest, materialized.payloads)
    scope = threading.local()
    entered = threading.Event()
    release = threading.Event()
    writer_done = threading.Event()
    reader_done = threading.Event()
    writes, reads, errors, threads = [], [], [], []
    counts = {"target_fsync": 0}
    original_fsync = os.fsync

    def paused_fsync(descriptor):
        if getattr(scope, "target", False):
            counts["target_fsync"] += 1
            if counts["target_fsync"] == 1:
                entered.set()
                if not release.wait(timeout=10):
                    raise TimeoutError("Не освобождён fsync проверки M9")
        return original_fsync(descriptor)

    if target == "registry-json":
        original_write = registry._write_json_durably

        def write_registry(path, payload, **kwargs):
            previous = getattr(scope, "target", False)
            scope.target = path == registry.registry_path
            try:
                return original_write(path, payload, **kwargs)
            finally:
                scope.target = previous

        monkeypatch.setattr(registry, "_write_json_durably", write_registry)
    else:
        store = registry._generation_store
        original_recovery = store.write_recovery

        def write_final_recovery(recovery):
            previous = getattr(scope, "target", False)
            # Первый PREPARED WAL и promote fsync уже вне read lock. Нужен
            # финальный cleanup-plan с принятым job, а не порядковый вызов WAL.
            scope.target = (
                recovery.phase is RecoveryPhase.PREPARED
                and recovery.staged.last_successful_load_job_id == "m9-accepted"
            )
            try:
                return original_recovery(recovery)
            finally:
                scope.target = previous

        monkeypatch.setattr(store, "write_recovery", write_final_recovery)

    monkeypatch.setattr(os, "fsync", paused_fsync)
    try:
        threads.append(_worker(
            lambda: registry.publish_generation(
                staged, expected_previous=old_pointer, accepted_job_id="m9-accepted"
            ),
            writes, errors, writer_done,
        ))
        assert entered.wait(timeout=5), f"Не достигнут целевой fsync: {target}; {errors!r}"
        assert not writer_done.is_set()
        # До rename реестра на диске обязан оставаться прежний pointer.
        assert registry.registry_path.read_bytes() == old_disk
        threads.append(_snapshot_reader(registry, reads, errors, reader_done))
        assert reader_done.wait(timeout=0.1), f"snapshot ждёт {target} fsync"
        assert not errors, repr(errors)
        snapshot, elapsed = reads[0]
        assert elapsed < 0.1
        assert not writer_done.is_set()
        assert snapshot.generations[key].pointer == old_pointer
        assert snapshot.generations[key].manifest == materialized.manifest
        assert snapshot.configurations[name] is old_snapshot.configurations[name]
        assert snapshot.configuration_names == old_snapshot.configuration_names
    finally:
        release.set()
        _join_threads(threads)

    assert not errors, repr(errors)
    assert writer_done.is_set()
    assert counts["target_fsync"] >= 1
    pointer = writes[0]
    assert pointer.generation_id == "m9-next"
    assert pointer.last_successful_load_job_id == "m9-accepted"
    after = registry.snapshot()
    assert after.generations[key].pointer == pointer
    assert after.generations[key].manifest == manifest
    assert after.configurations[name].config.name == name
    assert after.configurations[name].config == old_snapshot.configurations[name].config
    assert registry.active_generation_pointer(identity) == pointer
    persisted = json.loads(registry.registry_path.read_text(encoding="utf-8"))
    assert pointer.to_dict() in persisted["generation_manifests"]


@contextmanager
def _owner_process(data_dir):
    """Только lease синтетического data/, с ограничением startup и shutdown."""
    program = (
        "import sys\n"
        "from mcp1c.registry import Registry\n"
        "registry = Registry(sys.argv[1])\n"
        "registry.startup()\n"
        "assert registry.role == 'owner'\n"
        "print('ready', flush=True)\n"
        "sys.stdin.readline()\n"
    )
    root = Path(__file__).parents[1]
    process = subprocess.Popen(
        [sys.executable, "-c", program, str(data_dir)],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env={**os.environ, "PYTHONPATH": str(root / "src")},
    )
    try:
        readable, _write, _error = select.select([process.stdout], [], [], 10)
        assert readable, "Owner не подтвердил запуск за 10 секунд"
        assert process.stdout.readline().strip() == "ready"
        yield process
    finally:
        try:
            process.communicate(input="\n", timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()
            process.communicate(timeout=10)
        assert process.poll() is not None
        assert process.returncode == 0, "Owner проверки завершился с ошибкой"


def test_m9_guest_ожидание_flock_не_блокирует_snapshot_и_сохраняет_чужие_строки(
    tmp_path, monkeypatch
):
    pytest.importorskip("fcntl")
    registry, materialized, old_pointer, _root = _m7_published(tmp_path)
    syntax = write_syntax(tmp_path / "guest-syntax", "8.3.99.1")
    with _owner_process(registry.data_dir):
        guest = Registry(registry.data_dir)
        guest.startup()
        assert guest.role == "guest"
        assert guest.acquire_ownership(timeout=0) is False
        before = guest.snapshot()
        disk_before = json.loads(guest.registry_path.read_text(encoding="utf-8"))
        entered = threading.Event()
        release = threading.Event()
        writer_done = threading.Event()
        reader_done = threading.Event()
        writes, reads, errors, threads = [], [], [], []
        original_file_lock = guest._registry_file_lock
        waits = []

        @contextmanager
        def waiting_file_lock():
            # Та же граница, на которой настоящий guest ждёт flock другого
            # процесса; owner lease настоящий, задержка управляется событиями.
            waits.append(True)
            entered.set()
            if not release.wait(timeout=10):
                raise TimeoutError("Не освобождён guest flock проверки M9")
            with original_file_lock():
                yield

        monkeypatch.setattr(guest, "_registry_file_lock", waiting_file_lock)

        def guest_write():
            source = guest.add_syntax(syntax)
            guest.save()
            return source

        try:
            threads.append(_worker(guest_write, writes, errors, writer_done))
            assert entered.wait(timeout=5), f"Guest не достиг flock: {errors!r}"
            assert not writer_done.is_set()
            threads.append(_snapshot_reader(guest, reads, errors, reader_done))
            assert reader_done.wait(timeout=0.1), "snapshot ждёт guest flock"
            assert not errors, repr(errors)
            snapshot, elapsed = reads[0]
            assert elapsed < 0.1
            assert not writer_done.is_set()
            assert snapshot.generations == before.generations
            assert snapshot.configuration_names == before.configuration_names
            assert set(before.sources) <= set(snapshot.sources)
        finally:
            release.set()
            _join_threads(threads)

        assert waits
        assert not errors, repr(errors)
        assert writes[0].id == "syntax-8.3.99.1"
        assert "syntax-8.3.99.1" in guest.snapshot().syntax_versions
        assert guest.active_generation_pointer(materialized.manifest.identity) == old_pointer
        persisted = json.loads(guest.registry_path.read_text(encoding="utf-8"))
        assert persisted["generation_manifests"] == disk_before["generation_manifests"]
        previous_rows = {row["id"] for row in disk_before["sources"]}
        current_rows = {row["id"] for row in persisted["sources"]}
        assert previous_rows <= current_rows
        assert "syntax-8.3.99.1" in current_rows


@pytest.mark.parametrize("failure", ["before-write", "after-visible-replace"])
def test_m9_ошибка_registry_write_сохраняет_исходный_отказ_и_границу_retry(
    tmp_path, monkeypatch, failure
):
    registry, materialized, pointer, _root = _m7_published(tmp_path)
    identity = materialized.manifest.identity
    name = identity.configuration_name
    assert registry.platform_declaration(name) is None
    before = registry.registry_path.read_bytes()
    original_write = registry._write_json_durably
    first_error = OSError("синтетический отказ fsync каталога")

    def failed_write(target, payload, **kwargs):
        if target != registry.registry_path:
            return original_write(target, payload, **kwargs)
        if failure == "after-visible-replace":
            original_write(target, payload, **kwargs)
        raise first_error

    monkeypatch.setattr(registry, "_write_json_durably", failed_write)
    with pytest.raises(OSError) as raised:
        registry.set_platform_version(name, "8.3.27")
    # Блокировка дальнейших записей не должна скрыть исходный IO-отказ.
    assert type(raised.value) is OSError
    assert raised.value is first_error
    assert registry.platform_declaration(name) is None
    assert registry.active_generation_pointer(identity) == pointer
    assert registry.active_generation(identity) == materialized.manifest
    failed_disk = registry.registry_path.read_bytes()
    monkeypatch.setattr(registry, "_write_json_durably", original_write)

    if failure == "before-write":
        assert failed_disk == before
        # Заведомо не начавшаяся запись не требует восстановления реестра.
        registry.save()
        assert registry.platform_declaration(name) is None
        assert name not in json.loads(
            registry.registry_path.read_text(encoding="utf-8")
        ).get("platform_declarations", {})
    else:
        assert failed_disk != before
        with pytest.raises(RegistryRecoveryBlocked):
            registry.save()
        assert registry.registry_path.read_bytes() == failed_disk
        assert registry.platform_declaration(name) is None
        # До restore память прежняя, но видимая на диске новая декларация
        # не может быть затёрта повторной записью старого снимка.
        assert registry.restore() == []
        declaration = registry.platform_declaration(name)
        assert declaration.version == "8.3.27"
        registry.save()
        persisted = json.loads(registry.registry_path.read_text(encoding="utf-8"))
        assert persisted["platform_declarations"][name] == declaration.to_dict()

    assert registry.active_generation_pointer(identity) == pointer
    assert registry.active_generation(identity) == materialized.manifest


def test_m9_restore_с_изолированной_битой_строкой_снимает_блокировку_commit(
    tmp_path, monkeypatch
):
    registry, materialized, pointer, _root = _m7_published(tmp_path)
    identity = materialized.manifest.identity
    name = identity.configuration_name
    isolated_row = {"id": "isolated-broken", "kind": "unknown-kind"}
    payload = json.loads(registry.registry_path.read_text(encoding="utf-8"))
    payload["sources"].append(isolated_row)
    # Явная собственная транзакция: это не внешняя подмена registry stamp.
    with registry._registry_commit(payload):
        pass
    problems = registry.restore()
    assert problems and all("isolated-broken" in problem for problem in problems)
    assert registry.active_generation_pointer(identity) == pointer
    assert registry.active_generation(identity) == materialized.manifest
    assert registry.platform_declaration(name) is None
    # Изолированный дефект строки не делает валидный корень read-only.
    registry.save()
    original_write = registry._write_json_durably
    first_error = OSError("синтетический отказ fsync каталога")

    def failed_after_replace(target, payload, **kwargs):
        result = original_write(target, payload, **kwargs)
        if target == registry.registry_path:
            raise first_error
        return result

    monkeypatch.setattr(registry, "_write_json_durably", failed_after_replace)
    with pytest.raises(OSError) as raised:
        registry.set_platform_version(name, "8.3.27")
    assert type(raised.value) is OSError
    assert raised.value is first_error
    assert registry.platform_declaration(name) is None
    assert registry.active_generation_pointer(identity) == pointer
    assert registry.active_generation(identity) == materialized.manifest
    monkeypatch.setattr(registry, "_write_json_durably", original_write)

    problems = registry.restore()
    assert problems and all("isolated-broken" in problem for problem in problems)
    declaration = registry.platform_declaration(name)
    assert declaration.version == "8.3.27"
    assert registry.active_generation_pointer(identity) == pointer
    assert registry.active_generation(identity) == materialized.manifest
    # Повторно проверенный валидный корень снимает неопределённость commit.
    # Предупреждение о carried-строке не должно запрещать эту запись.
    registry.save()
    persisted = json.loads(registry.registry_path.read_text(encoding="utf-8"))
    assert [row for row in persisted["sources"] if row.get("id") == "isolated-broken"] == [isolated_row]
    assert persisted["platform_declarations"][name] == declaration.to_dict()


def test_m9_snapshot_доступен_во_время_очистки_покрытия_remove(
    tmp_path, monkeypatch
):
    from mcp1c import coverage_log, source_ids

    registry, materialized, _pointer, native_root = _m7_published(tmp_path)
    identity = materialized.manifest.identity
    key = identity.grouping_key
    name = identity.configuration_name
    modules_id = source_ids.modules_id(name)
    scope = threading.local()
    entered = threading.Event()
    release = threading.Event()
    writer_done = threading.Event()
    reader_done = threading.Event()
    writes, reads, errors, threads = [], [], [], []
    cleanup_calls = []
    original_remove = coverage_log.remove

    def paused_coverage_remove(data_dir, source_id):
        if getattr(scope, "removing", False) and source_id == modules_id:
            cleanup_calls.append(source_id)
            if len(cleanup_calls) == 1:
                entered.set()
                if not release.wait(timeout=10):
                    raise TimeoutError("Не освобождена очистка покрытия проверки M9")
        return original_remove(data_dir, source_id)

    def remove_configuration():
        scope.removing = True
        try:
            return registry.remove(name)
        finally:
            scope.removing = False

    monkeypatch.setattr(coverage_log, "remove", paused_coverage_remove)
    try:
        threads.append(_worker(remove_configuration, writes, errors, writer_done))
        assert entered.wait(timeout=5), f"Remove не достиг очистки покрытия: {errors!r}"
        assert not writer_done.is_set()
        # Pointer уже durable снят: пауза относится к очистке, не к commit IO.
        persisted = json.loads(registry.registry_path.read_text(encoding="utf-8"))
        assert not persisted.get("generation_manifests", [])
        threads.append(_snapshot_reader(registry, reads, errors, reader_done))
        assert reader_done.wait(timeout=0.1), "snapshot ждёт очистку покрытия remove"
        assert not errors, repr(errors)
        snapshot, elapsed = reads[0]
        assert elapsed < 0.1
        assert not writer_done.is_set()
        assert key not in snapshot.generations
        assert name not in snapshot.configurations
        assert modules_id not in snapshot.modules
        assert registry.active_generation_pointer(identity) is None
    finally:
        release.set()
        _join_threads(threads)

    assert cleanup_calls
    assert not errors, repr(errors)
    assert writer_done.is_set()
    assert writes == [None]
    assert not native_root.exists()
    after = registry.snapshot()
    assert key not in after.generations
    assert name not in after.configurations
    assert modules_id not in after.modules
    assert name not in after.sources
    assert modules_id not in after.sources
    assert registry.active_generation_pointer(identity) is None
    persisted = json.loads(registry.registry_path.read_text(encoding="utf-8"))
    assert not persisted.get("generation_manifests", [])
    assert {row["id"] for row in persisted["sources"]} == set(after.sources)


def test_m12_параллельные_публикации_разных_конфигураций_не_теряют_строк(tmp_path):
    # Публикации сериализуются mutation lock, запись registry.json идёт вне
    # `_lock` с CAS по ревизии: ни одна из двух одновременных публикаций не
    # должна потерять pointer другой ни в памяти, ни на диске.
    from test_registry_restore_isolation import (
        MemoryTree, _catalog, _common_attribute, _session_parameter,
        collect_source_b, configuration_xml, convert_collection,
        materialize_generation, probe_export,
    )
    from test_intake_v2_extensions import _stage_active_base

    def staged(registry, name, generation_id):
        xml = configuration_xml().replace(
            b"<Name>DemoConfiguration</Name>", f"<Name>{name}</Name>".encode("utf-8")
        )
        tree = MemoryTree({
            "Configuration.xml": xml,
            "Catalogs/Items.xml": _catalog(),
            "CommonAttributes/Tenant.xml": _common_attribute(),
            "SessionParameters/Tenant.xml": _session_parameter(),
        })
        collection = collect_source_b(tree, probe_export(tree), tmp_path / f"c-{generation_id}")
        generation = materialize_generation(
            collection, convert_collection(collection), tmp_path / f"m-{generation_id}",
            generation_id=generation_id,
        )
        return _stage_active_base(registry, generation)

    for round_number in range(3):
        registry = Registry(tmp_path / f"data-{round_number}")
        names = (f"Первая{round_number}", f"Вторая{round_number}")
        bundles = [staged(registry, name, f"g{round_number}-{index}") for index, name in enumerate(names)]
        start = threading.Barrier(2)
        results, errors = [], []

        def publish(bundle):
            try:
                start.wait(5)
                results.append(registry.publish_generation(bundle))
            except BaseException as error:  # pragma: no cover - диагностика
                errors.append(error)

        threads = [threading.Thread(target=publish, args=(bundle,)) for bundle in bundles]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(30)
        assert not errors, errors
        assert set(registry.snapshot().configurations) == set(names)
        rows = json.loads(registry.registry_path.read_text(encoding="utf-8"))["generation_manifests"]
        assert {row["identity"]["configuration_name"] for row in rows} == set(names)
        restarted = Registry(registry.data_dir)
        restarted.startup()
        assert set(restarted.snapshot().configurations) == set(names)
