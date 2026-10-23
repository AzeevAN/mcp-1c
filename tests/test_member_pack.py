"""Контракты последовательного хранения immutable members."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

import mcp1c.member_pack as member_pack

from mcp1c.member_pack import (
    INDEX_NAME,
    PACK_NAME,
    MemberPackError,
    MemberPackWriter,
    open_stored_member,
)


def _write(root, members: dict[str, bytes]) -> None:
    root.mkdir()
    with MemberPackWriter(root) as pack:
        for path, payload in members.items():
            with pack.entry(path) as target:
                target.write(payload)


@pytest.mark.parametrize(
    "members",
    [
        (("payload/z.bin", b""), ("payload/a.bin", b"alpha")),
        (("payload/a.bin", b"alpha"), ("payload/z.bin", b"")),
        (
            ("payload/z-before.bin", b""),
            ("payload/a.bin", b"alpha"),
            ("payload/z-between-1.bin", b""),
            ("payload/z-between-2.bin", b""),
            ("payload/b.bin", b"beta"),
            ("payload/z-after.bin", b""),
        ),
        (("payload/zero-a.bin", b""), ("payload/zero-b.bin", b"")),
    ],
)
def test_pack_читает_нужные_диапазоны_и_пустые_members(tmp_path, members):
    root = tmp_path / "pack"
    _write(root, dict(members))

    for path, payload in members:
        with open_stored_member(root, path) as source:
            assert source.read(2) == payload[:2]
            assert source.read() == payload[2:]
            assert source.read() == b""


def test_pack_откатывает_незавершённую_entry(tmp_path):
    root = tmp_path / "pack"
    root.mkdir()
    with MemberPackWriter(root) as pack:
        with pytest.raises(ValueError, match="broken"):
            with pack.entry("payload/broken.bin") as target:
                target.write(b"partial")
                raise ValueError("broken")
        with pack.entry("payload/good.bin") as target:
            target.write(b"good")

    assert (root / PACK_NAME).read_bytes() == b"good"
    with open_stored_member(root, "payload/good.bin") as source:
        assert source.read() == b"good"
    with pytest.raises(MemberPackError, match="отсутствует"):
        open_stored_member(root, "payload/broken.bin")


@pytest.mark.parametrize("target_name", [PACK_NAME, INDEX_NAME])
def test_pack_не_следует_по_symlink(tmp_path, target_name):
    root = tmp_path / "pack"
    _write(root, {"payload/a.bin": b"alpha"})
    outside = tmp_path / "outside"
    outside.write_bytes((root / target_name).read_bytes())
    (root / target_name).unlink()
    (root / target_name).symlink_to(outside)

    with pytest.raises(MemberPackError, match="обычным файлом"):
        open_stored_member(root, "payload/a.bin")


def test_pack_отклоняет_неканоничный_или_несогласованный_индекс(tmp_path):
    root = tmp_path / "pack"
    _write(root, {"payload/a.bin": b"alpha"})
    index = json.loads((root / INDEX_NAME).read_text(encoding="utf-8"))
    index["entries"][0]["offset"] = 1
    (root / INDEX_NAME).write_text(json.dumps(index), encoding="utf-8")

    with pytest.raises(MemberPackError, match="разрыв|пересечение"):
        open_stored_member(root, "payload/a.bin")


def test_pack_отклоняет_пустой_member_внутри_занятого_диапазона(tmp_path):
    root = tmp_path / "pack"
    _write(root, {"payload/a.bin": b"alpha", "payload/z.bin": b""})
    index = json.loads((root / INDEX_NAME).read_text(encoding="utf-8"))
    empty = next(entry for entry in index["entries"] if entry["size"] == 0)
    empty["offset"] = 2
    (root / INDEX_NAME).write_text(
        json.dumps(
            index,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        + "\n",
        encoding="utf-8",
    )

    with pytest.raises(MemberPackError, match="разрыв|пересечение"):
        open_stored_member(root, "payload/z.bin")


@pytest.mark.parametrize("offset", [4, 6])
def test_pack_отклоняет_настоящие_пересечение_и_разрыв(tmp_path, offset):
    root = tmp_path / "pack"
    _write(root, {"payload/a.bin": b"alpha", "payload/b.bin": b"beta"})
    index = json.loads((root / INDEX_NAME).read_text(encoding="utf-8"))
    second = next(
        entry
        for entry in index["entries"]
        if entry["relative_path"] == "payload/b.bin"
    )
    second["offset"] = offset
    (root / INDEX_NAME).write_text(
        json.dumps(
            index,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        + "\n",
        encoding="utf-8",
    )

    with pytest.raises(MemberPackError, match="разрыв|пересечение"):
        open_stored_member(root, "payload/b.bin")


def test_reader_поддерживает_прежний_loose_member(tmp_path):
    root = tmp_path / "legacy"
    target = root / "payload" / "a.bin"
    target.parent.mkdir(parents=True)
    target.write_bytes(b"legacy")

    with open_stored_member(root, "payload/a.bin") as source:
        assert source.read() == b"legacy"


def test_pack_не_позволяет_loose_файлу_затенить_indexed_member(tmp_path):
    root = tmp_path / "pack"
    _write(root, {"payload/a.bin": b"packed"})
    shadow = root / "payload" / "a.bin"
    shadow.parent.mkdir()
    shadow.write_bytes(b"shadow")

    with open_stored_member(root, "payload/a.bin") as source:
        assert source.read() == b"packed"


def test_reader_отклоняет_неполную_пару_pack_и_index(tmp_path):
    root = tmp_path / "pack"
    _write(root, {"payload/a.bin": b"packed"})
    (root / INDEX_NAME).unlink()

    with pytest.raises(MemberPackError, match="парой"):
        open_stored_member(root, "payload/a.bin")


def _stored_reader(root):
    # Отсутствие нового API должно давать RED при выполнении, не при collection.
    return getattr(member_pack, "StoredMemberReader")(root)


def _track_pack_streams(monkeypatch):
    opened = []
    original = member_pack._open_regular

    def tracked(path, label):
        result = original(path, label)
        if path.name == PACK_NAME:
            opened.append(result[0])
        return result

    monkeypatch.setattr(member_pack, "_open_regular", tracked)
    return opened


def test_stored_reader_читает_много_members_через_один_pack_fd(
    tmp_path, monkeypatch
):
    root = tmp_path / "pack"
    members = {
        f"payload/{number:03}.bin": bytes([number]) * (number % 7)
        for number in range(80)
    }
    _write(root, members)
    opened = _track_pack_streams(monkeypatch)
    reads = []
    original_read = member_pack._PackedReader.read

    def tracked_read(view, size=-1):
        reads.append(size)
        return original_read(view, size)

    monkeypatch.setattr(member_pack._PackedReader, "read", tracked_read)
    with _stored_reader(root) as reader:
        for path, payload in reversed(tuple(members.items())):
            with reader.open(path) as source:
                assert source.read(2) == payload[:2]
                assert source.read() == payload[2:]
                assert source.read() == b""
            assert source.closed
        assert len(opened) == 1
        assert not opened[0].closed

    assert opened[0].closed
    assert len(reads) == len(members) * 3


def test_stored_reader_упорядочивает_физически_пустые_раньше_занятых(tmp_path):
    root = tmp_path / "pack"
    members = {
        "payload/z-empty.bin": b"",
        "payload/z-first.bin": b"first",
        "payload/y-empty.bin": b"",
        "payload/a-last.bin": b"last",
        "payload/x-empty.bin": b"",
    }
    _write(root, members)
    for name in ("b-loose.bin", "a-loose.bin"):
        (root / name).write_bytes(name.encode())
    requested = ["b-loose.bin", *reversed(members), "a-loose.bin"]

    with _stored_reader(root) as reader:
        assert reader.ordered_paths(iter(requested)) == (
            *members,
            "a-loose.bin",
            "b-loose.bin",
        )


@pytest.mark.parametrize("path", ["../escape", "/absolute", "a/../b", "a\\b", ""])
def test_stored_reader_валидирует_пути_для_обоих_методов(tmp_path, path):
    root = tmp_path / "legacy"
    root.mkdir()
    with _stored_reader(root) as reader:
        with pytest.raises(MemberPackError):
            reader.ordered_paths([path])
        with pytest.raises(MemberPackError):
            with reader.open(path):
                pass


def test_stored_reader_запрещает_вложенный_view_и_продолжает_после_ошибки(tmp_path):
    root = tmp_path / "pack"
    _write(root, {"a.bin": b"alpha", "b.bin": b"beta"})
    with _stored_reader(root) as reader:
        with reader.open("a.bin") as first:
            assert first.read(1) == b"a"
            with pytest.raises(MemberPackError):
                with reader.open("b.bin"):
                    pass
            assert first.read() == b"lpha"
        with pytest.raises(ValueError, match="ошибка member"):
            with reader.open("b.bin"):
                raise ValueError("ошибка member")
        with reader.open("a.bin") as source:
            assert source.read() == b"alpha"


@pytest.mark.parametrize("packed", [False, True])
def test_stored_reader_поддерживает_loose_fallback_и_отсутствующие_members(
    tmp_path, packed
):
    root = tmp_path / "members"
    if packed:
        _write(root, {"packed.bin": b"packed"})
    else:
        root.mkdir()
    (root / "legacy.bin").write_bytes(b"legacy")
    with _stored_reader(root) as reader:
        assert reader.ordered_paths(["legacy.bin"]) == ("legacy.bin",)
        with reader.open("legacy.bin") as source:
            assert source.read() == b"legacy"
        with pytest.raises(MemberPackError, match="отсутствует"):
            with reader.open("missing.bin"):
                pass
        with pytest.raises(MemberPackError, match="отсутствует"):
            reader.ordered_paths(["missing.bin"])


@pytest.mark.parametrize("target_name", [PACK_NAME, INDEX_NAME])
@pytest.mark.parametrize("replace", [False, True])
def test_stored_reader_отклоняет_изменение_открытой_пары_даже_с_прежним_mtime(
    tmp_path, monkeypatch, target_name, replace
):
    root = tmp_path / "pack"
    _write(root, {"a.bin": b"alpha"})
    target = root / target_name
    before = target.stat()
    payload = target.read_bytes()
    opened = _track_pack_streams(monkeypatch)

    with pytest.raises(MemberPackError):
        with _stored_reader(root) as reader:
            with reader.open("a.bin") as source:
                assert source.read() == b"alpha"
            if replace:
                replacement = root / "replacement"
                replacement.write_bytes(payload)
                replacement.replace(target)
            else:
                target.write_bytes(payload[:-1] + bytes([payload[-1] ^ 1]))
            os.utime(target, ns=(before.st_atime_ns, before.st_mtime_ns))
    assert len(opened) == 1
    assert opened[0].closed


def test_stored_reader_не_маскирует_ошибку_тела_и_закрывает_pack(
    tmp_path, monkeypatch
):
    root = tmp_path / "pack"
    _write(root, {"a.bin": b"alpha"})
    opened = _track_pack_streams(monkeypatch)
    failure = ValueError("исходная ошибка")
    with pytest.raises(ValueError) as caught:
        with _stored_reader(root):
            (root / PACK_NAME).unlink()
            raise failure
    assert caught.value is failure
    assert len(opened) == 1
    assert opened[0].closed


@pytest.mark.parametrize("missing", [PACK_NAME, INDEX_NAME])
def test_stored_reader_проверяет_парность_при_входе(tmp_path, missing):
    root = tmp_path / "pack"
    _write(root, {"a.bin": b"alpha"})
    (root / missing).unlink()
    with pytest.raises(MemberPackError, match="парой"):
        with _stored_reader(root):
            pass


@pytest.mark.parametrize("kind", ["symlink", "file"])
def test_stored_reader_проверяет_корень_при_входе(tmp_path, kind):
    root = tmp_path / "root"
    if kind == "symlink":
        target = tmp_path / "target"
        target.mkdir()
        root.symlink_to(target, target_is_directory=True)
    else:
        root.write_bytes(b"file")
    with pytest.raises(MemberPackError):
        with _stored_reader(root):
            pass


def test_stored_reader_закрывает_pack_после_ошибки_индекса(tmp_path, monkeypatch):
    root = tmp_path / "pack"
    _write(root, {"a.bin": b"alpha"})
    (root / INDEX_NAME).write_bytes(b"broken")
    opened = _track_pack_streams(monkeypatch)
    with pytest.raises(MemberPackError):
        with _stored_reader(root):
            pass
    assert all(source.closed for source in opened)


def test_index_cache_не_принимает_правку_с_восстановленным_mtime(tmp_path):
    root = tmp_path / "pack"
    _write(root, {"a.bin": b"alpha"})
    with open_stored_member(root, "a.bin") as source:
        assert source.read() == b"alpha"
    index = root / INDEX_NAME
    before = index.stat()
    index.write_bytes(index.read_bytes().replace(b"a.bin", b"b.bin"))
    os.utime(index, ns=(before.st_atime_ns, before.st_mtime_ns))

    with pytest.raises(MemberPackError, match="отсутствует"):
        open_stored_member(root, "a.bin")


@pytest.mark.parametrize("packed", [False, True])
def test_member_не_зависает_при_подмене_файла_на_fifo(
    tmp_path, monkeypatch, packed
):
    root = tmp_path / "members"
    if packed:
        _write(root, {"a.bin": b"alpha"})
        target = root / PACK_NAME
    else:
        root.mkdir()
        target = root / "a.bin"
        target.write_bytes(b"alpha")
    original_open = os.open
    descriptors = []

    def replace_before_open(path, flags, *args, **kwargs):
        if (packed and Path(path) == target) or (not packed and path == "a.bin"):
            # Проверка флага не даёт RED-прогону зависнуть на настоящем FIFO.
            assert flags & os.O_NONBLOCK
            target.unlink()
            os.mkfifo(target)
            descriptor = original_open(path, flags, *args, **kwargs)
            descriptors.append(descriptor)
            return descriptor
        return original_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(member_pack.os, "open", replace_before_open)
    with pytest.raises(MemberPackError):
        open_stored_member(root, "a.bin")
    assert len(descriptors) == 1
    with pytest.raises(OSError):
        os.fstat(descriptors[0])
