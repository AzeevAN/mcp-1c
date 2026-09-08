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


def test_pack_читает_нужный_диапазон_и_пустой_member(tmp_path):
    root = tmp_path / "pack"
    _write(root, {"payload/a.bin": b"alpha", "payload/empty.bin": b""})

    with open_stored_member(root, "payload/a.bin") as source:
        assert source.read(2) == b"al"
        assert source.read() == b"pha"
        assert source.read() == b""
    with open_stored_member(root, "payload/empty.bin") as source:
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
