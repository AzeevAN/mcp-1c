"""Кэш индексов подписан HMAC-SHA256, и `marshal.loads` идёт только после сверки.

MAC защищает от порчи и подмены файла кэша тем, кто не может прочитать ключ;
атакующий с правом чтения и записи всего `data/` подделает и подпись, это
описано в документации. Главное здесь — порядок: чужой блоб с валидным
штампом не доходит до `marshal`, который на испорченных данных способен
уронить интерпретатор.
"""

from __future__ import annotations

import errno
import gzip
import marshal
import os
import stat
from pathlib import Path

import pytest

from mcp1c import index_cache


def _forbid_marshal(monkeypatch):
    def boom(_data):
        raise AssertionError("marshal.loads вызван до проверки MAC")

    monkeypatch.setattr(index_cache.marshal, "loads", boom)


def _split(raw: bytes) -> tuple[bytes, bytes, bytes]:
    header, mac, blob = raw.split(b"\n", 2)
    return header, mac, blob


def test_подменённый_блоб_с_верным_штампом_не_доходит_до_marshal(
    tmp_path, monkeypatch
):
    path = tmp_path / "blob"
    index_cache.save_blob({"a": 1}, path, source_sha256="abc", kind="blob")
    header, mac, _blob = _split(path.read_bytes())
    forged = gzip.compress(marshal.dumps({"a": 2}))
    path.write_bytes(header + b"\n" + mac + b"\n" + forged)
    _forbid_marshal(monkeypatch)

    assert index_cache.load_blob(path, source_sha256="abc", kind="blob") is None


def test_файл_без_подписи_прежнего_формата_это_промах(tmp_path, monkeypatch):
    path = tmp_path / "blob"
    index_cache.save_blob({"a": 1}, path, source_sha256="abc", kind="blob")
    header, _mac, blob = _split(path.read_bytes())
    path.write_bytes(header + b"\n" + blob)
    _forbid_marshal(monkeypatch)

    assert index_cache.load_blob(path, source_sha256="abc", kind="blob") is None


def test_кэш_чужой_установки_с_другим_ключом_не_поднимается(tmp_path):
    first = tmp_path / "first"
    second = tmp_path / "second"
    index_cache.save_blob({"a": 1}, first / "blob", source_sha256="abc", kind="blob")
    index_cache.save_blob({"a": 1}, second / "blob", source_sha256="abc", kind="blob")
    (second / "blob").write_bytes((first / "blob").read_bytes())

    assert index_cache.load_blob(second / "blob", source_sha256="abc", kind="blob") is None
    assert index_cache.load_blob(first / "blob", source_sha256="abc", kind="blob") == {
        "a": 1
    }


def test_ключ_переживает_рестарт_процесса(tmp_path):
    path = tmp_path / "blob"
    index_cache.save_blob({"a": 1}, path, source_sha256="abc", kind="blob")
    # Новый процесс: ключ в памяти не закэширован, читается с диска.
    index_cache._KEYS.clear()

    assert index_cache.load_blob(path, source_sha256="abc", kind="blob") == {"a": 1}


def test_ключ_лежит_в_каталоге_кэша_с_правами_0600_и_не_убирается(tmp_path):
    path = index_cache.path_for(tmp_path, "Розница", "objects")
    index_cache.save_blob({"a": 1}, path, source_sha256="abc", kind="objects")
    key = tmp_path / index_cache.KEY_FILE_NAME

    info = key.lstat()
    assert stat.S_ISREG(info.st_mode)
    assert stat.S_IMODE(info.st_mode) == 0o600
    assert info.st_size == 32
    assert index_cache.sweep(tmp_path, keep=[path.name]) == []
    assert key.exists()


def test_временный_каталог_сборки_делит_ключ_с_каталогом_кэша(tmp_path):
    """Registry собирает кэш модулей в `.modules-cache.tmp-*` и переносит файлы."""
    staging = tmp_path / ".modules-cache.tmp-abc"
    staging.mkdir()
    built = staging / "Розница.modules-toc"
    index_cache.save_blob({"a": 1}, built, source_sha256="abc", kind="toc")
    target = tmp_path / built.name
    built.replace(target)

    assert not (staging / index_cache.KEY_FILE_NAME).exists()
    assert index_cache.load_blob(target, source_sha256="abc", kind="toc") == {"a": 1}


def test_испорченный_ключ_заменяется_и_кэш_перестраивается(tmp_path):
    path = tmp_path / "blob"
    index_cache.save_blob({"a": 1}, path, source_sha256="abc", kind="blob")
    (tmp_path / index_cache.KEY_FILE_NAME).write_bytes(b"short")
    index_cache._KEYS.clear()

    assert index_cache.load_blob(path, source_sha256="abc", kind="blob") is None
    index_cache.save_blob({"a": 3}, path, source_sha256="abc", kind="blob")
    index_cache._KEYS.clear()
    assert index_cache.load_blob(path, source_sha256="abc", kind="blob") == {"a": 3}


def test_символическая_ссылка_вместо_ключа_не_принимается(tmp_path):
    outside = tmp_path / "outside-key"
    outside.write_bytes(b"k" * 32)
    cache = tmp_path / "cache"
    cache.mkdir()
    (cache / index_cache.KEY_FILE_NAME).symlink_to(outside)

    assert (
        index_cache.save_blob({"a": 1}, cache / "blob", source_sha256="abc", kind="blob")
        is None
    )
    assert index_cache.load_blob(cache / "blob", source_sha256="abc", kind="blob") is None


@pytest.mark.skipif(os.geteuid() == 0, reason="под root права на каталог не мешают")
def test_каталог_только_для_чтения_даёт_промах_без_ошибки(tmp_path):
    cache = tmp_path / "cache"
    cache.mkdir()
    cache.chmod(0o500)
    try:
        assert (
            index_cache.save_blob(
                {"a": 1}, cache / "blob", source_sha256="abc", kind="blob"
            )
            is None
        )
        assert index_cache.load_blob(cache / "blob", source_sha256="abc", kind="blob") is None
    finally:
        cache.chmod(0o700)


def _saved_with_key(tmp_path):
    path = tmp_path / "blob"
    index_cache.save_blob({"a": 1}, path, source_sha256="abc", kind="blob")
    index_cache._KEYS.clear()
    return path, tmp_path / index_cache.KEY_FILE_NAME


@pytest.mark.parametrize("mode", [0o640, 0o604, 0o660, 0o644])
def test_ключ_доступный_группе_или_всем_не_используется(tmp_path, mode):
    path, key = _saved_with_key(tmp_path)
    key.chmod(mode)

    assert index_cache.load_blob(path, source_sha256="abc", kind="blob") is None
    assert (
        index_cache.save_blob({"a": 2}, path, source_sha256="abc", kind="blob") is None
    )
    # Ключ не подменён молча: решать, чей он, оператору.
    assert stat.S_IMODE(key.lstat().st_mode) == mode


def test_ключ_чужого_владельца_не_используется(tmp_path, monkeypatch):
    path, _key = _saved_with_key(tmp_path)
    real = os.geteuid()
    monkeypatch.setattr(index_cache.os, "geteuid", lambda: real + 1)

    assert index_cache.load_blob(path, source_sha256="abc", kind="blob") is None


def test_ключ_поднимается_только_из_временного_каталога_модулей(tmp_path):
    hidden = tmp_path / ".hidden"
    hidden.mkdir()
    index_cache.save_blob({"a": 1}, hidden / "blob", source_sha256="abc", kind="blob")
    assert (hidden / index_cache.KEY_FILE_NAME).exists()
    assert not (tmp_path / index_cache.KEY_FILE_NAME).exists()

    nested = tmp_path / ".modules-cache.tmp-x" / ".deeper"
    nested.mkdir(parents=True)
    index_cache.save_blob({"a": 1}, nested / "blob", source_sha256="abc", kind="blob")
    # Подъём ровно на один уровень и только для `.modules-cache.tmp-*`.
    assert (nested / index_cache.KEY_FILE_NAME).exists()
    assert not (tmp_path / ".modules-cache.tmp-x" / index_cache.KEY_FILE_NAME).exists()


@pytest.mark.parametrize("code", [errno.EPERM, errno.ENOTSUP, errno.EXDEV])
def test_без_жёстких_ссылок_ключ_создаётся_переименованием(tmp_path, monkeypatch, code):
    def no_link(_source, _target):
        raise OSError(code, os.strerror(code))

    monkeypatch.setattr(index_cache.os, "link", no_link)
    path = tmp_path / "blob"

    assert index_cache.save_blob({"a": 1}, path, source_sha256="abc", kind="blob") == path
    index_cache._KEYS.clear()
    assert index_cache.load_blob(path, source_sha256="abc", kind="blob") == {"a": 1}
    assert [p.name for p in tmp_path.iterdir() if p.name.endswith(".tmp")] == []


def test_без_жёстких_ссылок_гонка_создания_сходится_на_одном_ключе(
    tmp_path, monkeypatch
):
    def no_link(_source, _target):
        raise OSError(errno.EXDEV, os.strerror(errno.EXDEV))

    monkeypatch.setattr(index_cache.os, "link", no_link)
    подлинный_open = index_cache.os.open
    key_path = tmp_path / index_cache.KEY_FILE_NAME
    чужой_ключ = b"k" * index_cache._KEY_BYTES

    def open_с_соперником(path, flags, *args, **kwargs):
        # Соперник успевает создать ключ сразу перед нашим созданием.
        if Path(path) == key_path and flags & os.O_EXCL and not key_path.exists():
            descriptor = подлинный_open(key_path, os.O_WRONLY | os.O_CREAT, 0o600)
            os.write(descriptor, чужой_ключ)
            os.close(descriptor)
        return подлинный_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(index_cache.os, "open", open_с_соперником)
    path = tmp_path / "blob"
    assert index_cache.save_blob({"a": 1}, path, source_sha256="abc", kind="blob") == path

    # Наш кэш подписан ключом, который действительно лежит на диске.
    assert key_path.read_bytes() == чужой_ключ
    index_cache._KEYS.clear()
    assert index_cache.load_blob(path, source_sha256="abc", kind="blob") == {"a": 1}


def test_чужой_ключ_предупреждает_один_раз(tmp_path, monkeypatch, caplog):
    path, _key = _saved_with_key(tmp_path)
    index_cache._WARNED_FOREIGN_KEYS.clear()
    real = os.geteuid()
    monkeypatch.setattr(index_cache.os, "geteuid", lambda: real + 1)

    with caplog.at_level("WARNING", logger="mcp1c.index_cache"):
        for _ in range(3):
            index_cache._KEYS.clear()
            assert index_cache.load_blob(path, source_sha256="abc", kind="blob") is None
    assert sum("другому пользователю" in record.message for record in caplog.records) == 1


def test_без_geteuid_владелец_ключа_не_сверяется(tmp_path, monkeypatch):
    path, _key = _saved_with_key(tmp_path)
    monkeypatch.delattr(index_cache.os, "geteuid")
    assert index_cache.load_blob(path, source_sha256="abc", kind="blob") == {"a": 1}


def _пустой_ключ(tmp_path, *, возраст: float = 0.0) -> Path:
    """Ключ, созданный O_EXCL на месте, но ещё не записанный соперником."""
    key_path = tmp_path / index_cache.KEY_FILE_NAME
    descriptor = os.open(key_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    os.close(descriptor)
    if возраст:
        прошлое = key_path.stat().st_mtime - возраст
        os.utime(key_path, (прошлое, прошлое))
    index_cache._KEYS.clear()
    return key_path


def test_ключ_в_записи_соперником_дожидаются_а_не_заменяют(tmp_path):
    import threading

    key_path = _пустой_ключ(tmp_path)
    ключ_соперника = b"r" * index_cache._KEY_BYTES

    def соперник_дописывает():
        with open(key_path, "r+b") as stream:
            stream.write(ключ_соперника)

    таймер = threading.Timer(0.05, соперник_дописывает)
    таймер.start()
    try:
        path = tmp_path / "blob"
        assert (
            index_cache.save_blob({"a": 1}, path, source_sha256="abc", kind="blob")
            == path
        )
    finally:
        таймер.join()

    # Ключ победителя не заменён, и наш кэш подписан именно им.
    assert key_path.read_bytes() == ключ_соперника
    index_cache._KEYS.clear()
    assert index_cache.load_blob(path, source_sha256="abc", kind="blob") == {"a": 1}


def test_недописанный_свежий_ключ_не_заменяется_кэш_пропускается(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(index_cache, "_KEY_WAIT_SECONDS", 0.05)
    key_path = _пустой_ключ(tmp_path)
    inode = key_path.stat().st_ino

    path = tmp_path / "blob"
    assert index_cache.save_blob({"a": 1}, path, source_sha256="abc", kind="blob") is None
    assert key_path.stat().st_ino == inode
    assert key_path.read_bytes() == b""


def test_давно_недописанный_ключ_считается_битым_и_заменяется(tmp_path):
    key_path = _пустой_ключ(tmp_path, возраст=3600)

    path = tmp_path / "blob"
    assert index_cache.save_blob({"a": 1}, path, source_sha256="abc", kind="blob") == path
    assert len(key_path.read_bytes()) == index_cache._KEY_BYTES
