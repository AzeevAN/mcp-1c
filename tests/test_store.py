"""Синхронизация канонического индекса справки при сохранении."""

import errno
import os
import stat
from pathlib import Path

import pytest

from conftest import build_syntax
from mcp1c.store import load_syntax, save_syntax


def test_save_syntax_синхронизирует_файл_до_replace_и_каталог_после(tmp_path, monkeypatch):
    target = tmp_path / 'syntax.json.gz'
    index = build_syntax('8.3.99.2')
    index.warnings.append('Синтетическое предупреждение для round-trip')
    events = []
    synced_files = []
    synced_directories = []
    real_fsync = os.fsync
    real_replace = os.replace

    def fsync(descriptor):
        info = os.fstat(descriptor)
        identity = (info.st_dev, info.st_ino)
        if stat.S_ISREG(info.st_mode):
            events.append('file-fsync')
            synced_files.append(identity)
        elif stat.S_ISDIR(info.st_mode):
            events.append('dir-fsync')
            synced_directories.append(identity)
        else:
            pytest.fail('fsync должен получать файл индекса или его каталог')
        return real_fsync(descriptor)

    def replace(source, destination, **kwargs):
        assert Path(destination).name == target.name
        events.append('replace')
        return real_replace(source, destination, **kwargs)

    monkeypatch.setattr(os, 'fsync', fsync)
    monkeypatch.setattr(os, 'replace', replace)
    assert save_syntax(index, target) == target

    assert events.count('replace') == 1
    published = events.index('replace')
    assert 'file-fsync' in events[:published]
    assert 'dir-fsync' in events[published + 1:]
    # Проверяем принадлежность fd именно опубликованному индексу и его каталогу.
    file_info = target.stat()
    directory_info = target.parent.stat()
    assert (file_info.st_dev, file_info.st_ino) in synced_files
    assert (directory_info.st_dev, directory_info.st_ino) in synced_directories
    restored = load_syntax(target)
    assert restored.platforms == ['8.3.99.2']
    assert restored.warnings == index.warnings
    assert set(restored.items) == set(index.items)
    assert restored.items['method.СтрНайти'].description == index.items['method.СтрНайти'].description


def test_save_syntax_отказ_file_fsync_сохраняет_прежний_читаемый_индекс(tmp_path, monkeypatch):
    target = tmp_path / 'syntax.json.gz'
    original = build_syntax('8.3.99.1')
    save_syntax(original, target)
    before = target.read_bytes()
    replacement = build_syntax('8.3.99.2')
    replacement.items['method.СтрНайти'].description = 'Новое содержимое не должно опубликоваться'
    real_fsync = os.fsync
    real_replace = os.replace
    replacements = []

    def fail_file_fsync(descriptor):
        if stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise OSError(errno.EIO, 'Синтетический отказ синхронизации файла')
        return real_fsync(descriptor)

    def replace(source, destination, **kwargs):
        replacements.append(Path(destination).name)
        return real_replace(source, destination, **kwargs)

    monkeypatch.setattr(os, 'fsync', fail_file_fsync)
    monkeypatch.setattr(os, 'replace', replace)
    with pytest.raises(OSError, match='Синтетический отказ синхронизации файла'):
        save_syntax(replacement, target)

    assert replacements == []
    assert target.read_bytes() == before
    restored = load_syntax(target)
    assert restored.platforms == ['8.3.99.1']
    assert restored.items['method.СтрНайти'].description == original.items['method.СтрНайти'].description
