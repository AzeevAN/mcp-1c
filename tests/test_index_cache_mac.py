"""Кэш индексов подписан HMAC-SHA256, и `marshal.loads` идёт только после сверки.

MAC защищает от порчи и подмены файла кэша тем, кто не может прочитать ключ;
атакующий с правом чтения и записи всего `data/` подделает и подпись, это
описано в документации. Главное здесь — порядок: чужой блоб с валидным
штампом не доходит до `marshal`, который на испорченных данных способен
уронить интерпретатор.
"""

from __future__ import annotations

import gzip
import marshal
import os
import stat

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
