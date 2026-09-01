"""RED-контракты безопасных транспортов CONFIG-INTAKE-V2.

Все входы синтетические. Тесты не создают Registry и не обращаются к ``data/``:
на этом этапе транспорт только сохраняет, перечисляет и потоково читает байты.
"""

from __future__ import annotations

import importlib
import io
import os
import stat
import time
import warnings
import zipfile
from collections import namedtuple
from pathlib import Path

import pytest

from mcp1c.intake_v2 import CandidateTransport, VirtualExportTree
from mcp1c.resource_limits import ResourceLimits


SUBJECT = "mcp1c.intake_v2_transport"


def _symbol(name: str):
    try:
        module = importlib.import_module(SUBJECT)
    except ModuleNotFoundError as error:
        if error.name != SUBJECT:
            raise
        pytest.fail(f"RED: отсутствует модуль {SUBJECT} для контракта {name}")
    if not hasattr(module, name):
        pytest.fail(f"RED: в {SUBJECT} отсутствует контракт {name}")
    return getattr(module, name)


def _write_zip(path: Path, payloads: dict[str, bytes]) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name, payload in payloads.items():
            archive.writestr(name, payload)
    raw = buffer.getvalue()
    path.write_bytes(raw)
    return raw


def _write_tree(root: Path, payloads: dict[str, bytes]) -> None:
    for name, payload in payloads.items():
        target = root.joinpath(*name.split("/"))
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(payload)


def _small_limits(**changes: int) -> ResourceLimits:
    values = {
        "max_entries": 20,
        "max_entry_bytes": 1024,
        "max_total_bytes": 4096,
        "max_compression_ratio": 50,
    }
    values.update(changes)
    return ResourceLimits(**values)


def test_четыре_транспорта_дают_один_virtual_tree_и_safe_origin(tmp_path):
    BrowserStagingStore = _symbol("BrowserStagingStore")
    open_export_tree = _symbol("open_export_tree")

    payloads = {
        "Catalogs/Demo/Ext/ObjectModule.bsl": b"procedure Demo()\nendprocedure",
        "Configuration.xml": b"<MetaDataObject/>",
    }
    archive_path = tmp_path / "outside" / "candidate.zip"
    archive_path.parent.mkdir()
    archive_bytes = _write_zip(archive_path, payloads)
    directory_path = tmp_path / "outside" / "candidate"
    _write_tree(directory_path, payloads)

    browser = BrowserStagingStore(
        tmp_path / "managed", max_upload_bytes=len(archive_bytes)
    )
    staged = browser.accept(
        "candidate-browser",
        r"C:\fakepath\demo-export.zip",
        io.BytesIO(archive_bytes),
        expected_size=len(archive_bytes),
    )

    trees = (
        browser.open_tree(staged.candidate_id, limits=_small_limits()),
        open_export_tree(
            archive_path,
            CandidateTransport.INCOMING,
            limits=_small_limits(),
        ),
        open_export_tree(
            archive_path,
            CandidateTransport.LOCAL_FILE,
            limits=_small_limits(),
        ),
        open_export_tree(
            directory_path,
            CandidateTransport.LOCAL_DIRECTORY,
            limits=_small_limits(),
            directory_settle_seconds=0,
        ),
    )

    for tree in trees:
        assert isinstance(tree, VirtualExportTree)
        assert tree.paths() == tuple(sorted(payloads))
        assert "/outside/" not in tree.origin_name
        for name, payload in payloads.items():
            assert tree.size(name) == len(payload)
            with tree.open(name) as stream:
                assert stream.read() == payload
        assert tree.verify_stable(tree.fingerprint()) is True

    assert staged.origin_name == "demo-export.zip"
    assert [tree.transport for tree in trees] == [
        CandidateTransport.BROWSER,
        CandidateTransport.INCOMING,
        CandidateTransport.LOCAL_FILE,
        CandidateTransport.LOCAL_DIRECTORY,
    ]


def test_browser_staging_принимает_ровно_лимит_и_удаляет_отказ(tmp_path):
    BrowserStagingStore = _symbol("BrowserStagingStore")
    TransportError = _symbol("TransportError")
    TransportLimitError = _symbol("TransportLimitError")

    store = BrowserStagingStore(tmp_path / "managed", max_upload_bytes=8)
    accepted = store.accept(
        "candidate-ok", "demo.zip", io.BytesIO(b"12345678"), expected_size=8
    )

    assert accepted.size == 8
    assert accepted.sha256 == "ef797c8118f02dfb649607dd5d3f8c7623048c9c063d532cc95c5ed7a898a64f"
    assert BrowserStagingStore(
        tmp_path / "managed", max_upload_bytes=8
    ).load("candidate-ok") == accepted

    duplicate = io.BytesIO(b"changed")
    with pytest.raises(TransportError, match="существ"):
        store.accept("candidate-ok", "other.zip", duplicate, expected_size=7)
    assert duplicate.tell() == 0
    assert store.load("candidate-ok") == accepted

    with pytest.raises(TransportLimitError, match="предел"):
        store.accept(
            "candidate-large", "large.zip", io.BytesIO(b"123456789")
        )

    with pytest.raises(KeyError):
        store.load("candidate-large")
    assert not any("candidate-large" in path.name for path in store.root.rglob("*"))


def test_browser_staging_проверяет_место_до_копирования(tmp_path, monkeypatch):
    BrowserStagingStore = _symbol("BrowserStagingStore")
    TransportLimitError = _symbol("TransportLimitError")
    module = importlib.import_module(SUBJECT)

    usage = namedtuple("usage", "total used free")
    store = BrowserStagingStore(
        tmp_path / "managed",
        max_upload_bytes=100,
        free_space_reserve=10,
    )
    monkeypatch.setattr(module.shutil, "disk_usage", lambda _path: usage(100, 95, 5))

    source = io.BytesIO(b"1234")
    with pytest.raises(TransportLimitError, match="мест|свобод"):
        store.accept(
            "candidate-no-space",
            "demo.zip",
            source,
            expected_size=4,
        )

    assert source.tell() == 0
    assert not any("candidate-no-space" in path.name for path in store.root.rglob("*"))


@pytest.mark.parametrize(
    "names",
    [
        ("../outside.xml",),
        ("/absolute.xml",),
        ("folder/item.xml", "folder//item.xml"),
        ("folder/item.xml", "folder/./item.xml"),
        (r"folder\item.xml",),
    ],
)
def test_zip_tree_fail_closed_на_опасном_или_неоднозначном_пути(
    tmp_path, names
):
    TransportSecurityError = _symbol("TransportSecurityError")
    ZipExportTree = _symbol("ZipExportTree")

    archive_path = tmp_path / "unsafe.zip"
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", UserWarning)
        with zipfile.ZipFile(archive_path, "w") as archive:
            for name in names:
                archive.writestr(name, b"payload")

    with pytest.raises(TransportSecurityError, match="путь|дубл|неоднознач"):
        ZipExportTree(
            archive_path,
            transport=CandidateTransport.INCOMING,
            limits=_small_limits(),
        )


def test_zip_tree_отвергает_symlink_и_zip_bomb_до_open(tmp_path):
    TransportLimitError = _symbol("TransportLimitError")
    TransportSecurityError = _symbol("TransportSecurityError")
    ZipExportTree = _symbol("ZipExportTree")

    symlink_zip = tmp_path / "symlink.zip"
    link = zipfile.ZipInfo("Catalogs/link.xml")
    link.create_system = 3
    link.external_attr = (stat.S_IFLNK | 0o777) << 16
    with zipfile.ZipFile(symlink_zip, "w") as archive:
        archive.writestr(link, "../../outside")

    with pytest.raises(TransportSecurityError, match="символическ"):
        ZipExportTree(
            symlink_zip,
            transport=CandidateTransport.LOCAL_FILE,
            limits=_small_limits(),
        )

    bomb_zip = tmp_path / "bomb.zip"
    with zipfile.ZipFile(
        bomb_zip, "w", compression=zipfile.ZIP_DEFLATED
    ) as archive:
        archive.writestr("Configuration.xml", b" " * 4096)

    with pytest.raises(TransportLimitError, match="сжати|предел"):
        ZipExportTree(
            bomb_zip,
            transport=CandidateTransport.INCOMING,
            limits=_small_limits(
                max_entry_bytes=8192,
                max_total_bytes=8192,
                max_compression_ratio=2,
            ),
        )


def test_directory_tree_отвергает_symlink_и_лимиты_до_open(tmp_path):
    DirectoryExportTree = _symbol("DirectoryExportTree")
    TransportLimitError = _symbol("TransportLimitError")
    TransportSecurityError = _symbol("TransportSecurityError")

    outside = tmp_path / "outside.xml"
    outside.write_bytes(b"outside")
    linked_root = tmp_path / "linked-root"
    linked_root.symlink_to(tmp_path, target_is_directory=True)
    with pytest.raises(TransportSecurityError, match="символическ"):
        DirectoryExportTree(linked_root, limits=_small_limits(), settle_seconds=0)

    tree_root = tmp_path / "tree"
    tree_root.mkdir()
    (tree_root / "Configuration.xml").write_bytes(b"ok")
    (tree_root / "linked.xml").symlink_to(outside)
    with pytest.raises(TransportSecurityError, match="символическ"):
        DirectoryExportTree(tree_root, limits=_small_limits(), settle_seconds=0)

    limited_root = tmp_path / "limited"
    _write_tree(limited_root, {"one.xml": b"12345", "two.xml": b"67890"})
    with pytest.raises(TransportLimitError, match="суммар|предел"):
        DirectoryExportTree(
            limited_root,
            limits=_small_limits(max_entry_bytes=8, max_total_bytes=9),
            settle_seconds=0,
        )


def test_tree_обнаруживает_изменение_zip_и_каталога_во_время_операции(tmp_path):
    DirectoryExportTree = _symbol("DirectoryExportTree")
    TransportUnstableError = _symbol("TransportUnstableError")
    ZipExportTree = _symbol("ZipExportTree")

    archive_path = tmp_path / "candidate.zip"
    _write_zip(archive_path, {"Configuration.xml": b"first"})
    zip_tree = ZipExportTree(
        archive_path,
        transport=CandidateTransport.INCOMING,
        limits=_small_limits(),
    )
    zip_fingerprint = zip_tree.fingerprint()
    _write_zip(
        archive_path,
        {"Configuration.xml": b"second", "Catalogs/Demo.xml": b"new"},
    )

    assert zip_tree.verify_stable(zip_fingerprint) is False
    with pytest.raises(TransportUnstableError, match="измен"):
        zip_tree.open("Configuration.xml")

    directory_path = tmp_path / "tree"
    _write_tree(directory_path, {"Configuration.xml": b"first"})
    directory_tree = DirectoryExportTree(
        directory_path,
        limits=_small_limits(),
        settle_seconds=0,
    )
    directory_fingerprint = directory_tree.fingerprint()
    (directory_path / "Configuration.xml").write_bytes(b"changed")

    assert directory_tree.verify_stable(directory_fingerprint) is False
    with pytest.raises(TransportUnstableError, match="измен"):
        directory_tree.open("Configuration.xml")


def test_zip_tree_превращает_crc_failure_в_обезличенный_отказ(tmp_path):
    TransportError = _symbol("TransportError")
    ZipExportTree = _symbol("ZipExportTree")

    archive_path = tmp_path / "broken.zip"
    _write_zip(archive_path, {"Configuration.xml": b"synthetic payload"})
    with zipfile.ZipFile(archive_path) as archive:
        info = archive.getinfo("Configuration.xml")
        header_offset = info.header_offset
        compressed_size = info.compress_size
    raw = bytearray(archive_path.read_bytes())
    name_length = int.from_bytes(raw[header_offset + 26 : header_offset + 28], "little")
    extra_length = int.from_bytes(raw[header_offset + 28 : header_offset + 30], "little")
    payload_offset = header_offset + 30 + name_length + extra_length
    raw[payload_offset + compressed_size // 2] ^= 0xFF
    archive_path.write_bytes(raw)

    tree = ZipExportTree(
        archive_path,
        transport=CandidateTransport.LOCAL_FILE,
        limits=_small_limits(),
    )
    with pytest.raises(TransportError, match="ZIP|CRC|поврежд"):
        with tree.open("Configuration.xml") as stream:
            stream.read()


def test_directory_tree_не_блокирует_поток_но_ждёт_settle_window(tmp_path):
    DirectoryExportTree = _symbol("DirectoryExportTree")
    TransportUnstableError = _symbol("TransportUnstableError")

    root = tmp_path / "tree"
    _write_tree(root, {"Configuration.xml": b"ok"})

    started = time.monotonic()
    with pytest.raises(TransportUnstableError, match="ещё изменяется"):
        DirectoryExportTree(root, limits=_small_limits(), settle_seconds=5)
    assert time.monotonic() - started < 1

    old = time.time_ns() - 10_000_000_000
    os.utime(root / "Configuration.xml", ns=(old, old))
    os.utime(root, ns=(old, old))
    tree = DirectoryExportTree(root, limits=_small_limits(), settle_seconds=5)
    assert tree.verify_stable(tree.fingerprint()) is True


def test_zip_tree_читает_центральный_каталог_один_раз(tmp_path, monkeypatch):
    TransportError = _symbol("TransportError")
    ZipExportTree = _symbol("ZipExportTree")
    module = importlib.import_module(SUBJECT)

    archive_path = tmp_path / "candidate.zip"
    _write_zip(archive_path, {"one.xml": b"one", "two.xml": b"two"})
    original = module.zipfile.ZipFile
    opened = 0

    def tracked_zip(*args, **kwargs):
        nonlocal opened
        opened += 1
        return original(*args, **kwargs)

    monkeypatch.setattr(module.zipfile, "ZipFile", tracked_zip)
    tree = ZipExportTree(
        archive_path,
        transport=CandidateTransport.LOCAL_FILE,
        limits=_small_limits(),
    )

    for name in tree.paths():
        with tree.open(name) as stream:
            assert stream.read() in {b"one", b"two"}
    assert opened == 1

    tree.close()
    assert tree.verify_stable(tree.fingerprint()) is False
    with pytest.raises(TransportError, match="закрыт"):
        tree.open("one.xml")


def test_browser_staging_restart_cleanup_сохраняет_только_committed(tmp_path):
    BrowserStagingStore = _symbol("BrowserStagingStore")

    root = tmp_path / "managed"
    first = BrowserStagingStore(root, max_upload_bytes=1024)
    committed = first.accept(
        "candidate-ok", "demo.zip", io.BytesIO(b"committed"), expected_size=9
    )
    (first.payloads_dir / ".interrupted.part").write_bytes(b"partial")
    (first.records_dir / ".interrupted.part").write_bytes(b"partial")
    (first.payloads_dir / "orphan.upload").write_bytes(b"orphan")
    (first.records_dir / "missing.json").write_text("{}", encoding="utf-8")

    restarted = BrowserStagingStore(root, max_upload_bytes=1024)

    assert restarted.load("candidate-ok") == committed
    assert not (restarted.payloads_dir / ".interrupted.part").exists()
    assert not (restarted.records_dir / ".interrupted.part").exists()
    assert not (restarted.payloads_dir / "orphan.upload").exists()
    assert not (restarted.records_dir / "missing.json").exists()
    assert not (root / "registry.json").exists()

    restarted.discard("candidate-ok")
    with pytest.raises(KeyError):
        restarted.load("candidate-ok")


def test_unknown_и_traversal_path_не_открываются(tmp_path):
    TransportSecurityError = _symbol("TransportSecurityError")
    open_export_tree = _symbol("open_export_tree")

    root = tmp_path / "tree"
    _write_tree(root, {"Configuration.xml": b"ok"})
    tree = open_export_tree(
        root,
        CandidateTransport.LOCAL_DIRECTORY,
        limits=_small_limits(),
        directory_settle_seconds=0,
    )

    with pytest.raises(KeyError):
        tree.open("missing.xml")
    with pytest.raises(TransportSecurityError, match="путь"):
        tree.open("../outside.xml")
    with pytest.raises(TransportSecurityError, match="путь"):
        tree.size("/outside.xml")
