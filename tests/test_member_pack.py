"""Контракты последовательного хранения immutable members."""

from __future__ import annotations

import json

import pytest

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
