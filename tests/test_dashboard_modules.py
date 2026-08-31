"""Согласованность снимка источников и файловых обходов backend."""

from mcp1c import dashboard_backend as dashboard, tools


def test_единый_снимок_sources_после_remove_readd_не_смешивает_поколения(
    корень_кода,
    реестр_с_кодом,
    архив_кода,
    tmp_path,
    monkeypatch,
):
    from conftest import build_configuration, write_export

    old_source = реестр_с_кодом.sources["Пример"]
    old_source.warnings.append("старое поколение")
    module = корень_кода / "CommonModules" / "ОбщийПример" / "Ext" / "Module.bsl"
    module.write_text(
        module.read_text(encoding="utf-8")
        + "\nПроцедура НовоеПоколение() Экспорт\nКонецПроцедуры\n",
        encoding="utf-8",
    )
    archive = архив_кода(корень_кода)
    incoming = tmp_path / "incoming"
    incoming.mkdir()
    export = write_export(incoming, build_configuration(name="Пример"))
    real = tools._summarize_code
    changed = False

    def summarize(loaded):
        nonlocal changed
        if not changed:
            changed = True
            реестр_с_кодом.remove("Пример")
            new_source = реестр_с_кодом.add_configuration(export)
            new_source.warnings.append("новое поколение")
            реестр_с_кодом.add_modules(archive, configuration="Пример")
        return real(loaded)

    monkeypatch.setattr(tools, "_summarize_code", summarize)

    snapshot = tools.sources_snapshot(реестр_с_кодом)

    assert any("новое поколение" in row.warnings for row in snapshot.sources)
    assert all("старое поколение" not in row.warnings for row in snapshot.sources)
    total = len(реестр_с_кодом.resolve("Пример").modules.оглавление.имена)
    assert any(f"процедур {total}" in row.state for row in snapshot.code)


def test_prepare_sources_повторяет_дисковый_обход_после_remove_readd(
    корень_кода,
    реестр_с_кодом,
    архив_кода,
    tmp_path,
    monkeypatch,
):
    from conftest import build_configuration, write_export

    реестр_с_кодом.sources["Пример"].warnings.append("старое поколение")
    incoming = tmp_path / "incoming-readd"
    incoming.mkdir()
    export = write_export(
        incoming,
        build_configuration(name="Пример", version="2.0"),
    )
    archive = архив_кода(корень_кода)
    real = реестр_с_кодом.orphan_sources
    calls = 0

    def scan_and_replace():
        nonlocal calls
        calls += 1
        result = real()
        if calls == 1:
            реестр_с_кодом.remove("Пример")
            source = реестр_с_кодом.add_configuration(export)
            source.warnings.append("новое поколение")
            реестр_с_кодом.add_modules(archive, configuration="Пример")
        return result

    monkeypatch.setattr(реестр_с_кодом, "orphan_sources", scan_and_replace)

    prepared = dashboard._prepare_sources_page(
        реестр_с_кодом, authorized=False
    )

    assert calls == 2
    warnings = [warning for row in prepared.sources.sources for warning in row.warnings]
    assert "новое поколение" in warnings
    assert "старое поколение" not in warnings


def test_prepare_sources_после_двух_смен_возвращает_страницу_с_ошибкой(
    корень_кода, реестр_с_кодом, архив_кода, monkeypatch
):
    archive = архив_кода(корень_кода)
    real = реестр_с_кодом.orphan_sources
    calls = 0

    def scan_and_reparse():
        nonlocal calls
        calls += 1
        result = real()
        реестр_с_кодом.add_modules(archive, configuration="Пример")
        return result

    monkeypatch.setattr(реестр_с_кодом, "orphan_sources", scan_and_reparse)

    prepared = dashboard._prepare_sources_page(
        реестр_с_кодом, authorized=False
    )

    assert calls == 2
    assert "Источники изменились дважды" in prepared.sources_error
    assert prepared.sources.code == ()


def test_orphan_sources_сначала_снимает_пути_под_lock(tmp_path, monkeypatch):
    from mcp1c.registry import KIND_SYNTAX, Registry, Source

    registry = Registry(tmp_path / "data")
    first = registry.sources_dir / "first.hbk"
    second = registry.sources_dir / "second.hbk"
    first.parent.mkdir(parents=True)
    first.write_bytes(b"first")
    second.write_bytes(b"second")
    registry.sources["first"] = Source(
        id="first", kind=KIND_SYNTAX, stored_path="sources/first.hbk"
    )
    registry.sources["second"] = Source(
        id="second", kind=KIND_SYNTAX, stored_path="sources/second.hbk"
    )
    real = registry._absolute
    changed = False

    def absolute(path):
        nonlocal changed
        assert not registry._lock._is_owned()
        if not changed:
            changed = True
            with registry._lock:
                registry.sources.pop("second")
        return real(path)

    monkeypatch.setattr(registry, "_absolute", absolute)

    orphans = registry.orphan_sources()

    assert orphans == []


def test_orphan_sources_пропускает_исчезнувший_временный_файл(
    tmp_path, monkeypatch
):
    from pathlib import Path

    from mcp1c.registry import Registry

    registry = Registry(tmp_path / "data")
    temporary = registry.sources_dir / ".source.tmp"
    temporary.parent.mkdir(parents=True)
    temporary.write_bytes(b"temporary")
    real_is_file = Path.is_file

    def is_file_and_remove(path):
        result = real_is_file(path)
        if path == temporary and result:
            path.unlink()
        return result

    monkeypatch.setattr(Path, "is_file", is_file_and_remove)

    assert registry.orphan_sources() == []
