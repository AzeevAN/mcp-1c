"""Код прежней версии в реестре — второе измерение `ResolvedContext`.

Приём кода отдельно от структуры (`add_modules`) удалён: код приходит только
native-поколением. Строки `<Имя>:modules`/`<Имя>:ext:*` и каталоги
`data/modules`/`data/extensions`, которые оставила прежняя версия, `restore()`
ещё поднимает в окне совместимости: из кэша, если штамп сошёлся, иначе строит
индекс фоном прямо по коду на диске. Восстановленная пара старой Source A и
отдельного кода доступна только диагностике: публичный `resolve()` требует
перезагрузки. Здесь проверяются именно эта фоновая сборка, каскад `remove` и
устойчивость restore к гонкам.

Данные прежней версии собираются `native_export.записать_старый_код`: строка
registry.json и каталог кода такие же, какие писал удалённый приём.

`рабочий` — отдельный от `корень_кода` временный каталог (`tmp_path_factory`,
не `tmp_path`): `корень_кода` — фикстура conftest.py, она пишет дерево модулей
прямо в свой `tmp_path`.
"""
from __future__ import annotations

import json
import threading
import time
from pathlib import Path

import pytest
from starlette.applications import Starlette

from conftest import build_configuration, живой_клиент, write_export
from mcp1c import coverage_log, index_cache, modules_index, search
from mcp1c.registry import (
    KIND_MODULES,
    STATUS_ERROR,
    Registry,
    RegistryError,
)
from mcp1c.server import build_server
from native_export import записать_старый_код, опубликовать_код


def _старый_код(
    реестр: Registry,
    корень: Path,
    *,
    extension: bool = False,
    version: str = "",
):
    """Строка кода прежней версии (или её расширения «Доп») в registry.json."""
    if not реестр.registry_path.exists():
        реестр.save()
    return записать_старый_код(
        реестр,
        корень,
        configuration="Пример",
        extension="Доп" if extension else None,
        version=version,
        origin="extension.zip" if extension else "modules.zip",
    )


def _поднятый(реестр: Registry) -> Registry:
    """Перезапуск над data/ прежней версии с дождавшейся фоновой сборкой."""
    заново = Registry(реестр.data_dir)
    assert заново.startup() == []
    assert заново.wait_for_module_builds(timeout=10)
    return заново


def _реестр_с_конфигурацией(
    рабочий: Path, *, name: str = "Пример", version: str = "1.0"
) -> Registry:
    входящее = рабочий / "in"
    входящее.mkdir(parents=True, exist_ok=True)
    реестр = Registry(рабочий / "data")
    реестр.add_configuration(
        write_export(входящее, build_configuration(name=name, version=version))
    )
    return реестр


def _снести_кэш_модулей(реестр: Registry, source_id: str = "Пример:modules") -> None:
    """Оставляет код и запись реестра, но создаёт честный холодный старт."""
    for вид in реестр.CACHE_KINDS[KIND_MODULES]:
        реестр._cache_path(source_id, вид).unlink(missing_ok=True)


def _дождаться(условие, *, timeout: float = 3.0) -> None:
    конец = time.monotonic() + timeout
    while not условие():
        if time.monotonic() >= конец:
            pytest.fail("фоновая сборка не завершилась вовремя")
        time.sleep(0.01)


def _диагностический_контекст(реестр: Registry, *, extension: str | None = None):
    """Восстановленный legacy-код виден диагностике, но закрыт для MCP."""
    with pytest.raises(RegistryError, match="reload_required"):
        реестр.resolve("Пример", extension=extension)
    return реестр.resolve("Пример", extension=extension, diagnostic=True)


def _вызвать_с_ошибкой(ошибки: list[Exception], функция, *args, **kwargs) -> None:
    try:
        функция(*args, **kwargs)
    except Exception as error:
        ошибки.append(error)


def test_модули_попадают_в_контекст(tmp_path_factory, корень_кода):
    рабочий = tmp_path_factory.mktemp("реестр")
    реестр = Registry(рабочий / "data")
    опубликовать_код(
        реестр,
        корень_кода,
        configuration=build_configuration(name="Пример"),
        рабочий=рабочий,
    )

    контекст = реестр.resolve("Пример")

    assert контекст.modules is not None
    assert контекст.modules.оглавление.по_имени("сложить")


def test_снятие_конфигурации_снимает_код(tmp_path_factory, корень_кода):
    рабочий = tmp_path_factory.mktemp("реестр")
    реестр = _реестр_с_конфигурацией(рабочий)
    _старый_код(реестр, корень_кода)
    реестр = _поднятый(реестр)
    корень = реестр._modules_root("Пример")
    assert корень.exists()

    реестр.remove("Пример")

    assert "Пример:modules" not in реестр.sources
    assert not корень.exists()
    # Индекс в памяти тоже не должен пережить конфигурацию — иначе он висит
    # без владельца и `resolve()` на пустом реестре его уже не вернёт.
    assert "Пример:modules" not in реестр.modules


def test_снятие_конфигурации_снимает_код_расширения(tmp_path_factory, корень_кода):
    """Тот же каскад, что и для модулей конфигурации — на `<Имя>:ext:*`."""
    рабочий = tmp_path_factory.mktemp("реестр")
    реестр = _реестр_с_конфигурацией(рабочий)
    _старый_код(реестр, корень_кода, extension=True)
    реестр = _поднятый(реестр)
    корень_расширения = реестр._extension_root("Пример", "Доп")
    корень_конфигурации = корень_расширения.parent
    assert корень_расширения.exists()
    assert корень_конфигурации.exists()
    assert "Пример:ext:Доп" in реестр.sources

    реестр.remove("Пример")

    assert "Пример:ext:Доп" not in реестр.sources
    assert not корень_расширения.exists()
    assert not корень_конфигурации.exists()


def test_рестарт_поднимает_индекс_из_кэша(tmp_path_factory, корень_кода):
    """`restore()` поднимает индекс из кэша, а не молчит про него.

    Мутация, которая уберёт вызов `поднять_индексы`/сборку из `restore()`
    и оставит только `KIND_MODULES` в `self.sources`, роняет этот тест:
    диагностический `контекст.modules` останется `None`.
    """
    рабочий = tmp_path_factory.mktemp("реестр")
    реестр = _реестр_с_конфигурацией(рабочий)
    _старый_код(реестр, корень_кода, version="1.0.0")
    # Первый запуск строит индекс фоном и кладёт его в кэш.
    _поднятый(реестр)

    заново = Registry(реестр.data_dir)
    problems = заново.restore()

    assert not problems
    контекст = _диагностический_контекст(заново)
    assert контекст.modules is not None
    assert контекст.modules.оглавление.по_имени("сложить")
    assert контекст.modules.версия_кода == "1.0.0"
    assert контекст.modules.готов is True
    assert контекст.modules.прогресс[0] == контекст.modules.прогресс[1]


def test_старт_не_ждёт_холодной_сборки(
    tmp_path_factory, корень_кода, monkeypatch
):
    """Промах кэша публикует состояние и отдаёт управление серверу сразу."""
    рабочий = tmp_path_factory.mktemp("реестр")
    реестр = _реестр_с_конфигурацией(рабочий)
    _старый_код(реестр, корень_кода)
    _снести_кэш_модулей(реестр)

    строит = threading.Event()
    отпустить = threading.Event()
    исходная = modules_index.Оглавление.построить

    def тормозит(корень, **kwargs):
        строит.set()
        отпустить.wait(timeout=2)
        return исходная(корень, **kwargs)

    monkeypatch.setattr(modules_index.Оглавление, "построить", staticmethod(тормозит))
    заново = Registry(реестр.data_dir)
    начало = time.monotonic()
    try:
        problems = заново.startup()
        прошло = time.monotonic() - начало

        assert not problems
        assert прошло < 1.0
        assert строит.wait(timeout=1)
        модули = _диагностический_контекст(заново).modules
        assert модули is not None
        assert модули.готов is False
        assert модули.прогресс[0] < модули.прогресс[1]
    finally:
        отпустить.set()

    _дождаться(lambda: _диагностический_контекст(заново).modules.готов)
    готовые = _диагностический_контекст(заново).modules
    assert готовые.оглавление.по_имени("сложить")
    assert готовые.прогресс[0] == готовые.прогресс[1]


def test_четыре_индекса_появляются_только_готовым_комплектом(
    tmp_path_factory, корень_кода, monkeypatch
):
    """Даже на последней фазе наружу не торчат три готовых индекса из четырёх."""
    рабочий = tmp_path_factory.mktemp("реестр")
    реестр = _реестр_с_конфигурацией(рабочий)
    _старый_код(реестр, корень_кода)
    _снести_кэш_модулей(реестр)

    строит_поиск = threading.Event()
    отпустить = threading.Event()
    исходная = modules_index.построить_поиск

    def тормозит(оглавление, корень, **kwargs):
        строит_поиск.set()
        отпустить.wait(timeout=2)
        return исходная(оглавление, корень, **kwargs)

    monkeypatch.setattr(modules_index, "построить_поиск", тормозит)
    заново = Registry(реестр.data_dir)
    try:
        assert not заново.startup()
        assert строит_поиск.wait(timeout=1)
        модули = _диагностический_контекст(заново).modules
        assert модули is not None
        assert модули.готов is False
        assert (модули.оглавление, модули.вызовы, модули.формы, модули.поиск) == (
            None,
            None,
            None,
            None,
        )
    finally:
        отпустить.set()

    _дождаться(lambda: _диагностический_контекст(заново).modules.готов)
    готовые = _диагностический_контекст(заново).modules
    assert all(
        индекс is not None
        for индекс in (
            готовые.оглавление,
            готовые.вызовы,
            готовые.формы,
            готовые.поиск,
        )
    )


def test_прогресс_показывает_фактический_проход_текущего_этапа(
    tmp_path_factory, корень_кода, monkeypatch
):
    """N/M — реально обработанные файлы, отдельно от номера этапа X/4."""
    рабочий = tmp_path_factory.mktemp("реестр")
    реестр = _реестр_с_конфигурацией(рабочий)
    _старый_код(реестр, корень_кода)
    _снести_кэш_модулей(реестр)

    второй_файл = threading.Event()
    отпустить = threading.Event()
    прочитано = 0
    настоящее_чтение = modules_index.read_bsl

    def прочитать(корень, адрес, локатор):
        nonlocal прочитано
        прочитано += 1
        if прочитано == 2:
            второй_файл.set()
            отпустить.wait(timeout=2)
        return настоящее_чтение(корень, адрес, локатор)

    monkeypatch.setattr(modules_index, "read_bsl", прочитать)
    заново = Registry(реестр.data_dir)
    try:
        assert not заново.startup()
        assert второй_файл.wait(timeout=1)
        модули = _диагностический_контекст(заново).modules
        assert модули.готов is False
        assert модули.этап == (1, 4)
        assert модули.название_этапа == "оглавление"
        assert модули.прогресс == (1, 3)
    finally:
        отпустить.set()

    _дождаться(lambda: _диагностический_контекст(заново).modules.готов)


@pytest.mark.parametrize(
    "compiled_name",
    ["CommonModules/Закрытый.Module", "CommonModule.Плоский.Module"],
)
def test_холодный_старт_считает_скомпилированный_модуль_в_первом_этапе(
    tmp_path, monkeypatch, compiled_name
):
    рабочий = tmp_path / "cold-compiled"
    корень = рабочий / "code"
    корень.mkdir(parents=True)
    compiled = корень / compiled_name
    compiled.parent.mkdir(parents=True, exist_ok=True)
    compiled.write_bytes(b"compiled")
    реестр = _реестр_с_конфигурацией(рабочий)
    _старый_код(реестр, корень)
    # Холодный builder обязан считать фактический canonical root. Добавляем
    # исходный модуль уже туда: этот regression проверяет именно mixed root,
    # оставшийся после обновления.
    обычный = реестр.modules_dir / "Пример/CommonModules/Открытый/Ext/Module.bsl"
    обычный.parent.mkdir(parents=True)
    обычный.write_text("Процедура А() Экспорт\nКонецПроцедуры\n", encoding="utf-8")

    started = threading.Event()
    release = threading.Event()
    original = modules_index.Оглавление.построить

    def blocked(root, *, каталог=None, прогресс=None):
        started.set()
        release.wait(timeout=2)
        return original(root, каталог=каталог, прогресс=прогресс)

    monkeypatch.setattr(modules_index.Оглавление, "построить", staticmethod(blocked))
    fresh = Registry(реестр.data_dir)
    try:
        assert not fresh.startup()
        assert started.wait(timeout=1)
        loaded = _диагностический_контекст(fresh).modules
        assert loaded.этап == (1, 4)
        assert loaded.прогресс == (0, 2)
    finally:
        release.set()

    _дождаться(lambda: _диагностический_контекст(fresh).modules.готов)


def test_без_выгрузки_кода_состояния_модулей_нет(tmp_path_factory):
    рабочий = tmp_path_factory.mktemp("реестр")
    реестр = _реестр_с_конфигурацией(рабочий)

    assert реестр.resolve("Пример").modules is None


def test_отказ_фоновой_сборки_не_остаётся_вечным_строится(
    tmp_path_factory, корень_кода, monkeypatch, caplog
):
    рабочий = tmp_path_factory.mktemp("реестр")
    реестр = _реестр_с_конфигурацией(рабочий)
    _старый_код(реестр, корень_кода)
    _снести_кэш_модулей(реестр)

    def падает(*_, **__):
        raise MemoryError("не влезло")

    monkeypatch.setattr(modules_index.Оглавление, "построить", staticmethod(падает))
    заново = Registry(реестр.data_dir)
    with caplog.at_level("ERROR", logger="mcp1c.registry"):
        assert not заново.startup()

    _дождаться(lambda: заново.sources["Пример:modules"].status == STATUS_ERROR)
    источник = заново.sources["Пример:modules"]
    assert "не влезло" in источник.error
    assert _диагностический_контекст(заново).modules.готов is False
    assert "Фоновая сборка индекса кода" in caplog.text
    assert "MemoryError: не влезло" in caplog.text


def test_параллельные_cold_miss_восстанавливают_все_корпуса(
    tmp_path_factory,
    корень_кода,
    monkeypatch,
):
    рабочий = tmp_path_factory.mktemp("параллельный-cold")
    реестр = _реестр_с_конфигурацией(рабочий)
    источники = (
        _старый_код(реестр, корень_кода),
        _старый_код(реестр, корень_кода, extension=True),
    )

    class ГоночныйОбщийСтеммер:
        def __init__(self):
            self.barrier = threading.Barrier(2)
            self.lock = threading.Lock()
            self.calls = 0
            self.owner = 0

        def stemWord(self, token: str) -> str:
            with self.lock:
                if self.calls >= 2:
                    return token
                self.calls += 1
                owner = threading.get_ident()
                self.owner = owner
            self.barrier.wait(timeout=3)
            if self.owner != owner:
                raise IndexError("string index out of range")
            return token

    class БезопасныйСтеммер:
        def stemWord(self, token: str) -> str:
            return token

    monkeypatch.setattr(
        search,
        "_STEMMER",
        ГоночныйОбщийСтеммер(),
        raising=False,
    )
    monkeypatch.setattr(
        search,
        "_STEMMER_FACTORY",
        БезопасныйСтеммер,
        raising=False,
    )
    monkeypatch.setattr(search, "_STEMMER_LOCAL", threading.local(), raising=False)
    with search._STEM_CACHE_LOCK:
        search._STEM_CACHE.clear()

    заново = Registry(реестр.data_dir)
    assert заново.startup() == []
    ids = tuple(источник.id for источник in источники)
    _дождаться(
        lambda: all(
            заново.sources[source_id].status in {"ready", "error"}
            for source_id in ids
        )
    )

    assert [заново.sources[source_id].status for source_id in ids] == [
        "ready",
        "ready",
    ]
    assert all(заново.modules[source_id].готов for source_id in ids)
    assert all(
        coverage_log.load_current(заново.data_dir, заново.sources[source_id])
        is not None
        for source_id in ids
    )
    for source_id in ids:
        source = заново.sources[source_id]
        for вид in заново.CACHE_KINDS[source.kind]:
            header = json.loads(
                заново._cache_path(source_id, вид)
                .read_bytes()
                .split(b"\n", 1)[0]
            )
            assert header["code"] == index_cache._code_digest()


def test_повторный_startup_не_запускает_вторую_сборку(
    tmp_path_factory, корень_кода, monkeypatch
):
    рабочий = tmp_path_factory.mktemp("реестр")
    реестр = _реестр_с_конфигурацией(рабочий)
    _старый_код(реестр, корень_кода)
    _снести_кэш_модулей(реестр)

    начата = threading.Event()
    отпустить = threading.Event()
    исходная = modules_index.Оглавление.построить
    вызовов = 0

    def тормозит(корень, **kwargs):
        nonlocal вызовов
        вызовов += 1
        начата.set()
        отпустить.wait(timeout=2)
        return исходная(корень, **kwargs)

    monkeypatch.setattr(modules_index.Оглавление, "построить", staticmethod(тормозит))
    заново = Registry(реестр.data_dir)
    try:
        assert not заново.startup()
        assert начата.wait(timeout=1)
        assert not заново.startup()
        assert вызовов == 1
    finally:
        отпустить.set()

    _дождаться(lambda: _диагностический_контекст(заново).modules.готов)


def test_снятие_источника_во_время_сборки_не_воскрешает_индекс(
    tmp_path_factory, корень_кода, monkeypatch
):
    рабочий = tmp_path_factory.mktemp("реестр")
    реестр = _реестр_с_конфигурацией(рабочий)
    _старый_код(реестр, корень_кода)
    _снести_кэш_модулей(реестр)

    начата = threading.Event()
    отпустить = threading.Event()
    исходная = modules_index.Оглавление.построить

    def тормозит(корень, **kwargs):
        начата.set()
        отпустить.wait(timeout=2)
        return исходная(корень, **kwargs)

    monkeypatch.setattr(modules_index.Оглавление, "построить", staticmethod(тормозит))
    заново = Registry(реестр.data_dir)
    try:
        assert not заново.startup()
        assert начата.wait(timeout=1)
        заново.remove("Пример")
    finally:
        отпустить.set()

    _дождаться(lambda: "Пример:modules" not in заново._module_builds)
    assert "Пример:modules" not in заново.sources
    assert "Пример:modules" not in заново.modules
    assert not заново._modules_root("Пример").exists()


def test_admin_reload_не_блокирует_health(tmp_path, monkeypatch):
    """`run_in_threadpool` оставляет event loop доступным во время reload."""
    реестр = Registry(tmp_path / "data")
    начат = threading.Event()
    отпустить = threading.Event()

    def медленный_старт():
        начат.set()
        отпустить.wait(timeout=2)
        return []

    monkeypatch.setattr(реестр, "startup", медленный_старт)
    monkeypatch.setenv("ADMIN_TOKEN", "secret")
    server = build_server(реестр)
    client = живой_клиент(Starlette(routes=server._custom_starlette_routes[:2]))
    ответы = []
    поток = threading.Thread(
        target=lambda: ответы.append(
            client.post("/admin/reload", headers={"x-admin-token": "secret"})
        )
    )
    поток.start()
    try:
        assert начат.wait(timeout=1)
        начало = time.monotonic()
        ответ_health = client.get("/health")
        assert time.monotonic() - начало < 0.5
        assert ответ_health.status_code == 200
    finally:
        отпустить.set()
        поток.join(timeout=2)

    assert not поток.is_alive()
    assert ответы[0].status_code == 200


def test_два_параллельных_startup_запускают_одну_сборку(
    tmp_path_factory, корень_кода, monkeypatch
):
    """Проверка и резервирование source_id обязаны быть одной операцией."""
    рабочий = tmp_path_factory.mktemp("реестр")
    реестр = _реестр_с_конфигурацией(рабочий)
    _старый_код(реестр, корень_кода)
    _снести_кэш_модулей(реестр)

    первый_load = threading.Event()
    отпустить_load = threading.Event()
    первая_сборка = threading.Event()
    отпустить_сборку = threading.Event()
    load_calls = 0
    build_calls = 0
    настоящий_load = modules_index.поднять_индексы
    настоящее_построить = modules_index.Оглавление.построить

    def загрузить(*args, **kwargs):
        nonlocal load_calls
        load_calls += 1
        if load_calls == 1:
            первый_load.set()
            отпустить_load.wait(timeout=3)
        return настоящий_load(*args, **kwargs)

    def построить(корень, **kwargs):
        nonlocal build_calls
        build_calls += 1
        первая_сборка.set()
        отпустить_сборку.wait(timeout=3)
        return настоящее_построить(корень, **kwargs)

    monkeypatch.setattr(modules_index, "поднять_индексы", загрузить)
    monkeypatch.setattr(
        modules_index.Оглавление, "построить", staticmethod(построить)
    )
    заново = Registry(реестр.data_dir)
    ошибки: list[Exception] = []
    второй_начал = threading.Event()

    def стартовать(начал: threading.Event | None = None):
        if начал is not None:
            начал.set()
        try:
            заново.startup()
        except Exception as error:
            ошибки.append(error)

    первый = threading.Thread(target=стартовать)
    второй = threading.Thread(target=стартовать, args=(второй_начал,))
    первый.start()
    assert первый_load.wait(timeout=1)
    второй.start()
    try:
        assert второй_начал.wait(timeout=1)
        отпустить_load.set()
        assert первая_сборка.wait(timeout=1)
        первый.join(timeout=1)
        второй.join(timeout=1)
        assert build_calls == 1
        assert not ошибки
    finally:
        отпустить_load.set()
        отпустить_сборку.set()
        первый.join(timeout=3)
        второй.join(timeout=3)

    _дождаться(lambda: _диагностический_контекст(заново).modules.готов)
    assert build_calls == 1


@pytest.mark.parametrize("первый_исход", ["ready", "error"])
def test_фон_не_сохраняет_частичный_registry_во_время_startup(
    tmp_path_factory, корень_кода, monkeypatch, первый_исход
):
    """Фоновый save ждёт полного restore, включая следующий code-source."""
    рабочий = tmp_path_factory.mktemp("реестр")
    исходный = _реестр_с_конфигурацией(рабочий)
    _старый_код(исходный, корень_кода)
    _старый_код(исходный, корень_кода, extension=True)
    ожидаемые_id = {"Пример", "Пример:modules", "Пример:ext:Доп"}

    заново = Registry(исходный.data_dir)
    первая_строка_кода = threading.Event()
    отпустить_restore = threading.Event()
    первая_сборка_закончена = threading.Event()
    фоновый_save_закончен = threading.Event()
    второй_startup_начат = threading.Event()
    второй_startup_закончен = threading.Event()
    настоящая_загрузка_кода = заново._поднять_или_построить_модули
    настоящая_сборка = заново._построить_индекс_кода
    настоящий_save = заново.save
    строк_кода = 0
    фоновые_save: list[str] = []

    def загрузить_код(*args, **kwargs):
        nonlocal строк_кода
        настоящая_загрузка_кода(*args, **kwargs)
        строк_кода += 1
        if строк_кода == 1:
            первая_строка_кода.set()
            отпустить_restore.wait(timeout=3)

    def собрать(
        метка, корень, identity=None, прогресс=None, файлы_каталога=None
    ):
        try:
            if метка == "modules.zip" and первый_исход == "error":
                raise MemoryError("не влезло")
            return настоящая_сборка(
                метка, корень, identity, прогресс, файлы_каталога
            )
        finally:
            if метка == "modules.zip":
                первая_сборка_закончена.set()

    def сохранить():
        настоящий_save()
        if threading.current_thread().name.startswith("modules:"):
            фоновые_save.append(threading.current_thread().name)
            фоновый_save_закончен.set()

    monkeypatch.setattr(
        заново, "_поднять_или_построить_модули", загрузить_код
    )
    monkeypatch.setattr(заново, "_построить_индекс_кода", собрать)
    monkeypatch.setattr(заново, "save", сохранить)
    ошибки: list[Exception] = []
    первый = threading.Thread(
        target=lambda: _вызвать_с_ошибкой(ошибки, заново.startup)
    )

    def второй_старт():
        второй_startup_начат.set()
        _вызвать_с_ошибкой(ошибки, заново.startup)
        второй_startup_закончен.set()

    второй = threading.Thread(target=второй_старт)
    первый.start()
    assert первая_строка_кода.wait(timeout=1)
    второй.start()
    try:
        assert второй_startup_начат.wait(timeout=1)
        assert not второй_startup_закончен.wait(timeout=0.1)
        assert первая_сборка_закончена.wait(timeout=1)
        сохранил_во_время_startup = фоновый_save_закончен.wait(timeout=0.3)
        снимок = json.loads(
            заново.registry_path.read_text(encoding="utf-8")
        )
        id_на_диске = {row["id"] for row in снимок["sources"]}
        assert (сохранил_во_время_startup, id_на_диске) == (
            False,
            ожидаемые_id,
        )
    finally:
        отпустить_restore.set()
        первый.join(timeout=3)
        второй.join(timeout=3)

    assert not первый.is_alive()
    assert not второй.is_alive()
    assert not ошибки
    _дождаться(lambda: len(фоновые_save) >= 2)
    финал = json.loads(заново.registry_path.read_text(encoding="utf-8"))
    строки = {row["id"]: row for row in финал["sources"]}
    assert set(строки) == ожидаемые_id
    assert строки["Пример:modules"]["status"] == первый_исход
    assert строки["Пример:ext:Доп"]["status"] == "ready"


def test_ошибка_startup_освобождает_фоновый_save(
    tmp_path_factory, корень_кода, monkeypatch
):
    """После partial restore фон выходит, но не заменяет полный снимок."""
    рабочий = tmp_path_factory.mktemp("реестр")
    исходный = _реестр_с_конфигурацией(рабочий)
    _старый_код(исходный, корень_кода)
    _старый_код(исходный, корень_кода, extension=True)
    ожидаемые_id = {"Пример", "Пример:modules", "Пример:ext:Доп"}

    заново = Registry(исходный.data_dir)
    сборка_начата = threading.Event()
    отпустить_сборку = threading.Event()
    фоновый_save_закончен = threading.Event()
    фоновая_финализация = threading.Event()
    результаты_финализации: list[bool] = []
    первая_строка_кода = 0
    настоящая_сборка = заново._построить_индекс_кода
    настоящий_save = заново.save
    настоящая_загрузка_кода = заново._поднять_или_построить_модули
    настоящая_финализация = заново._сохранить_результат_фоновой_сборки

    def собрать(*args, **kwargs):
        сборка_начата.set()
        отпустить_сборку.wait(timeout=3)
        return настоящая_сборка(*args, **kwargs)

    def загрузить_код(*args, **kwargs):
        nonlocal первая_строка_кода
        настоящая_загрузка_кода(*args, **kwargs)
        первая_строка_кода += 1
        if первая_строка_кода == 1:
            raise KeyboardInterrupt("авария после первой строки кода")

    def сохранить():
        настоящий_save()
        if threading.current_thread().name.startswith("modules:"):
            фоновый_save_закончен.set()

    def финализировать():
        результат = настоящая_финализация()
        результаты_финализации.append(результат)
        фоновая_финализация.set()
        return результат

    monkeypatch.setattr(заново, "_построить_индекс_кода", собрать)
    monkeypatch.setattr(
        заново, "_поднять_или_построить_модули", загрузить_код
    )
    monkeypatch.setattr(заново, "save", сохранить)
    monkeypatch.setattr(
        заново, "_сохранить_результат_фоновой_сборки", финализировать
    )
    try:
        with pytest.raises(KeyboardInterrupt, match="авария после первой"):
            заново.startup()
        assert сборка_начата.wait(timeout=1)
        assert not фоновый_save_закончен.is_set()
    finally:
        отпустить_сборку.set()

    assert фоновая_финализация.wait(timeout=2)
    assert результаты_финализации == [False]
    assert not фоновый_save_закончен.is_set()
    снимок = json.loads(заново.registry_path.read_text(encoding="utf-8"))
    строки = {row["id"]: row for row in снимок["sources"]}
    assert set(строки) == ожидаемые_id
    assert all(row["status"] == "ready" for row in строки.values())


def test_заблокированный_warm_load_не_воскрешает_remove(
    tmp_path_factory, корень_кода, monkeypatch
):
    рабочий = tmp_path_factory.mktemp("реестр")
    реестр = _реестр_с_конфигурацией(рабочий)
    _старый_код(реестр, корень_кода, version="1.0.0")

    кэш_прочитан = threading.Event()
    отпустить = threading.Event()
    настоящее_поднять = modules_index.поднять_индексы

    def поднять(*args, **kwargs):
        result = настоящее_поднять(*args, **kwargs)
        кэш_прочитан.set()
        отпустить.wait(timeout=3)
        return result

    monkeypatch.setattr(modules_index, "поднять_индексы", поднять)
    заново = Registry(реестр.data_dir)
    поток = threading.Thread(target=заново.startup)
    поток.start()
    try:
        assert кэш_прочитан.wait(timeout=1)
        заново.remove("Пример")
    finally:
        отпустить.set()
        поток.join(timeout=3)

    assert not поток.is_alive()
    assert "Пример" not in заново.sources
    assert "Пример:modules" not in заново.sources
    assert "Пример:modules" not in заново.modules


def test_remove_откатывает_detach_если_следующий_rename_не_удался(
    tmp_path_factory, корень_кода, monkeypatch
):
    """До изменения registry все уже detached roots можно вернуть."""
    рабочий = tmp_path_factory.mktemp("реестр")
    реестр = _реестр_с_конфигурацией(рабочий)
    _старый_код(реестр, корень_кода)
    _старый_код(реестр, корень_кода, extension=True)
    реестр = _поднятый(реестр)
    корень_модулей = реестр._modules_root("Пример")
    корень_расширения = реестр._extension_root("Пример", "Доп")
    настоящий_rename = Path.rename

    def уронить_второй_detach(self, target):
        target = Path(target)
        if self == корень_модулей and ".retired-" in target.name:
            raise OSError("нет прав на rename")
        return настоящий_rename(self, target)

    monkeypatch.setattr(Path, "rename", уронить_второй_detach)

    with pytest.raises(RegistryError, match="не снят"):
        реестр.remove("Пример")

    assert "Пример" in реестр.configurations
    assert "Пример:modules" in реестр.sources
    assert "Пример:ext:Доп" in реестр.sources
    assert корень_модулей.is_dir()
    assert корень_расширения.is_dir()
    assert not list(корень_модулей.parent.glob(".*.retired-*"))
    assert not list(корень_расширения.parent.glob(".*.retired-*"))


def test_remove_не_падает_если_расходный_кэш_не_удаляется(
    tmp_path_factory, корень_кода, monkeypatch
):
    """Read-only cache не оставляет registry между поколениями."""
    рабочий = tmp_path_factory.mktemp("реестр")
    реестр = _реестр_с_конфигурацией(рабочий)
    _старый_код(реестр, корень_кода)
    реестр = _поднятый(реестр)
    корень = реестр._modules_root("Пример")
    кэши_модулей = [
        реестр._cache_path("Пример:modules", вид)
        for вид in реестр.CACHE_KINDS[KIND_MODULES]
    ]
    assert all(путь.is_file() for путь in кэши_модулей)
    реестр.save()

    настоящий_unlink = Path.unlink

    def кэш_только_для_чтения(self, *args, **kwargs):
        if self.parent == реестр.cache_dir:
            raise OSError("кэш только для чтения")
        return настоящий_unlink(self, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", кэш_только_для_чтения)

    реестр.remove("Пример")

    assert not реестр.sources
    assert not реестр.configurations
    assert not реестр.modules
    assert not корень.exists()
    assert not list(корень.parent.glob(".*.retired-*"))
    # Файлы могут остаться на read-only томе, но они больше
    # ничем не заявлены и не могут вернуть Source после старта.
    assert all(путь.is_file() for путь in кэши_модулей)
    реестр.save()

    после_рестарта = Registry(реестр.data_dir)
    после_рестарта.startup()
    assert not после_рестарта.sources
    assert not после_рестарта.configurations
    assert not после_рестарта.modules
    assert not корень.exists()


@pytest.mark.parametrize("расширение", [False, True], ids=["modules", "extension"])
@pytest.mark.parametrize(
    "добавить_заново", [False, True], ids=["remove", "remove-readd"]
)
def test_restore_не_привязывает_старый_код_после_remove_владельца(
    tmp_path_factory,
    корень_кода,
    monkeypatch,
    расширение,
    добавить_заново,
):
    """Restore row кода принадлежит identity своей config row."""
    рабочий = tmp_path_factory.mktemp("реестр")
    исходный = _реестр_с_конфигурацией(рабочий)
    _старый_код(исходный, корень_кода, extension=расширение)
    source_id = "Пример:ext:Доп" if расширение else "Пример:modules"
    путь_конфигурации = исходный._absolute(
        исходный.sources["Пример"].stored_path
    )
    sha_конфигурации = исходный.sources["Пример"].sha256

    восстановленный = Registry(исходный.data_dir)
    конфигурация_опубликована = threading.Event()
    отпустить_restore = threading.Event()
    настоящий_add_configuration = восстановленный.add_configuration

    def опубликовать_и_задержать(*args, **kwargs):
        source = настоящий_add_configuration(*args, **kwargs)
        конфигурация_опубликована.set()
        отпустить_restore.wait(timeout=3)
        return source

    monkeypatch.setattr(
        восстановленный,
        "add_configuration",
        опубликовать_и_задержать,
    )
    ошибки: list[Exception] = []
    сообщения: list[str] = []

    def запустить():
        try:
            сообщения.extend(восстановленный.startup())
        except Exception as error:
            ошибки.append(error)

    поток = threading.Thread(target=запустить)
    поток.start()
    try:
        assert конфигурация_опубликована.wait(timeout=1)
        восстановленный.remove("Пример")
        новая_конфигурация = None
        if добавить_заново:
            новая_конфигурация = настоящий_add_configuration(
                путь_конфигурации,
                keep_source=False,
                known_sha256=sha_конфигурации,
            )
    finally:
        отпустить_restore.set()
        поток.join(timeout=3)

    assert not поток.is_alive()
    assert not ошибки
    assert source_id not in восстановленный.sources
    assert source_id not in восстановленный.modules
    assert any(source_id in сообщение for сообщение in сообщения)
    if добавить_заново:
        assert (
            восстановленный.configurations["Пример"].source
            is новая_конфигурация
        )
    else:
        assert "Пример" not in восстановленный.configurations

    после_рестарта = Registry(исходный.data_dir)
    после_рестарта.startup()
    assert source_id not in после_рестарта.sources
    assert source_id not in после_рестарта.modules


@pytest.mark.parametrize("расширение", [False, True], ids=["modules", "extension"])
@pytest.mark.parametrize(
    "сценарий", ["код-первым", "нет-владельца"], ids=["reversed", "missing"]
)
def test_restore_кода_не_зависит_от_порядка_строк_и_требует_владельца(
    tmp_path_factory, корень_кода, расширение, сценарий
):
    """Code rows отложены, а orphan row честно отклоняется."""
    рабочий = tmp_path_factory.mktemp("реестр")
    исходный = _реестр_с_конфигурацией(рабочий)
    _старый_код(исходный, корень_кода, extension=расширение)
    source_id = "Пример:ext:Доп" if расширение else "Пример:modules"
    payload = json.loads(исходный.registry_path.read_text(encoding="utf-8"))
    строка_кода = next(
        raw for raw in payload["sources"] if raw["id"] == source_id
    )
    строка_конфигурации = next(
        raw for raw in payload["sources"] if raw["id"] == "Пример"
    )
    payload["sources"] = (
        [строка_кода, строка_конфигурации]
        if сценарий == "код-первым"
        else [строка_кода]
    )
    исходный.registry_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8"
    )

    восстановленный = Registry(исходный.data_dir)
    сообщения = восстановленный.startup()

    if сценарий == "код-первым":
        assert восстановленный.wait_for_module_builds(timeout=10)
        assert "Пример" in восстановленный.configurations
        assert source_id in восстановленный.sources
        assert восстановленный.modules[source_id].готов is True
    else:
        assert source_id not in восстановленный.sources
        assert source_id not in восстановленный.modules
        assert any(source_id in сообщение for сообщение in сообщения)
        после_рестарта = Registry(исходный.data_dir)
        после_рестарта.startup()
        assert source_id not in после_рестарта.sources
        assert source_id not in после_рестарта.modules


