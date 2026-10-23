"""Снимки источников при конкурентной регистрации."""

from dataclasses import replace
from threading import Event, Thread, current_thread

import pytest

from conftest import write_syntax, состарить
from mcp1c import index_cache
from mcp1c.registry import Registry


def _l8_read_during_source_insert(registry, seed, operation, seam, monkeypatch):
    original = getattr(registry, seam)
    state = {}
    published = []
    observed = []

    def paused_processing(*args, **kwargs):
        if current_thread() is state.get('reader') and not state['entered'].is_set():
            # Копия sources уже должна быть снята, lock освобождён. На старом
            # коде эта точка находится внутри живого dict iterator.
            state['entered'].set()
            if not state['release'].wait(2):
                raise TimeoutError('Не снята пауза обработки элемента snapshot')
        return original(*args, **kwargs)

    monkeypatch.setattr(registry, seam, paused_processing)
    for number in range(200):
        entered, release, writer_done = Event(), Event(), Event()
        errors = []
        results = []
        version = f'8.3.99.{number + 10}'
        source = replace(seed, id=f'syntax-{version}', platform=version)

        def read():
            try:
                results.append(operation())
            except Exception as error:
                errors.append(error)

        def write():
            try:
                if not entered.wait(2):
                    raise TimeoutError('Reader не дошёл до обработки элемента')
                # Существующая production-граница публикации Source под _lock;
                # повторного parsing одного и того же индекса в 200 раундах нет.
                registry._register_syntax_source(source, None, None)
            except Exception as error:
                errors.append(error)
            finally:
                writer_done.set()

        reader = Thread(target=read, name=f'l8-reader-{number}', daemon=True)
        writer = Thread(target=write, name=f'l8-writer-{number}', daemon=True)
        state.update(reader=reader, entered=entered, release=release)
        reader.start()
        writer.start()
        try:
            reached_processing = entered.wait(2)
            # Writer завершается ДО снятия паузы reader. Ожидание не находится
            # в dict iterator и не требует, чтобы writer обходил захваченный lock.
            inserted_during_processing = writer_done.wait(2)
        finally:
            release.set()
            reader.join(2)
            writer.join(2)
        assert not reader.is_alive() and not writer.is_alive()
        assert reached_processing, f'Раунд {number}: reader не достиг seam'
        assert inserted_during_processing, f'Раунд {number}: обработка snapshot удерживает writer'
        assert errors == [], f'Раунд {number}: {errors!r}'
        assert len(results) == 1
        published.append(source.id)
        observed.append(results[0])
    state.clear()
    return published, observed


@pytest.mark.parametrize('operation', ['cached-names', 'sweep-syntax'])
def test_итерация_источников_переживает_200_конкурентных_публикаций(tmp_path, monkeypatch, operation):
    registry = Registry(tmp_path / 'data')
    seed = registry.add_syntax(write_syntax(registry._syntax_index_dir(), '8.3.99.1'))
    active_index = registry._absolute(seed.stored_path)
    before = active_index.read_bytes()
    состарить(active_index)
    if operation == 'cached-names':
        read, seam = registry._cached_names, '_cache_path'
    else:
        read, seam = registry.sweep_syntax, '_absolute'
        orphan = active_index.parent / 'obsolete-unregistered.json.gz'
        orphan.write_bytes(b'obsolete synthetic index')
        состарить(orphan)

    published, results = _l8_read_during_source_insert(
        registry, seed, read, seam, monkeypatch,
    )

    assert set(published) <= set(registry.snapshot().sources)
    assert active_index.read_bytes() == before
    if operation == 'cached-names':
        expected_seed = {
            index_cache.path_for(registry.cache_dir, seed.id, kind).name
            for kind in ('syntax', 'lookup')
        }
        assert all(expected_seed <= names for names in results)
        final_names = registry._cached_names()
        assert {
            index_cache.path_for(registry.cache_dir, source_id, kind).name
            for source_id in published for kind in ('syntax', 'lookup')
        } <= final_names
    else:
        assert all(active_index.name not in removed for removed in results)
        assert not orphan.exists()
