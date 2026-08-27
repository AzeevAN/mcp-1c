"""Состояние индекса кода на человеческом пути страницы «Источники»."""

import threading
import time

import pytest
from starlette.applications import Starlette
from starlette.responses import PlainTextResponse
from starlette.routing import Route

from conftest import живой_клиент
from mcp1c import dashboard, tools
from mcp1c.registry import STATUS_ERROR, STATUS_LOADING


def _client(registry):
    async def health(_request):
        return PlainTextResponse("ok")

    return живой_клиент(
        Starlette(routes=dashboard.routes(registry) + [Route("/health", health)])
    )


def test_человек_переходит_из_навигации_и_видит_счётчики_готового_индекса(
    реестр_с_кодом,
):
    client = _client(реестр_с_кодом)
    overview = client.get("/")

    assert "href=/sources" in overview.text
    response = client.get("/sources")

    loaded = реестр_с_кодом.resolve("Пример").modules
    assert response.status_code == 200
    assert "Индексы кода" in response.text
    assert "Основная конфигурация" in response.text
    assert f"модулей {len(loaded.оглавление.модули)}" in response.text
    assert f"процедур {len(loaded.оглавление.имена)}" in response.text
    assert f"форм {len(loaded.формы.модули)}" in response.text


def test_основной_код_и_расширение_показаны_отдельными_строками(
    корень_кода, реестр_из_кода
):
    registry = реестр_из_кода(корень_кода, extension="Доп")

    response = _client(registry).get("/sources")
    loaded = registry.resolve("Пример", extension="Доп").extension

    assert "Основная конфигурация" in response.text
    assert "не загружен" in response.text
    assert "Расширение Доп" in response.text
    assert "готов" in response.text
    assert f"процедур {len(loaded.оглавление.имена)}" in response.text


def test_строящийся_индекс_показывает_этап_и_фактический_прогресс(
    реестр_с_кодом,
):
    loaded = реестр_с_кодом.resolve("Пример").modules
    with реестр_с_кодом._lock:
        loaded.готов = False
        loaded.source.status = STATUS_LOADING
        loaded.этап = (2, 4)
        loaded.название_этапа = "вызовы"
        loaded.прогресс = (3, 11)

    response = _client(реестр_с_кодом).get("/sources")

    assert "строится: этап 2/4 «вызовы»" in response.text
    assert "обработано 3 из 11 элементов этапа" in response.text
    assert "процедур" not in response.text


def test_ошибка_индекса_обезличена_и_не_раскрывает_локальный_путь(
    реестр_с_кодом,
):
    loaded = реестр_с_кодом.resolve("Пример").modules
    with реестр_с_кодом._lock:
        loaded.готов = False
        loaded.source.status = STATUS_ERROR
        loaded.source.error = "Permission denied: /private/secret/Module.bsl"

    response = _client(реестр_с_кодом).get("/sources")

    assert "ошибка" in response.text
    assert "подробности ошибки доступны в журнале сервера" in response.text
    assert "/private/" not in response.text
    assert "Permission denied" not in response.text


def test_файловый_обход_sources_не_блокирует_event_loop(
    реестр_с_кодом, monkeypatch
):
    entered = threading.Event()
    release = threading.Event()
    real = реестр_с_кодом.orphan_sources

    def blocked_orphans():
        entered.set()
        release.wait(timeout=2)
        return real()

    monkeypatch.setattr(реестр_с_кодом, "orphan_sources", blocked_orphans)
    client = _client(реестр_с_кодом)
    responses = []
    request = threading.Thread(
        target=lambda: responses.append(client.get("/sources")), daemon=True
    )
    request.start()
    try:
        assert entered.wait(timeout=1)
        started = time.monotonic()
        health = client.get("/health")
        elapsed = time.monotonic() - started
    finally:
        release.set()
        request.join(timeout=2)

    assert health.status_code == 200
    assert elapsed < 0.5
    assert responses and responses[0].status_code == 200


def test_сканирование_incoming_не_блокирует_event_loop(
    реестр_с_кодом, monkeypatch
):
    monkeypatch.setenv("ADMIN_TOKEN", "secret")
    monkeypatch.delenv("API_TOKEN", raising=False)
    client = _client(реестр_с_кодом)
    assert client.post("/login", data={"token": "secret"}).status_code == 200
    scanner = dashboard._scanner(реестр_с_кодом)
    real = scanner.scan
    entered = threading.Event()
    release = threading.Event()

    def blocked_scan():
        entered.set()
        release.wait(timeout=2)
        return real()

    monkeypatch.setattr(scanner, "scan", blocked_scan)
    responses = []
    request = threading.Thread(
        target=lambda: responses.append(client.get("/sources")), daemon=True
    )
    request.start()
    try:
        assert entered.wait(timeout=1)
        started = time.monotonic()
        health = client.get("/health")
        elapsed = time.monotonic() - started
    finally:
        release.set()
        request.join(timeout=2)

    assert health.status_code == 200
    assert elapsed < 0.5
    assert responses and responses[0].status_code == 200


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


def test_renderer_sources_не_читает_живой_registry_или_файловую_систему(
    реестр_с_кодом, monkeypatch
):
    page_data = dashboard._prepare_sources_page(
        реестр_с_кодом, authorized=False
    )
    реестр_с_кодом.remove("Пример")
    monkeypatch.setattr(
        реестр_с_кодом,
        "orphan_sources",
        lambda: pytest.fail("renderer повторно обошёл файловую систему"),
    )

    response = dashboard._sources_page(page_data)
    html = response.body.decode()

    assert "Пример:modules" in html
    assert "готов — модулей" in html


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

    response = _client(реестр_с_кодом).get("/sources")

    assert response.status_code == 200
    assert calls == 2
    assert "новое поколение" in response.text
    assert "старое поколение" not in response.text


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

    response = _client(реестр_с_кодом).get("/sources")

    assert response.status_code == 200
    assert calls == 2
    assert "Источники изменились дважды" in response.text
    assert "готов — модулей" not in response.text


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


def test_reparse_во_время_сводки_не_смешивает_старые_и_новые_счётчики(
    корень_кода,
    реестр_с_кодом,
    архив_кода,
    monkeypatch,
):
    module = корень_кода / "CommonModules" / "ОбщийПример" / "Ext" / "Module.bsl"
    module.write_text(
        module.read_text(encoding="utf-8")
        + "\nПроцедура НоваяПослеПерезагрузки() Экспорт\nКонецПроцедуры\n",
        encoding="utf-8",
    )
    новый = архив_кода(корень_кода)
    old_total = len(реестр_с_кодом.resolve("Пример").modules.оглавление.имена)
    real = dashboard.tools._summarize_code
    changed = False

    def summarize(loaded):
        nonlocal changed
        if not changed:
            changed = True
            реестр_с_кодом.add_modules(новый, configuration="Пример")
        return real(loaded)

    monkeypatch.setattr(dashboard.tools, "_summarize_code", summarize)

    response = _client(реестр_с_кодом).get("/sources")

    new_total = len(реестр_с_кодом.resolve("Пример").modules.оглавление.имена)
    assert new_total == old_total + 1
    assert f"процедур {new_total}" in response.text
    assert f"процедур {old_total}," not in response.text
