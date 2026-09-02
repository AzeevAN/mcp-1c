"""RED-контракты on-demand lifecycle кандидатов единого intake."""

from __future__ import annotations

import importlib
import io
import json
import zipfile
from pathlib import Path

import pytest

from mcp1c.intake_v2 import CandidateJobState, DurableCandidateStore, SourceKind
from mcp1c.intake_v2_operations import IntakeCoordinator
from mcp1c.intake_v2_transport import BrowserStagingStore
from test_intake_v2_collector import _configuration


SUBJECT = "mcp1c.intake_v2_lifecycle"
NS = "http://v8.1c.ru/8.3/MDClasses"


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


def _extension(name: str = "DemoExtension") -> bytes:
    return (
        f'<MetaDataObject xmlns="{NS}"><Configuration><Properties>'
        f"<Name>{name}</Name><Version>1.0</Version>"
        "<NamePrefix>Demo_</NamePrefix><ObjectBelonging>Adopted</ObjectBelonging>"
        "<ConfigurationExtensionPurpose>AddOn</ConfigurationExtensionPurpose>"
        "<CompatibilityMode></CompatibilityMode>"
        "</Properties></Configuration></MetaDataObject>"
    ).encode()


def _archive(path: Path, descriptor: bytes, *, marker: bytes = b"") -> bytes:
    payload = io.BytesIO()
    with zipfile.ZipFile(payload, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("Configuration.xml", descriptor)
        if marker:
            archive.writestr("CommonModules/Demo/Ext/Module.bsl", marker)
    raw = payload.getvalue()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(raw)
    return raw


def _backend(tmp_path: Path):
    managed = tmp_path / "managed"
    browser = BrowserStagingStore(managed / "uploads")
    records = DurableCandidateStore(managed / "records")
    operations = IntakeCoordinator(managed / "operations", records)
    return managed, browser, records, operations


def test_refresh_объединяет_browser_и_incoming_по_internal_identity_без_publish(
    tmp_path,
):
    CandidateCatalog = _symbol("CandidateCatalog")
    IntakeLifecycle = _symbol("IntakeLifecycle")

    managed, browser, records, operations = _backend(tmp_path)
    browser_raw = _archive(
        tmp_path / "browser.zip", _configuration("DemoConfiguration"), marker=b"browser"
    )
    browser.accept(
        "browser-001",
        "renamed-browser.zip",
        io.BytesIO(browser_raw),
        expected_size=len(browser_raw),
    )
    incoming = tmp_path / "incoming"
    _archive(
        incoming / "first-name.zip",
        _configuration("DemoConfiguration"),
        marker=b"incoming-one",
    )
    _archive(
        incoming / "second-name.zip",
        _configuration("DemoConfiguration"),
        marker=b"incoming-two",
    )

    catalog = CandidateCatalog(managed / "catalog")
    lifecycle = IntakeLifecycle(
        catalog,
        browser,
        operations,
        incoming_root=incoming,
        directory_settle_seconds=0,
    )
    accepted = lifecycle.discover_browser("browser-001")
    snapshot = lifecycle.refresh()

    assert len(snapshot.candidates) == 3
    assert accepted in snapshot.candidates
    assert snapshot.issues == ()
    assert tuple(snapshot.groups) == (("configuration", "DemoConfiguration"),)
    assert len(snapshot.groups[("configuration", "DemoConfiguration")]) == 3
    assert {item.probe.origin_name for item in snapshot.candidates} == {
        "renamed-browser.zip",
        "first-name.zip",
        "second-name.zip",
    }
    assert not (tmp_path / "registry.json").exists()
    assert list(records.candidates_dir.iterdir()) == []

    restarted = IntakeLifecycle(
        CandidateCatalog(managed / "catalog"),
        BrowserStagingStore(managed / "uploads"),
        IntakeCoordinator(
            managed / "operations", DurableCandidateStore(managed / "records")
        ),
        incoming_root=incoming,
        directory_settle_seconds=0,
    ).refresh()
    assert [item.candidate_id for item in restarted.candidates] == [
        item.candidate_id for item in snapshot.candidates
    ]


def test_local_source_поддерживает_zip_каталог_zip_и_unpacked_root(tmp_path):
    CandidateCatalog = _symbol("CandidateCatalog")
    IntakeLifecycle = _symbol("IntakeLifecycle")

    managed, browser, _records, operations = _backend(tmp_path)
    single = tmp_path / "local" / "single.zip"
    _archive(single, _configuration("SingleConfiguration"))
    archive_directory = tmp_path / "local" / "archives"
    _archive(archive_directory / "a.zip", _configuration("ArchiveA"))
    _archive(archive_directory / "b.ZIP", _configuration("ArchiveB"))
    unpacked = tmp_path / "local" / "unpacked"
    unpacked.mkdir(parents=True)
    (unpacked / "Configuration.xml").write_bytes(_configuration("Unpacked"))

    snapshot = IntakeLifecycle(
        CandidateCatalog(managed / "catalog"),
        browser,
        operations,
        local_sources={
            "single": single,
            "archives": archive_directory,
            "unpacked": unpacked,
        },
        directory_settle_seconds=0,
    ).refresh()

    assert snapshot.issues == ()
    assert {item.probe.internal_name for item in snapshot.candidates} == {
        "SingleConfiguration",
        "ArchiveA",
        "ArchiveB",
        "Unpacked",
    }
    persisted = [
        json.dumps(item.to_dict(), ensure_ascii=False)
        for item in snapshot.candidates
    ]
    assert all(str(tmp_path) not in raw for raw in persisted)
    assert {
        (item.locator.source_id, item.locator.entry_name)
        for item in snapshot.candidates
    } == {
        ("single", ""),
        ("archives", "a.zip"),
        ("archives", "b.ZIP"),
        ("unpacked", ""),
    }


def test_расширение_durable_до_выбора_родителя_и_start_возобновляем(tmp_path):
    CandidateCatalog = _symbol("CandidateCatalog")
    IntakeLifecycle = _symbol("IntakeLifecycle")
    LifecycleConflict = _symbol("LifecycleConflict")

    managed, browser, records, operations = _backend(tmp_path)
    raw = _archive(tmp_path / "extension.zip", _extension())
    browser.accept(
        "extension-001",
        "extension.zip",
        io.BytesIO(raw),
        expected_size=len(raw),
    )
    lifecycle = IntakeLifecycle(
        CandidateCatalog(managed / "catalog"), browser, operations
    )
    discovered = lifecycle.refresh().candidates[0]

    assert discovered.probe.source_kind is SourceKind.EXTENSION
    assert discovered.grouping_key == ("extension", "DemoExtension")
    assert not (managed / "records" / "candidates" / "extension-001.json").exists()
    with pytest.raises(LifecycleConflict, match="родител"):
        lifecycle.start("job-extension", discovered.candidate_id)
    with pytest.raises(KeyError):
        records.load_job("job-extension")

    restarted = IntakeLifecycle(
        CandidateCatalog(managed / "catalog"),
        BrowserStagingStore(managed / "uploads"),
        IntakeCoordinator(
            managed / "operations", DurableCandidateStore(managed / "records")
        ),
    )
    candidate = restarted.start(
        "job-extension",
        discovered.candidate_id,
        parent_configuration="DemoConfiguration",
    )
    assert candidate.identity.parent_configuration == "DemoConfiguration"
    assert DurableCandidateStore(managed / "records").load_job(
        "job-extension"
    ).state is CandidateJobState.READY
    assert restarted.start(
        "job-extension",
        discovered.candidate_id,
        parent_configuration="DemoConfiguration",
    ) == candidate


def test_refresh_изолирует_ошибки_а_start_отвергает_изменённый_source(tmp_path):
    CandidateCatalog = _symbol("CandidateCatalog")
    IntakeLifecycle = _symbol("IntakeLifecycle")
    LifecycleConflict = _symbol("LifecycleConflict")

    managed, browser, records, operations = _backend(tmp_path)
    incoming = tmp_path / "incoming"
    good = incoming / "good.zip"
    _archive(good, _configuration("StableConfiguration"), marker=b"first")
    (incoming / "broken.zip").write_bytes(b"not a zip")
    lifecycle = IntakeLifecycle(
        CandidateCatalog(managed / "catalog"),
        browser,
        operations,
        incoming_root=incoming,
        directory_settle_seconds=0,
    )

    snapshot = lifecycle.refresh()
    assert [item.probe.internal_name for item in snapshot.candidates] == [
        "StableConfiguration"
    ]
    assert len(snapshot.issues) == 1
    assert snapshot.issues[0].origin_name == "broken.zip"
    assert len(snapshot.issues[0].message) <= 2048
    candidate_id = snapshot.candidates[0].candidate_id

    _archive(good, _configuration("StableConfiguration"), marker=b"second")
    with pytest.raises(LifecycleConflict, match="измен"):
        lifecycle.start("job-stale", candidate_id)
    assert records.load_job("job-stale").state is CandidateJobState.FAILED


def test_constructor_не_сканирует_источники_и_refresh_не_выбирает_newest(
    tmp_path, monkeypatch
):
    CandidateCatalog = _symbol("CandidateCatalog")
    IntakeLifecycle = _symbol("IntakeLifecycle")
    module = importlib.import_module(SUBJECT)

    managed, browser, _records, operations = _backend(tmp_path)
    incoming = tmp_path / "incoming"
    _archive(incoming / "z-last.zip", _configuration("SameIdentity"), marker=b"z")
    _archive(incoming / "a-first.zip", _configuration("SameIdentity"), marker=b"a")
    calls = 0
    real_probe = module.probe_export

    def counted_probe(tree):
        nonlocal calls
        calls += 1
        return real_probe(tree)

    monkeypatch.setattr(module, "probe_export", counted_probe)
    lifecycle = IntakeLifecycle(
        CandidateCatalog(managed / "catalog"),
        browser,
        operations,
        incoming_root=incoming,
        directory_settle_seconds=0,
    )
    assert calls == 0

    snapshot = lifecycle.refresh()
    assert calls == 2
    assert [item.probe.origin_name for item in snapshot.candidates] == [
        "a-first.zip",
        "z-last.zip",
    ]
    assert snapshot.selected_candidate_id is None


def test_catalog_fail_closed_на_повреждении_и_symlink(tmp_path):
    CandidateCatalog = _symbol("CandidateCatalog")
    IntakeLifecycle = _symbol("IntakeLifecycle")
    LifecycleError = _symbol("LifecycleError")

    managed, browser, _records, operations = _backend(tmp_path)
    incoming = tmp_path / "incoming"
    _archive(incoming / "candidate.zip", _configuration("CatalogSecurity"))
    catalog = CandidateCatalog(managed / "catalog")
    candidate = IntakeLifecycle(
        catalog,
        browser,
        operations,
        incoming_root=incoming,
        directory_settle_seconds=0,
    ).refresh().candidates[0]
    record = catalog.records_dir / f"{candidate.candidate_id}.json"
    record.write_text("not json", encoding="utf-8")

    with pytest.raises(LifecycleError, match="поврежд"):
        catalog.load(candidate.candidate_id)

    record.unlink()
    outside = tmp_path / "outside.json"
    outside.write_text("{}", encoding="utf-8")
    record.symlink_to(outside)
    with pytest.raises(LifecycleError, match="символичес"):
        CandidateCatalog(managed / "catalog")


def test_local_issue_не_раскрывает_настроенный_путь(tmp_path):
    CandidateCatalog = _symbol("CandidateCatalog")
    IntakeLifecycle = _symbol("IntakeLifecycle")

    managed, browser, _records, operations = _backend(tmp_path)
    snapshot = IntakeLifecycle(
        CandidateCatalog(managed / "catalog"),
        browser,
        operations,
        local_sources={"missing": tmp_path / "private" / "missing.zip"},
        directory_settle_seconds=0,
    ).refresh()

    assert snapshot.candidates == ()
    assert len(snapshot.issues) == 1
    assert snapshot.issues[0].source_id == "missing"
    assert str(tmp_path) not in snapshot.issues[0].message
