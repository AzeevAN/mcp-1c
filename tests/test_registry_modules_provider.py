"""Источник модулей в реестре — второе измерение `ResolvedContext`.

Готовые индексы из `modules_index.py` подключены к `Registry`:
`add_modules`/`_add_extension` строят их при разборе, кладут в
кэш проекта и делают доступными инструментам через `ResolvedContext.modules`/
`ResolvedContext.extension`. `restore()` после рестарта поднимает их снова —
из кэша, если штамп сошёлся, иначе строит заново прямо по коду на диске.

Три правила порядка зафиксированы в `docs/modules-provider-design.md`,
раздел 9:

1. **Индекс строится из временного каталога до рокировки.** Собери его
   ПОСЛЕ — и всё время сборки на диске уже новый код, а в памяти ещё старое
   оглавление: `get_procedure` отдавал бы куски чужих процедур по старым
   номерам строк, без единой пометки.
2. **Отказ сборки — `STATUS_ERROR` (или отсутствующая запись), а не
   молчаливое расхождение реестра с диском.**
3. **Снятие конфигурации снимает и её код** — каскад на `<Имя>:modules` и
   `<Имя>:ext:*`.

Реестр строится боевым путём — `Registry(рабочий / "data")` +
`add_configuration`/`add_modules` — как в `tests/test_registry_extensions.py`
и `tests/test_index_cache_modules.py`. Фикстур `живой_реестр`/
`add_modules_from_dir` в проекте нет, и здесь они не заводятся — только
реальные вызовы.

`рабочий` — отдельный от `корень_кода` временный каталог (`tmp_path_factory`,
не `tmp_path`): `корень_кода` — фикстура conftest.py, она пишет дерево модулей
прямо в свой `tmp_path`, и если туда же класть `data/` реестра и архивы, то
после первой же загрузки `корень_кода.rglob("*")` подхватит уже РАЗОБРАННЫЙ
код (`data/modules/Пример/...bsl`) как ещё один модуль выгрузки — с путём вида
`data/modules/...`, непонятным `module_address`. Этот конфликт путей
воспроизводит отдельный регрессионный тест.
"""
from __future__ import annotations

import json
import threading
import time
import zipfile
from pathlib import Path

import pytest
from starlette.applications import Starlette

from conftest import (
    build_configuration,
    живой_клиент,
    modules_configuration_xml,
    write_export,
)
from mcp1c import coverage_log, index_cache, intake, modules_index, search
from mcp1c.registry import (
    KIND_EXTENSION,
    KIND_MODULES,
    STATUS_ERROR,
    Registry,
    RegistryError,
)
from mcp1c.server import build_server


def _архив_кода(корень: Path, файл_zip: Path, *, version: str = "") -> Path:
    """Пакует дерево `корень` в архив выгрузки в файлы для `add_modules`.

    Список файлов снимается ДО открытия `zipfile.ZipFile` на запись: обход
    дерева ПОСЛЕ создания архива подхватил бы недописанный `.zip` как ещё
    один файл выгрузки (тот же приём, что в `tests/test_index_cache_modules.
    py`). `файл_zip` пишется ВНЕ `корень` (см. докстроку модуля про `рабочий`
    != `корень_кода`) — иначе он попал бы в собственный же список файлов.
    """
    файлы = [путь for путь in sorted(корень.rglob("*")) if путь.is_file()]
    with zipfile.ZipFile(файл_zip, "w") as zf:
        zf.writestr("Configuration.xml", modules_configuration_xml(version=version))
        for путь in файлы:
            zf.write(путь, путь.relative_to(корень).as_posix())
    return файл_zip


def _архив_расширения(корень: Path, файл_zip: Path) -> Path:
    файлы = [путь for путь in sorted(корень.rglob("*")) if путь.is_file()]
    with zipfile.ZipFile(файл_zip, "w") as zf:
        zf.writestr(
            "Configuration.xml",
            (
                '<MetaDataObject xmlns="http://v8.1c.ru/8.3/MDClasses">'
                '<Configuration uuid="00000000-0000-0000-0000-000000000000">'
                "<Properties><Name>Доп</Name><NamePrefix>zzд_</NamePrefix>"
                "<ObjectBelonging>Adopted</ObjectBelonging>"
                "<ConfigurationExtensionPurpose>Customization"
                "</ConfigurationExtensionPurpose></Properties>"
                "</Configuration></MetaDataObject>"
            ),
        )
        for путь in файлы:
            zf.write(путь, путь.relative_to(корень).as_posix())
    return файл_zip


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


def _вызвать_с_ошибкой(ошибки: list[Exception], функция, *args, **kwargs) -> None:
    try:
        функция(*args, **kwargs)
    except Exception as error:
        ошибки.append(error)


def _архив_с_новой_процедурой(корень: Path, файл_zip: Path) -> Path:
    """Меняет общий модуль так, чтобы поколения различались содержимым."""
    модуль = корень / "CommonModules" / "ОбщийПример" / "Ext" / "Module.bsl"
    модуль.write_text(
        "Процедура Новая() Экспорт\nКонецПроцедуры\n", encoding="utf-8"
    )
    return _архив_кода(корень, файл_zip, version="2.0.0")


def test_модули_попадают_в_контекст(tmp_path_factory, корень_кода):
    рабочий = tmp_path_factory.mktemp("реестр")
    реестр = _реестр_с_конфигурацией(рабочий)
    архив = _архив_кода(корень_кода, рабочий / "модули.zip")
    реестр.add_modules(архив, configuration="Пример")

    контекст = реестр.resolve("Пример")

    assert контекст.modules is not None
    assert контекст.modules.оглавление.по_имени("сложить")


def test_снятие_конфигурации_снимает_код(tmp_path_factory, корень_кода):
    рабочий = tmp_path_factory.mktemp("реестр")
    реестр = _реестр_с_конфигурацией(рабочий)
    архив = _архив_кода(корень_кода, рабочий / "модули.zip")
    реестр.add_modules(архив, configuration="Пример")
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
    выгрузка_расширения = _архив_расширения(
        корень_кода, рабочий / "расширение.zip"
    )
    реестр.add_modules(выгрузка_расширения, configuration="Пример")
    корень_расширения = реестр._extension_root("Пример", "Доп")
    assert корень_расширения.exists()
    assert "Пример:ext:Доп" in реестр.sources

    реестр.remove("Пример")

    assert "Пример:ext:Доп" not in реестр.sources
    assert not корень_расширения.exists()


def test_отказ_сборки_оставляет_источник_в_ошибке(
    tmp_path_factory, корень_кода, monkeypatch
):
    рабочий = tmp_path_factory.mktemp("реестр")
    реестр = _реестр_с_конфигурацией(рабочий)
    архив = _архив_кода(корень_кода, рабочий / "модули.zip")

    настоящее_построить = modules_index.Оглавление.построить

    def падает(*_, **__):
        raise MemoryError("не влезло")

    monkeypatch.setattr(modules_index.Оглавление, "построить", staticmethod(падает))

    with pytest.raises(RegistryError):
        реестр.add_modules(архив, configuration="Пример")

    источник = реестр.sources.get("Пример:modules")
    assert источник is None or источник.status == STATUS_ERROR
    # Ни каталога кода, ни записи в индексе — отказ сборки не должен оставить
    # реестр и диск в разных состояниях.
    assert not реестр._modules_root("Пример").exists()
    assert "Пример:modules" not in реестр.modules
    assert not list(
        реестр.modules_dir.glob(".Пример.tmp-*")
    )
    # Отказ освобождает lifecycle-lock: следующая попытка не
    # висит и публикует готовый комплект.
    monkeypatch.setattr(
        modules_index.Оглавление,
        "построить",
        staticmethod(настоящее_построить),
    )
    источник = реестр.add_modules(архив, configuration="Пример")
    assert источник.status == "ready"


def test_ошибка_fs_актуального_reparse_санитизирована_и_сохраняет_cause(
    tmp_path_factory, корень_кода, monkeypatch
):
    рабочий = tmp_path_factory.mktemp("реестр")
    реестр = _реестр_с_конфигурацией(рабочий)
    архив = _архив_кода(корень_кода, рабочий / "модули.zip")
    настоящее_extract = intake.extract

    def падает(*_, **__):
        raise OSError("нет места")

    monkeypatch.setattr(intake, "extract", падает)
    with pytest.raises(RegistryError) as ошибка:
        реестр.add_modules(архив, configuration="Пример")

    assert "нет места" not in str(ошибка.value)
    assert isinstance(ошибка.value.__cause__, OSError)
    assert not list(реестр.modules_dir.glob(".Пример.tmp-*"))

    # Ошибка актуальной операции не превращается в отмену и не оставляет
    # source-lock занятым: повторная попытка может завершиться.
    monkeypatch.setattr(intake, "extract", настоящее_extract)
    источник = реестр.add_modules(архив, configuration="Пример")
    assert источник.status == "ready"


def test_расхождение_версий_называется(tmp_path_factory, корень_кода):
    рабочий = tmp_path_factory.mktemp("реестр")
    реестр = _реестр_с_конфигурацией(рабочий, version="2.0.0")
    архив = _архив_кода(корень_кода, рабочий / "модули.zip", version="1.0.0")

    реестр.add_modules(архив, configuration="Пример")

    оговорки = реестр.resolve("Пример").notes()
    assert any("1.0.0" in о and "2.0.0" in о for о in оговорки)


def test_совпадающие_версии_молчат(tmp_path_factory, корень_кода):
    """Оговорка появляется только при расхождении — не на каждой загрузке."""
    рабочий = tmp_path_factory.mktemp("реестр")
    реестр = _реестр_с_конфигурацией(рабочий, version="2.0.0")
    архив = _архив_кода(корень_кода, рабочий / "модули.zip", version="2.0.0")

    реестр.add_modules(архив, configuration="Пример")

    оговорки = реестр.resolve("Пример").notes()
    assert not any("разошлись" in о for о in оговорки)


def test_индекс_строится_из_временного_каталога_до_рокировки(
    tmp_path_factory, корень_кода, monkeypatch
):
    """Прямое доказательство безопасного порядка публикации:
    пока строится индекс, `корень` на диске ещё несёт СТАРЫЙ код.

    Первая загрузка кладёт код на `корень`. Вторая — с ДОБАВЛЕННЫМ модулем —
    перехватывает момент сборки индекса и проверяет диск в этот момент:
    новый модуль там появиться ещё не должен, а старый — обязан быть на
    месте. Мутация, которая переставит сборку индекса ПОСЛЕ рокировки,
    роняет этот тест: на диске в момент сборки будет уже новый файл.
    """
    рабочий = tmp_path_factory.mktemp("реестр")
    реестр = _реестр_с_конфигурацией(рабочий)
    первый_архив = _архив_кода(корень_кода, рабочий / "модули-1.zip")
    реестр.add_modules(первый_архив, configuration="Пример")
    корень = реестр._modules_root("Пример")

    # Новый ОБЩИЙ модуль — не переименованный файл: `module_address` знает
    # только фиксированные имена файлов («Module.bsl», «ObjectModule.bsl»
    # и т.п.), «Module2.bsl» в таблице соответствия нет вовсе.
    новый_модуль = корень_кода / "CommonModules" / "ОбщийНовый" / "Ext" / "Module.bsl"
    новый_модуль.parent.mkdir(parents=True)
    новый_модуль.write_text(
        "Процедура НоваяПроцедура() Экспорт\nКонецПроцедуры\n", encoding="utf-8"
    )
    второй_архив = _архив_кода(корень_кода, рабочий / "модули-2.zip")

    исходное_построить = modules_index.Оглавление.построить
    снимок: dict[str, object] = {}

    def перехват(путь_сборки: Path, **kwargs):
        # `путь_сборки` — временный каталог `_extract_to_temp`, а не `корень`:
        # если бы сборка шла ПОСЛЕ рокировки, оба пути совпали бы.
        снимок["путь_сборки_это_не_корень"] = путь_сборки != корень
        снимок["на_корне_ещё_нет_нового_модуля"] = not (
            корень / "CommonModules" / "ОбщийНовый" / "Ext" / "Module.bsl"
        ).exists()
        снимок["на_корне_есть_старый_код"] = (
            корень / "CommonModules" / "ОбщийПример" / "Ext" / "Module.bsl"
        ).exists()
        return исходное_построить(путь_сборки, **kwargs)

    monkeypatch.setattr(modules_index.Оглавление, "построить", staticmethod(перехват))

    реестр.add_modules(второй_архив, configuration="Пример")

    assert снимок == {
        "путь_сборки_это_не_корень": True,
        "на_корне_ещё_нет_нового_модуля": True,
        "на_корне_есть_старый_код": True,
    }
    # После рокировки новый модуль на месте — доказывает, что перехват вообще
    # застал реальную последовательность, а не сборку, которая тихо не удалась.
    assert (
        корень / "CommonModules" / "ОбщийНовый" / "Ext" / "Module.bsl"
    ).exists()


def test_рестарт_поднимает_индекс_из_кэша(tmp_path_factory, корень_кода):
    """`restore()` поднимает индекс из кэша, а не молчит про него.

    Мутация, которая уберёт вызов `поднять_индексы`/сборку из `restore()`
    и оставит только `KIND_MODULES` в `self.sources`, роняет этот тест:
    `контекст.modules` останется `None`.
    """
    рабочий = tmp_path_factory.mktemp("реестр")
    реестр = _реестр_с_конфигурацией(рабочий)
    архив = _архив_кода(корень_кода, рабочий / "модули.zip", version="1.0.0")
    реестр.add_modules(архив, configuration="Пример")
    реестр.save()

    заново = Registry(реестр.data_dir)
    problems = заново.restore()

    assert not problems
    контекст = заново.resolve("Пример")
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
    архив = _архив_кода(корень_кода, рабочий / "модули.zip")
    реестр.add_modules(архив, configuration="Пример")
    реестр.save()
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
        модули = заново.resolve("Пример").modules
        assert модули is not None
        assert модули.готов is False
        assert модули.прогресс[0] < модули.прогресс[1]
    finally:
        отпустить.set()

    _дождаться(lambda: заново.resolve("Пример").modules.готов)
    готовые = заново.resolve("Пример").modules
    assert готовые.оглавление.по_имени("сложить")
    assert готовые.прогресс[0] == готовые.прогресс[1]


def test_четыре_индекса_появляются_только_готовым_комплектом(
    tmp_path_factory, корень_кода, monkeypatch
):
    """Даже на последней фазе наружу не торчат три готовых индекса из четырёх."""
    рабочий = tmp_path_factory.mktemp("реестр")
    реестр = _реестр_с_конфигурацией(рабочий)
    архив = _архив_кода(корень_кода, рабочий / "модули.zip")
    реестр.add_modules(архив, configuration="Пример")
    реестр.save()
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
        модули = заново.resolve("Пример").modules
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

    _дождаться(lambda: заново.resolve("Пример").modules.готов)
    готовые = заново.resolve("Пример").modules
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
    архив = _архив_кода(корень_кода, рабочий / "модули.zip")
    реестр.add_modules(архив, configuration="Пример")
    реестр.save()
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
        модули = заново.resolve("Пример").modules
        assert модули.готов is False
        assert модули.этап == (1, 4)
        assert модули.название_этапа == "оглавление"
        assert модули.прогресс == (1, 3)
    finally:
        отпустить.set()

    _дождаться(lambda: заново.resolve("Пример").modules.готов)


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
    реестр.add_modules(_архив_кода(корень, рабочий / "modules.zip"), configuration="Пример")
    # Холодный builder обязан считать фактический canonical root. Добавляем
    # исходный модуль уже туда: отбор архива выбирает один формат, тогда как
    # этот regression проверяет именно mixed root, оставшийся после обновления.
    обычный = реестр.modules_dir / "Пример/CommonModules/Открытый/Ext/Module.bsl"
    обычный.parent.mkdir(parents=True)
    обычный.write_text("Процедура А() Экспорт\nКонецПроцедуры\n", encoding="utf-8")
    реестр.save()
    _снести_кэш_модулей(реестр)

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
        loaded = fresh.resolve("Пример").modules
        assert loaded.этап == (1, 4)
        assert loaded.прогресс == (0, 2)
    finally:
        release.set()

    _дождаться(lambda: fresh.resolve("Пример").modules.готов)


def test_без_выгрузки_кода_состояния_модулей_нет(tmp_path_factory):
    рабочий = tmp_path_factory.mktemp("реестр")
    реестр = _реестр_с_конфигурацией(рабочий)

    assert реестр.resolve("Пример").modules is None


def test_отказ_фоновой_сборки_не_остаётся_вечным_строится(
    tmp_path_factory, корень_кода, monkeypatch, caplog
):
    рабочий = tmp_path_factory.mktemp("реестр")
    реестр = _реестр_с_конфигурацией(рабочий)
    архив = _архив_кода(корень_кода, рабочий / "модули.zip")
    реестр.add_modules(архив, configuration="Пример")
    реестр.save()
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
    assert заново.resolve("Пример").modules.готов is False
    assert "Фоновая сборка индекса кода" in caplog.text
    assert "MemoryError: не влезло" in caplog.text


def test_параллельные_cold_miss_восстанавливают_все_корпуса(
    tmp_path_factory,
    корень_кода,
    monkeypatch,
):
    рабочий = tmp_path_factory.mktemp("параллельный-cold")
    реестр = _реестр_с_конфигурацией(рабочий)
    реестр.add_modules(
        _архив_кода(корень_кода, рабочий / "base.zip"),
        configuration="Пример",
    )
    реестр.add_modules(
        _архив_расширения(корень_кода, рабочий / "extension.zip"),
        configuration="Пример",
    )
    реестр.save()
    источники = (
        реестр.sources["Пример:modules"],
        реестр.sources["Пример:ext:Доп"],
    )
    for источник in источники:
        for вид in реестр.CACHE_KINDS[источник.kind]:
            реестр._cache_path(источник.id, вид).unlink(missing_ok=True)

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
    архив = _архив_кода(корень_кода, рабочий / "модули.zip")
    реестр.add_modules(архив, configuration="Пример")
    реестр.save()
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

    _дождаться(lambda: заново.resolve("Пример").modules.готов)


def test_снятие_источника_во_время_сборки_не_воскрешает_индекс(
    tmp_path_factory, корень_кода, monkeypatch
):
    рабочий = tmp_path_factory.mktemp("реестр")
    реестр = _реестр_с_конфигурацией(рабочий)
    архив = _архив_кода(корень_кода, рабочий / "модули.zip")
    реестр.add_modules(архив, configuration="Пример")
    реестр.save()
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
    архив = _архив_кода(корень_кода, рабочий / "модули.zip")
    реестр.add_modules(архив, configuration="Пример")
    реестр.save()
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

    _дождаться(lambda: заново.resolve("Пример").modules.готов)
    assert build_calls == 1


@pytest.mark.parametrize("первый_исход", ["ready", "error"])
def test_фон_не_сохраняет_частичный_registry_во_время_startup(
    tmp_path_factory, корень_кода, monkeypatch, первый_исход
):
    """Фоновый save ждёт полного restore, включая следующий code-source."""
    рабочий = tmp_path_factory.mktemp("реестр")
    исходный = _реестр_с_конфигурацией(рабочий)
    исходный.add_modules(
        _архив_кода(корень_кода, рабочий / "modules.zip"),
        configuration="Пример",
    )
    исходный.add_modules(
        _архив_расширения(корень_кода, рабочий / "extension.zip"),
        configuration="Пример",
    )
    исходный.save()
    ожидаемые_id = {"Пример", "Пример:modules", "Пример:ext:Доп"}
    for source_id, kind in (
        ("Пример:modules", KIND_MODULES),
        ("Пример:ext:Доп", KIND_EXTENSION),
    ):
        for вид in исходный.CACHE_KINDS[kind]:
            исходный._cache_path(source_id, вид).unlink(missing_ok=True)

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
    исходный.add_modules(
        _архив_кода(корень_кода, рабочий / "modules.zip"),
        configuration="Пример",
    )
    исходный.add_modules(
        _архив_расширения(корень_кода, рабочий / "extension.zip"),
        configuration="Пример",
    )
    исходный.save()
    ожидаемые_id = {"Пример", "Пример:modules", "Пример:ext:Доп"}
    for source_id, kind in (
        ("Пример:modules", KIND_MODULES),
        ("Пример:ext:Доп", KIND_EXTENSION),
    ):
        for вид in исходный.CACHE_KINDS[kind]:
            исходный._cache_path(source_id, вид).unlink(missing_ok=True)

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


def test_старый_фоновый_writer_не_портит_кэш_нового_разбора(
    tmp_path_factory, корень_кода, monkeypatch
):
    рабочий = tmp_path_factory.mktemp("реестр")
    реестр = _реестр_с_конфигурацией(рабочий)
    старый = _архив_кода(корень_кода, рабочий / "old.zip", version="1.0.0")
    реестр.add_modules(старый, configuration="Пример")
    реестр.save()
    _снести_кэш_модулей(реестр)
    новый = _архив_с_новой_процедурой(корень_кода, рабочий / "new.zip")

    старый_writer = threading.Event()
    отпустить_writer = threading.Event()
    save_calls = 0
    настоящее_сохранить = modules_index.сохранить_индексы

    def сохранить(*args, **kwargs):
        nonlocal save_calls
        save_calls += 1
        if save_calls == 1:
            старый_writer.set()
            отпустить_writer.wait(timeout=3)
        return настоящее_сохранить(*args, **kwargs)

    monkeypatch.setattr(modules_index, "сохранить_индексы", сохранить)
    заново = Registry(реестр.data_dir)
    reparse_errors: list[Exception] = []

    def переразобрать():
        try:
            заново.add_modules(новый, configuration="Пример")
        except Exception as error:
            reparse_errors.append(error)

    reparse = threading.Thread(target=переразобрать)
    try:
        assert not заново.startup()
        assert старый_writer.wait(timeout=1)
        reparse.start()
    finally:
        отпустить_writer.set()
        reparse.join(timeout=3)

    assert not reparse.is_alive()
    assert not reparse_errors
    _дождаться(lambda: "Пример:modules" not in заново._module_builds)
    в_памяти = заново.resolve("Пример").modules.оглавление
    assert в_памяти.по_имени("Новая")
    assert not в_памяти.по_имени("Сложить")
    заново.save()

    после_рестарта = Registry(реестр.data_dir)
    assert not после_рестарта.restore()
    поднято = после_рестарта.resolve("Пример").modules
    assert поднято.готов is True
    assert поднято.оглавление.по_имени("Новая")
    assert not поднято.оглавление.по_имени("Сложить")


def test_заблокированный_warm_load_не_перетирает_reparse(
    tmp_path_factory, корень_кода, monkeypatch
):
    рабочий = tmp_path_factory.mktemp("реестр")
    реестр = _реестр_с_конфигурацией(рабочий)
    старый = _архив_кода(корень_кода, рабочий / "old.zip", version="1.0.0")
    реестр.add_modules(старый, configuration="Пример")
    реестр.save()
    новый = _архив_с_новой_процедурой(корень_кода, рабочий / "new.zip")

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
        заново.add_modules(новый, configuration="Пример")
    finally:
        отпустить.set()
        поток.join(timeout=3)

    assert not поток.is_alive()
    модули = заново.resolve("Пример").modules
    assert модули.source is заново.sources["Пример:modules"]
    assert модули.source.origin == "new.zip"
    assert модули.оглавление.по_имени("Новая")
    assert not модули.оглавление.по_имени("Сложить")


def test_заблокированный_warm_load_не_воскрешает_remove(
    tmp_path_factory, корень_кода, monkeypatch
):
    рабочий = tmp_path_factory.mktemp("реестр")
    реестр = _реестр_с_конфигурацией(рабочий)
    старый = _архив_кода(корень_кода, рабочий / "old.zip", version="1.0.0")
    реестр.add_modules(старый, configuration="Пример")
    реестр.save()

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


def test_foreground_reparse_не_воскрешает_код_после_remove(
    tmp_path_factory, корень_кода, monkeypatch
):
    """Remove инвалидирует reparse ещё до его долгой сборки."""
    рабочий = tmp_path_factory.mktemp("реестр")
    реестр = _реестр_с_конфигурацией(рабочий)
    старый = _архив_кода(корень_кода, рабочий / "old.zip")
    реестр.add_modules(старый, configuration="Пример")
    реестр.save()
    новый = _архив_с_новой_процедурой(корень_кода, рабочий / "new.zip")

    сборка_начата = threading.Event()
    отпустить = threading.Event()
    настоящее_построить = modules_index.Оглавление.построить

    def построить(корень, **kwargs):
        сборка_начата.set()
        отпустить.wait(timeout=3)
        return настоящее_построить(корень, **kwargs)

    monkeypatch.setattr(
        modules_index.Оглавление, "построить", staticmethod(построить)
    )
    ошибки: list[Exception] = []

    def переразобрать():
        try:
            реестр.add_modules(новый, configuration="Пример")
        except Exception as error:
            ошибки.append(error)

    поток = threading.Thread(target=переразобрать)
    поток.start()
    try:
        assert сборка_начата.wait(timeout=1)
        реестр.remove("Пример")
    finally:
        отпустить.set()
        поток.join(timeout=3)

    assert not поток.is_alive()
    assert ошибки
    assert not реестр.configurations
    assert "Пример:modules" not in реестр.sources
    assert "Пример:modules" not in реестр.modules
    assert not реестр._modules_root("Пример").exists()

    реестр.save()
    payload = json.loads(реестр.registry_path.read_text(encoding="utf-8"))
    assert payload["sources"] == []
    после_рестарта = Registry(реестр.data_dir)
    # Копия выгрузки метаданных остаётся orphan source по
    # общему контракту remove; важно, что реестр пуст.
    после_рестарта.startup()
    assert not после_рестарта.configurations
    assert "Пример:modules" not in после_рестарта.sources


def test_startup_не_подменяет_swapped_reparse_старым_registry(
    tmp_path_factory, корень_кода, monkeypatch
):
    """Active token защищает new root до завершения cache writer."""
    рабочий = tmp_path_factory.mktemp("реестр")
    реестр = _реестр_с_конфигурацией(рабочий)
    старый = _архив_кода(корень_кода, рабочий / "old.zip", version="1")
    реестр.add_modules(старый, configuration="Пример")
    реестр.save()
    новый = _архив_с_новой_процедурой(корень_кода, рабочий / "new.zip")

    writer_начат = threading.Event()
    отпустить = threading.Event()
    настоящее_сохранить = modules_index.сохранить_индексы

    def сохранить(*args, **kwargs):
        writer_начат.set()
        отпустить.wait(timeout=3)
        return настоящее_сохранить(*args, **kwargs)

    monkeypatch.setattr(modules_index, "сохранить_индексы", сохранить)
    reparse = threading.Thread(
        target=lambda: реестр.add_modules(новый, configuration="Пример")
    )
    startup = threading.Thread(target=реестр.startup)
    reparse.start()
    try:
        assert writer_начат.wait(timeout=1)
        startup.start()
        startup.join(timeout=2)
        assert not startup.is_alive()
    finally:
        отпустить.set()
        reparse.join(timeout=3)
        startup.join(timeout=3)

    assert not reparse.is_alive()
    source = реестр.sources["Пример:modules"]
    loaded = реестр.resolve("Пример").modules
    assert source.origin == "new.zip"
    assert loaded.source is source
    assert loaded.оглавление.по_имени("Новая")
    assert not loaded.оглавление.по_имени("Сложить")

    после_рестарта = Registry(реестр.data_dir)
    assert not после_рестарта.restore()
    поднято = после_рестарта.resolve("Пример").modules
    assert поднято.готов is True
    assert поднято.source.origin == "new.zip"
    assert поднято.оглавление.по_имени("Новая")
    assert not поднято.оглавление.по_имени("Сложить")


def test_foreground_reparse_расширения_тоже_инвалидируется_remove(
    tmp_path_factory, корень_кода, monkeypatch
):
    """Extension и modules используют одну reservation/CAS границу."""
    рабочий = tmp_path_factory.mktemp("реестр")
    реестр = _реестр_с_конфигурацией(рабочий)
    старый = _архив_расширения(корень_кода, рабочий / "old-ext.zip")
    реестр.add_modules(старый, configuration="Пример")
    модуль = корень_кода / "CommonModules" / "ОбщийПример" / "Ext" / "Module.bsl"
    модуль.write_text("Процедура Новая() Экспорт\nКонецПроцедуры\n", encoding="utf-8")
    новый = _архив_расширения(корень_кода, рабочий / "new-ext.zip")

    начата = threading.Event()
    отпустить = threading.Event()
    настоящее_построить = modules_index.Оглавление.построить

    def построить(корень, **kwargs):
        начата.set()
        отпустить.wait(timeout=3)
        return настоящее_построить(корень, **kwargs)

    monkeypatch.setattr(
        modules_index.Оглавление, "построить", staticmethod(построить)
    )
    ошибки: list[Exception] = []
    поток = threading.Thread(
        target=lambda: _вызвать_с_ошибкой(
            ошибки, реестр.add_modules, новый, configuration="Пример"
        )
    )
    поток.start()
    try:
        assert начата.wait(timeout=1)
        реестр.remove("Пример")
    finally:
        отпустить.set()
        поток.join(timeout=3)

    assert not поток.is_alive()
    assert ошибки
    assert not реестр.configurations
    assert "Пример:ext:Доп" not in реестр.sources
    assert not реестр._extension_root("Пример", "Доп").exists()


@pytest.mark.parametrize("расширение", [False, True], ids=["modules", "extension"])
def test_старый_remove_не_удаляет_новый_canonical_root(
    tmp_path_factory, корень_кода, monkeypatch, расширение
):
    """Deferred rmtree может удалять только detached old generation."""
    рабочий = tmp_path_factory.mktemp("реестр")
    реестр = _реестр_с_конфигурацией(рабочий)
    if расширение:
        старый = _архив_расширения(корень_кода, рабочий / "old-ext.zip")
        source_id = "Пример:ext:Доп"
        корень = реестр._extension_root("Пример", "Доп")
        имя_drop = "_drop_extension_root"
    else:
        старый = _архив_кода(корень_кода, рабочий / "old.zip")
        source_id = "Пример:modules"
        корень = реестр._modules_root("Пример")
        имя_drop = "_drop_modules_root"
    реестр.add_modules(старый, configuration="Пример")
    реестр.save()

    модуль = корень_кода / "CommonModules" / "ОбщийПример" / "Ext" / "Module.bsl"
    модуль.write_text("Процедура Новая() Экспорт\nКонецПроцедуры\n", encoding="utf-8")
    новый = (
        _архив_расширения(корень_кода, рабочий / "new-ext.zip")
        if расширение
        else _архив_кода(корень_кода, рабочий / "new.zip")
    )

    старый_remove_дошёл_до_rmtree = threading.Event()
    отпустить_старый_remove = threading.Event()
    настоящий_drop = getattr(реестр, имя_drop)
    вызовов = 0
    удаляемые: list[Path] = []
    счётчик_lock = threading.Lock()

    def задержать_первый_drop(путь):
        nonlocal вызовов
        with счётчик_lock:
            вызовов += 1
            первый = вызовов == 1
            удаляемые.append(Path(путь))
        if первый:
            старый_remove_дошёл_до_rmtree.set()
            отпустить_старый_remove.wait(timeout=3)
        настоящий_drop(путь)

    monkeypatch.setattr(реестр, имя_drop, задержать_первый_drop)
    ошибки_remove: list[Exception] = []
    remover = threading.Thread(
        target=lambda: _вызвать_с_ошибкой(ошибки_remove, реестр.remove, "Пример")
    )
    remover.start()
    try:
        assert старый_remove_дошёл_до_rmtree.wait(timeout=1)
        входящее = рабочий / "again"
        входящее.mkdir()
        реестр.add_configuration(
            write_export(входящее, build_configuration(name="Пример"))
        )
        реестр.add_modules(новый, configuration="Пример")
        реестр.save()
    finally:
        отпустить_старый_remove.set()
        remover.join(timeout=3)

    assert not remover.is_alive()
    assert not ошибки_remove
    assert корень.exists()
    assert удаляемые[0] != корень
    source = реестр.sources[source_id]
    assert source.origin == новый.name
    loaded = (
        реестр.resolve("Пример", extension="Доп").extension
        if расширение
        else реестр.resolve("Пример").modules
    )
    assert loaded.оглавление.по_имени("Новая")
    assert not loaded.оглавление.по_имени("Сложить")
    assert not [
        путь
        for путь in корень.parent.iterdir()
        if путь.name.startswith(f".{корень.name}.")
    ]

    после_рестарта = Registry(реестр.data_dir)
    assert not после_рестарта.restore()
    поднято = (
        после_рестарта.resolve("Пример", extension="Доп").extension
        if расширение
        else после_рестарта.resolve("Пример").modules
    )
    assert поднято.готов is True
    assert поднято.source.origin == новый.name
    assert поднято.оглавление.по_имени("Новая")


def test_remove_откатывает_detach_если_следующий_rename_не_удался(
    tmp_path_factory, корень_кода, monkeypatch
):
    """До изменения registry все уже detached roots можно вернуть."""
    рабочий = tmp_path_factory.mktemp("реестр")
    реестр = _реестр_с_конфигурацией(рабочий)
    реестр.add_modules(
        _архив_кода(корень_кода, рабочий / "modules.zip"),
        configuration="Пример",
    )
    реестр.add_modules(
        _архив_расширения(корень_кода, рабочий / "extension.zip"),
        configuration="Пример",
    )
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
    реестр.add_modules(
        _архив_кода(корень_кода, рабочий / "modules.zip"),
        configuration="Пример",
    )
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
    архив = (
        _архив_расширения(корень_кода, рабочий / "extension.zip")
        if расширение
        else _архив_кода(корень_кода, рабочий / "modules.zip")
    )
    исходный.add_modules(архив, configuration="Пример")
    исходный.save()
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
    архив = (
        _архив_расширения(корень_кода, рабочий / "extension.zip")
        if расширение
        else _архив_кода(корень_кода, рабочий / "modules.zip")
    )
    исходный.add_modules(архив, configuration="Пример")
    исходный.save()
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


@pytest.mark.parametrize("расширение", [False, True], ids=["modules", "extension"])
def test_restore_не_откатывает_завершённый_reparse_к_старой_строке(
    tmp_path_factory, корень_кода, monkeypatch, расширение
):
    """Generation tombstone живёт дольше active operation token."""
    рабочий = tmp_path_factory.mktemp("реестр")
    исходный = _реестр_с_конфигурацией(рабочий)
    старый = (
        _архив_расширения(корень_кода, рабочий / "old-ext.zip")
        if расширение
        else _архив_кода(корень_кода, рабочий / "old.zip")
    )
    исходный.add_modules(старый, configuration="Пример")
    исходный.save()
    модуль = (
        корень_кода / "CommonModules" / "ОбщийПример" / "Ext" / "Module.bsl"
    )
    модуль.write_text(
        "Процедура Новая() Экспорт\nКонецПроцедуры\n", encoding="utf-8"
    )
    новый = (
        _архив_расширения(корень_кода, рабочий / "new-ext.zip")
        if расширение
        else _архив_кода(корень_кода, рабочий / "new.zip")
    )
    source_id = "Пример:ext:Доп" if расширение else "Пример:modules"

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
    поток = threading.Thread(
        target=lambda: _вызвать_с_ошибкой(ошибки, восстановленный.startup)
    )
    поток.start()
    try:
        assert конфигурация_опубликована.wait(timeout=1)
        восстановленный.add_modules(новый, configuration="Пример")
    finally:
        отпустить_restore.set()
        поток.join(timeout=3)

    assert not поток.is_alive()
    assert not ошибки
    source = восстановленный.sources[source_id]
    loaded = восстановленный.modules[source_id]
    assert source.origin == новый.name
    assert loaded.source is source
    assert loaded.оглавление.по_имени("Новая")
    assert not loaded.оглавление.по_имени("Сложить")

    после_рестарта = Registry(исходный.data_dir)
    после_рестарта.startup()
    поднято = после_рестарта.modules[source_id]
    assert поднято.source.origin == новый.name
    assert поднято.оглавление.по_имени("Новая")


@pytest.mark.parametrize("расширение", [False, True], ids=["modules", "extension"])
def test_параллельные_reparse_не_удаляют_чужой_активный_tmp(
    tmp_path_factory, корень_кода, monkeypatch, расширение
):
    """Lifecycle source_id охватывает sweep, extract, build и publish."""
    рабочий = tmp_path_factory.mktemp("реестр")
    реестр = _реестр_с_конфигурацией(рабочий)

    def архив(имя: str) -> Path:
        return (
            _архив_расширения(корень_кода, рабочий / имя)
            if расширение
            else _архив_кода(корень_кода, рабочий / имя)
        )

    реестр.add_modules(архив("old.zip"), configuration="Пример")
    модуль = (
        корень_кода / "CommonModules" / "ОбщийПример" / "Ext" / "Module.bsl"
    )
    модуль.write_text(
        "Процедура Первая() Экспорт\nКонецПроцедуры\n", encoding="utf-8"
    )
    первый_архив = архив("first.zip")
    модуль.write_text(
        "Процедура Вторая() Экспорт\nКонецПроцедуры\n", encoding="utf-8"
    )
    второй_архив = архив("second.zip")
    source_id = "Пример:ext:Доп" if расширение else "Пример:modules"
    корень = (
        реестр._extension_root("Пример", "Доп")
        if расширение
        else реестр._modules_root("Пример")
    )

    первый_extract = threading.Event()
    второй_extract = threading.Event()
    отпустить_первый = threading.Event()
    второй_зарезервирован = threading.Event()
    настоящий_extract = intake.extract
    настоящая_резервация = реестр._зарезервировать_операцию_модулей
    вызовов_резервации = 0

    def извлечь(входной, временный):
        if входной == первый_архив:
            маркер = временный / "active-owner"
            маркер.mkdir()
            первый_extract.set()
            отпустить_первый.wait(timeout=3)
            if not маркер.is_dir():
                raise FileNotFoundError("чужой active tmp удалён")
        elif входной == второй_архив:
            второй_extract.set()
        return настоящий_extract(входной, временный)

    def зарезервировать(*args, **kwargs):
        nonlocal вызовов_резервации
        операция = настоящая_резервация(*args, **kwargs)
        вызовов_резервации += 1
        if вызовов_резервации == 2:
            второй_зарезервирован.set()
        return операция

    monkeypatch.setattr(intake, "extract", извлечь)
    monkeypatch.setattr(
        реестр,
        "_зарезервировать_операцию_модулей",
        зарезервировать,
    )
    ошибки_первого: list[Exception] = []
    ошибки_второго: list[Exception] = []
    первый = threading.Thread(
        target=lambda: _вызвать_с_ошибкой(
            ошибки_первого,
            реестр.add_modules,
            первый_архив,
            configuration="Пример",
        )
    )
    второй = threading.Thread(
        target=lambda: _вызвать_с_ошибкой(
            ошибки_второго,
            реестр.add_modules,
            второй_архив,
            configuration="Пример",
        )
    )
    первый.start()
    assert первый_extract.wait(timeout=1)
    второй.start()
    try:
        assert второй_зарезервирован.wait(timeout=1)
        # До lifecycle-lock вторая операция успевает войти в
        # extract и снести первый tmp. После исправления она здесь
        # ждёт source-lock, поэтому короткое ожидание закончивается без
        # события и разблокирует первую.
        второй_extract.wait(timeout=0.2)
    finally:
        отпустить_первый.set()
        первый.join(timeout=3)
        второй.join(timeout=3)

    assert not первый.is_alive()
    assert not второй.is_alive()
    assert len(ошибки_первого) == 1
    assert isinstance(ошибки_первого[0], RegistryError)
    assert "отмен" in str(ошибки_первого[0])
    assert not ошибки_второго
    source = реестр.sources[source_id]
    loaded = реестр.modules[source_id]
    assert source.origin == второй_архив.name
    assert loaded.source is source
    assert loaded.оглавление.по_имени("Вторая")
    assert not loaded.оглавление.по_имени("Первая")
    assert not list(корень.parent.glob(f".{корень.name}.tmp-*"))


def test_remove_инвалидирует_ожидающий_reparse_до_extract(
    tmp_path_factory, корень_кода, monkeypatch
):
    рабочий = tmp_path_factory.mktemp("реестр")
    реестр = _реестр_с_конфигурацией(рабочий)
    старый = _архив_кода(корень_кода, рабочий / "old.zip")
    реестр.add_modules(старый, configuration="Пример")
    новый = _архив_с_новой_процедурой(корень_кода, рабочий / "new.zip")
    source_id = "Пример:modules"
    lifecycle_lock = реестр._module_operation_locks[source_id]
    lifecycle_lock.acquire()
    зарезервирован = threading.Event()
    настоящая_резервация = реестр._зарезервировать_операцию_модулей
    настоящий_extract = intake.extract
    extract_calls = 0
    space_calls = 0

    def зарезервировать(*args, **kwargs):
        операция = настоящая_резервация(*args, **kwargs)
        зарезервирован.set()
        return операция

    def считать_extract(*args, **kwargs):
        nonlocal extract_calls
        extract_calls += 1
        return настоящий_extract(*args, **kwargs)

    def считать_место(нужно, каталог):
        nonlocal space_calls
        space_calls += 1
        return True, нужно

    monkeypatch.setattr(
        реестр,
        "_зарезервировать_операцию_модулей",
        зарезервировать,
    )
    monkeypatch.setattr(intake, "extract", считать_extract)
    monkeypatch.setattr(intake, "enough_space", считать_место)
    ошибки: list[Exception] = []
    поток = threading.Thread(
        target=lambda: _вызвать_с_ошибкой(
            ошибки,
            реестр.add_modules,
            новый,
            configuration="Пример",
        )
    )
    поток.start()
    try:
        assert зарезервирован.wait(timeout=1)
        реестр.remove("Пример")
    finally:
        lifecycle_lock.release()
        поток.join(timeout=3)

    assert not поток.is_alive()
    assert len(ошибки) == 1
    assert isinstance(ошибки[0], RegistryError)
    assert "отмен" in str(ошибки[0])
    assert extract_calls == 0
    assert space_calls == 0
    assert not реестр.sources
    assert not реестр.modules
    assert not list(реестр.modules_dir.glob(".Пример.tmp-*"))


def test_из_трёх_ожидающих_reparse_извлекается_только_последний(
    tmp_path_factory, корень_кода, monkeypatch
):
    рабочий = tmp_path_factory.mktemp("реестр")
    реестр = _реестр_с_конфигурацией(рабочий)
    реестр.add_modules(
        _архив_кода(корень_кода, рабочий / "old.zip"),
        configuration="Пример",
    )
    модуль = (
        корень_кода / "CommonModules" / "ОбщийПример" / "Ext" / "Module.bsl"
    )
    архивы: list[Path] = []
    for номер, имя in enumerate(("Первая", "Вторая", "Третья"), 1):
        модуль.write_text(
            f"Процедура {имя}() Экспорт\nКонецПроцедуры\n",
            encoding="utf-8",
        )
        архивы.append(
            _архив_кода(корень_кода, рабочий / f"{номер}.zip")
        )

    source_id = "Пример:modules"
    lifecycle_lock = реестр._module_operation_locks[source_id]
    lifecycle_lock.acquire()
    резервации = [threading.Event() for _ in архивы]
    настоящая_резервация = реестр._зарезервировать_операцию_модулей
    вызовов_резервации = 0
    настоящий_extract = intake.extract
    extract_calls = 0
    space_calls = 0

    def зарезервировать(*args, **kwargs):
        nonlocal вызовов_резервации
        операция = настоящая_резервация(*args, **kwargs)
        резервации[вызовов_резервации].set()
        вызовов_резервации += 1
        return операция

    def извлечь(*args, **kwargs):
        nonlocal extract_calls
        extract_calls += 1
        return настоящий_extract(*args, **kwargs)

    def проверить_место(нужно, каталог):
        nonlocal space_calls
        space_calls += 1
        return True, нужно

    monkeypatch.setattr(
        реестр,
        "_зарезервировать_операцию_модулей",
        зарезервировать,
    )
    monkeypatch.setattr(intake, "extract", извлечь)
    monkeypatch.setattr(intake, "enough_space", проверить_место)
    ошибки = [[], [], []]
    потоки = [
        threading.Thread(
            target=lambda i=i: _вызвать_с_ошибкой(
                ошибки[i],
                реестр.add_modules,
                архивы[i],
                configuration="Пример",
            )
        )
        for i in range(3)
    ]
    try:
        for i, поток in enumerate(потоки):
            поток.start()
            assert резервации[i].wait(timeout=1)
    finally:
        lifecycle_lock.release()
        for поток in потоки:
            поток.join(timeout=3)

    assert not any(поток.is_alive() for поток in потоки)
    assert all(
        len(список) == 1 and isinstance(список[0], RegistryError)
        for список in ошибки[:2]
    )
    assert not ошибки[2]
    assert extract_calls == 1
    assert space_calls == 1
    source = реестр.sources[source_id]
    assert source.origin == архивы[2].name
    assert реестр.modules[source_id].оглавление.по_имени("Третья")
    assert not list(реестр.modules_dir.glob(".Пример.tmp-*"))
