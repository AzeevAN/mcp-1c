"""Единственный сканер входящих файлов при одновременных запросах."""

from threading import Barrier, BrokenBarrierError, Event, Lock, Thread

from mcp1c import dashboard_backend, incoming
from mcp1c.registry import Registry


def test_восемь_одновременных_callers_получают_один_incoming_scanner(tmp_path, monkeypatch):
    registry = Registry(tmp_path / 'data')
    real_constructor = incoming.IncomingScanner
    # Изолируем глобальный кэш от других тестов, не затрагивая их экземпляры.
    monkeypatch.setattr(dashboard_backend, '_СКАНЕРЫ', {})
    callers = Barrier(9, timeout=5)
    first_constructor_entered = Event()
    all_attempted = Event()
    release_first_constructor = Event()
    counters_lock = Lock()
    attempted_calls = 0
    constructor_calls = 0
    results = []
    errors = []

    def held_constructor(owner_registry):
        nonlocal constructor_calls
        with counters_lock:
            constructor_calls += 1
            first = constructor_calls == 1
        if first:
            first_constructor_entered.set()
            # Только первый constructor ждёт Main. Нет barrier на 8 внутри
            # constructor: после фикса остальные callers ждут общий cache lock.
            if not release_first_constructor.wait(5):
                raise TimeoutError('Main не освободил первый constructor')
        return real_constructor(owner_registry)

    monkeypatch.setattr(incoming, 'IncomingScanner', held_constructor)

    def call_scanner():
        nonlocal attempted_calls
        try:
            callers.wait()
            with counters_lock:
                attempted_calls += 1
                if attempted_calls == 8:
                    all_attempted.set()
            results.append(dashboard_backend._scanner(registry))
        except Exception as error:
            errors.append(error)

    threads = [Thread(target=call_scanner, name=f'l9-caller-{number}', daemon=True)
               for number in range(8)]
    for thread in threads:
        thread.start()
    try:
        callers.wait()
        entered = first_constructor_entered.wait(3)
        attempted = all_attempted.wait(3)
    except BrokenBarrierError:
        entered = attempted = False
    finally:
        release_first_constructor.set()
        callers.abort()
        for thread in threads:
            thread.join(3)

    assert all(not thread.is_alive() for thread in threads)
    assert entered and attempted
    assert errors == []
    assert len(results) == 8
    assert constructor_calls == 1
    scanner = results[0]
    assert isinstance(scanner, real_constructor)
    assert all(result is scanner for result in results)
    # Повторный вызов с другим Registry того же data_dir использует тот же
    # экземпляр, а не только случайно совпавшие результаты одной волны.
    assert dashboard_backend._scanner(Registry(registry.data_dir)) is scanner
