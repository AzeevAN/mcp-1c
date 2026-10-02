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


def _collect_from(tmp_path, archive, transport=CandidateTransport.INCOMING):
    tree = ZipExportTree(archive, transport=transport)
    try:
        probe = probe_export(tree)
        return collect_source_b(tree, probe, tmp_path / "work" / "collection")
    finally:
        tree.close()


def _written(root: Path) -> int:
    return sum(path.stat().st_size for path in root.rglob("*") if path.is_file())


@pytest.mark.parametrize(
    "transport", [CandidateTransport.INCOMING, CandidateTransport.BROWSER]
)
def test_zip_bomb_отвергается_до_сборки_по_заявленному_составу(
    tmp_path, monkeypatch, transport
):
    archive = tmp_path / "bomb.zip"
    archive.write_bytes(_zip_bytes(_bomb(8 * MIB)))
    assert archive.stat().st_size < 64 * 1024
    monkeypatch.setenv(INTAKE_MAX_UNPACKED_ENV, "4MiB")

    # Первым срабатывает предел записи: он равен общему пределу 4 МиБ.
    with pytest.raises(CollectorError, match="превышает предел 4194304"):
        _collect_from(tmp_path, archive, transport)
    # Отказ до первой записи: на томе сборки нет распакованных байтов.
    assert _written(tmp_path / "work") == 0


@pytest.mark.parametrize(
    "transport", [CandidateTransport.INCOMING, CandidateTransport.BROWSER]
)
def test_сжатие_сильнее_предела_отвергается_и_без_окружения(
    tmp_path, monkeypatch, transport
):
    monkeypatch.delenv(INTAKE_MAX_UNPACKED_ENV, raising=False)
    archive = tmp_path / "bomb.zip"
    archive.write_bytes(_zip_bytes(_bomb(8 * MIB)))

    with pytest.raises(CollectorError, match="коэффициент сжатия"):
        _collect_from(tmp_path, archive, transport)
    # И прямое открытие записи сверяет тот же предел.
    tree = ZipExportTree(archive, transport=transport)
    try:
        with pytest.raises(TransportLimitError, match="коэффициент сжатия"):
            tree.open("Roles/Bomb.xml")
    finally:
        tree.close()


def test_непрочитанный_балласт_не_блокирует_ни_обнаружение_ни_сборку(
    tmp_path, monkeypatch
):
    """Балласт не читается, значит не распаковывается и бомбой не является."""
    monkeypatch.setenv(INTAKE_MAX_UNPACKED_ENV, "4MiB")
    archive = tmp_path / "ballast.zip"
    archive.write_bytes(
        _zip_bytes(
            {
                "Configuration.xml": _configuration(),
                "Catalogs/Demo/Ext/ObjectModule.bsl": b"procedure Demo() endprocedure",
                # Больше общего предела и сжат ~1000:1, но collector его не берёт.
                "Ext/ParentConfigurations/Supply.cf": b"\0" * (8 * MIB),
            }
        )
    )
    incoming = tmp_path / "incoming"
    incoming.mkdir()
    archive.replace(incoming / "ballast.zip")
    managed = tmp_path / "managed"
    snapshot = IntakeLifecycle(
        CandidateCatalog(managed / "catalog"),
        BrowserStagingStore(managed / "uploads"),
        IntakeCoordinator(managed / "operations", DurableCandidateStore(managed / "records")),
        incoming_root=incoming,
        directory_settle_seconds=0,
    ).refresh()
    assert snapshot.issues == ()
    assert len(snapshot.candidates) == 1

    result = _collect_from(tmp_path, incoming / "ballast.zip")
    assert [item.source_path for item in result.code] == [
        "Catalogs/Demo/Ext/ObjectModule.bsl"
    ]


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


def test_browser_upload_с_бомбой_отвергается_тем_же_профилем(tmp_path, monkeypatch):
    monkeypatch.setenv(INTAKE_MAX_UNPACKED_ENV, "4MiB")
    store = BrowserStagingStore(tmp_path / "managed")
    raw = _zip_bytes(_bomb(8 * MIB))
    store.accept("candidate-bomb", "bomb.zip", io.BytesIO(raw))

    tree = store.open_tree("candidate-bomb")
    try:
        with pytest.raises(TransportLimitError, match="превышает предел 4194304"):
            tree.validate_selection(["Roles/Bomb.xml"])
        with pytest.raises(TransportLimitError, match="предел"):
            tree.open("Roles/Bomb.xml")
    finally:
        tree.close()


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

    tree = ZipExportTree(archive, transport=CandidateTransport.INCOMING)
    try:
        with pytest.raises(TransportLimitError, match="суммарный распакованный объём"):
            tree.validate_selection(tree.paths())
    finally:
        tree.close()


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


def test_оценка_места_учитывает_только_выбранные_записи(tmp_path, monkeypatch):
    archive = tmp_path / "candidate.zip"
    selected = {
        "Configuration.xml": _configuration(),
        "Catalogs/Demo/Ext/ObjectModule.bsl": b"procedure Demo() endprocedure",
    }
    archive.write_bytes(
        _zip_bytes({**selected, "Ext/ParentConfigurations/Supply.cf": b"\0" * (8 * MIB)})
    )
    needed = sum(map(len, selected.values())) + intake_v2_collector._FREE_SPACE_RESERVE
    usage = namedtuple("usage", "total used free")
    monkeypatch.setattr(
        intake_v2_collector.shutil, "disk_usage", lambda _path: usage(0, 0, needed)
    )

    result = _collect_from(tmp_path, archive)
    assert len(result.code) == 1


def test_limited_reader_не_читает_мимо_учёта():
    limits = ResourceLimits(
        max_entries=10, max_entry_bytes=8, max_total_bytes=8, max_compression_ratio=50
    )
    budget = ResourceBudget(limits, "архив")
    reader = LimitedReader(io.BytesIO(b"x" * 100), budget, "entry")

    buffer = bytearray(4)
    assert reader.readinto(buffer) == 4
    assert reader.read1(4) == b"xxxx"
    assert budget.read_total == 8
    with pytest.raises(ResourceLimitError):
        reader.read1(1)
    # Методы, которые прочитали бы исходный поток без учёта, не делегируются.
    for name in ("readline", "readlines", "seek", "tell", "peek", "fileno"):
        with pytest.raises(AttributeError):
            getattr(reader, name)
    assert reader.seekable() is False


def test_каталог_сверяет_сумму_выбранных_файлов_до_сборки(tmp_path):
    from mcp1c.intake_v2_transport import DirectoryExportTree
    from mcp1c.resource_limits import ResourceLimits

    root = tmp_path / "export"
    (root / "Roles").mkdir(parents=True)
    (root / "Configuration.xml").write_bytes(_configuration())
    for index in range(5):
        (root / "Roles" / f"Part{index}.xml").write_bytes(b"\0" * 1024)
    limits = ResourceLimits(
        max_entries=100,
        max_entry_bytes=4096,
        max_total_bytes=4096,
        max_compression_ratio=200,
    )
    tree = DirectoryExportTree(root, limits=limits, settle_seconds=0)

    # Каждый файл в пределе, вместе — больше: отказ до первой записи staging.
    with pytest.raises(TransportLimitError, match="суммарный"):
        tree.validate_selection(tree.paths())
    with pytest.raises(CollectorError, match="суммарный"):
        collect_source_b(tree, probe_export(tree), tmp_path / "collection")
    assert not (tmp_path / "collection").exists()
