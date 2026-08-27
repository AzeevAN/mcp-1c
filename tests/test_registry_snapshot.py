"""Публичный снимок реестра и его конкурентные потребители."""

import threading
from dataclasses import FrozenInstanceError

import pytest

from mcp1c.incoming import IncomingScanner
from mcp1c.registry import KIND_MODULES, STATUS_LOADING, STATUS_READY, Registry, Source


def test_snapshot_не_меняется_вместе_с_живым_реестром(tmp_path):
    registry = Registry(tmp_path / "data")
    source = Source(
        id="ТестоваяКонфигурация:modules",
        kind=KIND_MODULES,
        status=STATUS_LOADING,
        warnings=["исходное предупреждение"],
    )
    registry.sources[source.id] = source

    snapshot = registry.snapshot()
    assert registry.snapshot_is_current(snapshot)
    assert snapshot.modules[source.id].source == snapshot.sources[source.id]
    source.status = STATUS_READY
    source.warnings.append("новое предупреждение")
    registry.sources["ДругаяКонфигурация:modules"] = Source(
        id="ДругаяКонфигурация:modules",
        kind=KIND_MODULES,
    )

    row = snapshot.sources[source.id]
    assert row.status == STATUS_LOADING
    assert row.warnings == ("исходное предупреждение",)
    assert tuple(snapshot.sources) == (source.id,)
    with pytest.raises(TypeError):
        snapshot.sources["ещё"] = row
    with pytest.raises(FrozenInstanceError):
        row.status = STATUS_READY
    assert not registry.snapshot_is_current(snapshot)


def test_scanner_не_обходит_живую_карту_одновременно_с_публикацией(tmp_path):
    registry = Registry(tmp_path / "data")
    registry.incoming_dir.mkdir(parents=True)
    чтение_началось = threading.Event()
    продолжить_чтение = threading.Event()
    мутация_началась = threading.Event()
    мутация_закончилась = threading.Event()

    class КартаСБарьером(dict):
        def values(self):
            iterator = iter(super().values())
            yield next(iterator)
            чтение_началось.set()
            assert продолжить_чтение.wait(timeout=2)
            yield from iterator

    registry.sources = КартаСБарьером(
        {
            "ТестоваяКонфигурация:modules": Source(
                id="ТестоваяКонфигурация:modules",
                kind=KIND_MODULES,
                sha256="старый",
            )
        }
    )
    scanner = IncomingScanner(registry)
    ошибки: list[BaseException] = []

    def scan():
        try:
            scanner.scan()
        except BaseException as error:
            ошибки.append(error)

    def publish():
        мутация_началась.set()
        with registry._lock:
            registry.sources["ДругаяКонфигурация:modules"] = Source(
                id="ДругаяКонфигурация:modules",
                kind=KIND_MODULES,
                sha256="новый",
            )
        мутация_закончилась.set()

    поток_чтения = threading.Thread(target=scan, daemon=True)
    поток_чтения.start()
    assert чтение_началось.wait(timeout=1), ошибки
    поток_записи = threading.Thread(target=publish, daemon=True)
    поток_записи.start()
    assert мутация_началась.wait(timeout=1)
    # На старом пути writer успевает изменить dict до следующего шага
    # итератора. Новый snapshot держит lock, поэтому writer завершится уже
    # после того, как структурный снимок будет собран.
    мутация_закончилась.wait(timeout=0.1)
    продолжить_чтение.set()
    поток_чтения.join(timeout=2)
    поток_записи.join(timeout=2)

    assert not поток_чтения.is_alive()
    assert not поток_записи.is_alive()
    assert not ошибки


def test_scanner_атомарно_занимает_единственный_слот(tmp_path):
    scanner = IncomingScanner(Registry(tmp_path / "data"))
    барьер = threading.Barrier(3)
    результаты: list[tuple[str, bool, tuple[str, ...]]] = []

    def start(name: str) -> None:
        барьер.wait(timeout=2)
        started, busy = scanner.try_start(name)
        результаты.append((name, started, busy))

    потоки = [
        threading.Thread(target=start, args=(name,), daemon=True)
        for name in ("первая.zip", "вторая.zip")
    ]
    for поток in потоки:
        поток.start()
    барьер.wait(timeout=2)
    for поток in потоки:
        поток.join(timeout=2)

    запущенные = [name for name, started, _ in результаты if started]
    отклонённые = [busy for _, started, busy in результаты if not started]
    assert len(запущенные) == 1
    assert отклонённые == [(запущенные[0],)]
    assert scanner.running == frozenset(запущенные)
