"""Профиль лимитов intake: browser- и incoming-ZIP всегда под бюджетом.

ZIP-бомбы здесь синтетические и маленькие: предел понижается через
``MCP1C_INTAKE_MAX_UNPACKED`` или параметр, поэтому тест не пишет на диск
больше нескольких килобайт и проверяет именно отказ, а не заполнение тома.
"""

from __future__ import annotations

import io
import zipfile
from collections import namedtuple
from pathlib import Path

import pytest

from mcp1c import intake_v2_collector
from mcp1c.intake_v2 import CandidateTransport
from mcp1c.intake_v2_lifecycle import CandidateCatalog, IntakeLifecycle
from mcp1c.intake_v2_operations import IntakeCoordinator
from mcp1c.intake_v2_probe import probe_export
from mcp1c.intake_v2 import DurableCandidateStore
from mcp1c.intake_v2_collector import CollectorError, collect_source_b
from mcp1c.intake_v2_transport import (
    BrowserStagingStore,
    TransportLimitError,
    ZipExportTree,
)
from mcp1c.resource_limits import (
    GIB,
    MIB,
    INTAKE_MAX_UNPACKED_ENV,
    LimitedReader,
    ResourceBudget,
    ResourceLimitError,
    ResourceLimits,
    intake_archive_limits,
)
from test_intake_v2_collector import _configuration


def _zip_bytes(payloads: dict[str, bytes]) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name, payload in payloads.items():
            archive.writestr(name, payload)
    return buffer.getvalue()


def _bomb(size: int) -> dict[str, bytes]:
    # Нули жмутся почти на пределе deflate (~1000:1): 8 МиБ занимают ~8 КиБ.
    return {
        "Configuration.xml": _configuration(),
        "Roles/Bomb.xml": b"\0" * size,
    }


def test_профиль_intake_по_умолчанию_выше_реальных_выгрузок():
    limits = intake_archive_limits({})

    # Крупнейшая реальная выгрузка: 2,01 ГиБ распакованных, запись 713 МиБ,
    # 37 037 записей, коэффициент 243,6:1 у XML на 2 МиБ.
    assert limits.max_total_bytes == 16 * GIB
    assert limits.max_entry_bytes == 4 * GIB
    assert limits.max_entries == 100_000
    assert limits.max_compression_ratio == 512
    assert limits.ratio_min_bytes == 1 * MIB


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("67108864", 64 * MIB),
        ("64MiB", 64 * MIB),
        ("64M", 64 * MIB),
        ("2GiB", 2 * GIB),
        (" 2g ", 2 * GIB),
        ("1T", 1024 * GIB),
    ],
)
def test_предел_распакованного_задаётся_окружением(raw, expected):
    limits = intake_archive_limits({INTAKE_MAX_UNPACKED_ENV: raw})

    assert limits.max_total_bytes == expected
    # Предел записи не может быть выше общего.
    assert limits.max_entry_bytes == min(4 * GIB, expected)


@pytest.mark.parametrize("raw", ["", "  "])
def test_пустое_значение_означает_умолчание(raw):
    assert intake_archive_limits({INTAKE_MAX_UNPACKED_ENV: raw}).max_total_bytes == (
        16 * GIB
    )


@pytest.mark.parametrize("raw", ["0", "-1", "много", "1.5G", "10X", "0M"])
def test_невалидный_предел_отвергается_понятно(raw):
    with pytest.raises(ValueError, match=INTAKE_MAX_UNPACKED_ENV):
        intake_archive_limits({INTAKE_MAX_UNPACKED_ENV: raw})


def test_малые_записи_не_проверяются_по_коэффициенту():
    limits = ResourceLimits(
        max_entries=10,
        max_entry_bytes=10_000,
        max_total_bytes=10_000,
        max_compression_ratio=4,
        ratio_min_bytes=100,
    )
    budget = ResourceBudget(limits, "архив")

    budget.validate_members([("small", 100, 1)])
    with pytest.raises(ResourceLimitError, match="коэффициент сжатия"):
        budget.validate_members([("large", 101, 1)])


def test_чтение_считает_фактические_байты_а_не_заявленный_размер():
    limits = ResourceLimits(
        max_entries=10,
        max_entry_bytes=10_000,
        max_total_bytes=10_000,
        max_compression_ratio=4,
        ratio_min_bytes=16,
    )
    budget = ResourceBudget(limits, "архив")
    # Заголовок мог соврать о размере: фактически поток отдаёт 100 байт при
    # 10 сжатых, т.е. 10:1 при пределе 4:1 сверх порога малых записей.
    reader = LimitedReader(
        io.BytesIO(b"x" * 100), budget, "entry", compressed_size=10
    )

    with pytest.raises(ResourceLimitError, match="коэффициент сжатия"):
        reader.read()
    assert budget.read_total <= 41


@pytest.mark.parametrize(
    "transport", [CandidateTransport.INCOMING, CandidateTransport.BROWSER]
)
def test_zip_bomb_отвергается_при_открытии_по_заявленному_составу(
    tmp_path, monkeypatch, transport
):
    archive = tmp_path / "bomb.zip"
    archive.write_bytes(_zip_bytes(_bomb(8 * MIB)))
    assert archive.stat().st_size < 64 * 1024
    monkeypatch.setenv(INTAKE_MAX_UNPACKED_ENV, "4MiB")

    # Первым срабатывает предел записи: он равен общему пределу 4 МиБ.
    with pytest.raises(TransportLimitError, match="превышает предел 4194304"):
        ZipExportTree(archive, transport=transport)


@pytest.mark.parametrize(
    "transport", [CandidateTransport.INCOMING, CandidateTransport.BROWSER]
)
def test_сжатие_сильнее_предела_отвергается_и_без_окружения(
    tmp_path, monkeypatch, transport
):
    monkeypatch.delenv(INTAKE_MAX_UNPACKED_ENV, raising=False)
    archive = tmp_path / "bomb.zip"
    archive.write_bytes(_zip_bytes(_bomb(8 * MIB)))

    with pytest.raises(TransportLimitError, match="коэффициент сжатия"):
        ZipExportTree(archive, transport=transport)


def test_параметр_профиля_ограничивает_прочитанный_объём(tmp_path):
    archive = tmp_path / "candidate.zip"
    archive.write_bytes(
        _zip_bytes({"Configuration.xml": b"12345", "Catalogs/Demo.xml": b"67890"})
    )
    limits = ResourceLimits(
        max_entries=10,
        max_entry_bytes=8,
        max_total_bytes=10,
        max_compression_ratio=50,
    )
    tree = ZipExportTree(
        archive, transport=CandidateTransport.INCOMING, limits=limits
    )
    try:
        with tree.open("Configuration.xml") as stream:
            assert stream.read() == b"12345"
        # Повторное чтение тоже расходует бюджет: считается прочитанное.
        with pytest.raises(TransportLimitError, match="прочитанный объём|предел"):
            with tree.open("Configuration.xml") as stream:
                stream.read()
                stream.read()
            with tree.open("Catalogs/Demo.xml") as stream:
                stream.read()
    finally:
        tree.close()


def test_incoming_zip_bomb_даёт_понятную_причину_в_refresh(tmp_path, monkeypatch):
    monkeypatch.setenv(INTAKE_MAX_UNPACKED_ENV, "4MiB")
    incoming = tmp_path / "incoming"
    incoming.mkdir()
    (incoming / "bomb.zip").write_bytes(_zip_bytes(_bomb(8 * MIB)))
    managed = tmp_path / "managed"
    records = DurableCandidateStore(managed / "records")

    snapshot = IntakeLifecycle(
        CandidateCatalog(managed / "catalog"),
        BrowserStagingStore(managed / "uploads"),
        IntakeCoordinator(managed / "operations", records),
        incoming_root=incoming,
        directory_settle_seconds=0,
    ).refresh()

    assert snapshot.candidates == ()
    assert len(snapshot.issues) == 1
    assert "превышает предел 4194304" in snapshot.issues[0].message
    # Отказ случился до сборки: на managed-томе нет распакованных байтов.
    written = sum(
        path.stat().st_size for path in managed.rglob("*") if path.is_file()
    )
    assert written < 64 * 1024


def test_browser_upload_с_бомбой_отвергается_тем_же_профилем(tmp_path, monkeypatch):
    monkeypatch.setenv(INTAKE_MAX_UNPACKED_ENV, "4MiB")
    store = BrowserStagingStore(tmp_path / "managed")
    raw = _zip_bytes(_bomb(8 * MIB))
    store.accept("candidate-bomb", "bomb.zip", io.BytesIO(raw))

    with pytest.raises(TransportLimitError, match="превышает предел 4194304"):
        store.open_tree("candidate-bomb")


def test_сумма_записей_сверяется_с_общим_пределом(tmp_path, monkeypatch):
    monkeypatch.setenv(INTAKE_MAX_UNPACKED_ENV, "4MiB")
    archive = tmp_path / "many.zip"
    # Каждая запись ниже порога проверки сжатия и предела записи, но вместе
    # они больше общего предела: так собирается бомба из многих малых частей.
    archive.write_bytes(
        _zip_bytes(
            {
                "Configuration.xml": _configuration(),
                **{f"Roles/Part{index}.xml": b"\0" * MIB for index in range(5)},
            }
        )
    )

    with pytest.raises(TransportLimitError, match="суммарный распакованный объём"):
        ZipExportTree(archive, transport=CandidateTransport.INCOMING)


def test_сборка_проверяет_свободное_место_до_записи(tmp_path, monkeypatch):
    archive = tmp_path / "candidate.zip"
    archive.write_bytes(
        _zip_bytes(
            {
                "Configuration.xml": _configuration(),
                "Catalogs/Demo/Ext/ObjectModule.bsl": b"procedure Demo() endprocedure",
            }
        )
    )
    tree = ZipExportTree(archive, transport=CandidateTransport.INCOMING)
    probe = probe_export(tree)
    usage = namedtuple("usage", "total used free")
    monkeypatch.setattr(
        intake_v2_collector.shutil,
        "disk_usage",
        lambda _path: usage(10, 10, 0),
    )
    target = tmp_path / "work" / "collection"
    try:
        with pytest.raises(CollectorError, match="свободного места"):
            collect_source_b(tree, probe, target)
    finally:
        tree.close()

    assert not target.exists()
    assert list((tmp_path / "work").glob(".*.tmp")) == []
