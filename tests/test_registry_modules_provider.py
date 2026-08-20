"""Источник модулей в реестре — второе измерение `ResolvedContext`.

Задача 10 подключает готовые индексы (задачи 5–9, `modules_index.py`) к
`Registry`: `add_modules`/`_add_extension` строят их при разборе, кладут в
кэш проекта и делают доступными инструментам через `ResolvedContext.modules`/
`ResolvedContext.extension`. `restore()` после рестарта поднимает их снова —
из кэша, если штамп сошёлся, иначе строит заново прямо по коду на диске.

Три правила порядка, каждое найдено ревью (docs/modules-provider-design.md,
раздел 9):

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
`data/modules/...`, непонятным `module_address`. Ровно так это и было
обнаружено (см. отчёт задачи).
"""
from __future__ import annotations

import zipfile
from pathlib import Path

import pytest

from conftest import build_configuration, modules_configuration_xml, write_export
from mcp1c import modules_index
from mcp1c.registry import STATUS_ERROR, Registry, RegistryError


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
    выгрузка_расширения = рабочий / "расширение.zip"
    файлы = [путь for путь in sorted(корень_кода.rglob("*")) if путь.is_file()]
    with zipfile.ZipFile(выгрузка_расширения, "w") as zf:
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
            zf.write(путь, путь.relative_to(корень_кода).as_posix())
    реестр.add_modules(выгрузка_расширения, configuration="Пример")
    корень_расширения = реестр._extension_root("Пример", "Доп")
    assert корень_расширения.exists()
    assert "Пример:ext:Доп" in реестр.sources

    реестр.remove("Пример")

    assert "Пример:ext:Доп" not in реестр.sources
    assert not корень_расширения.exists()


def test_отказ_сборки_оставляет_источник_в_ошибке(tmp_path_factory, корень_кода, monkeypatch):
    рабочий = tmp_path_factory.mktemp("реестр")
    реестр = _реестр_с_конфигурацией(рабочий)
    архив = _архив_кода(корень_кода, рабочий / "модули.zip")

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
    """Прямое доказательство порядка, ради которого задача существует:
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

    def перехват(путь_сборки: Path):
        # `путь_сборки` — временный каталог `_extract_to_temp`, а не `корень`:
        # если бы сборка шла ПОСЛЕ рокировки, оба пути совпали бы.
        снимок["путь_сборки_это_не_корень"] = путь_сборки != корень
        снимок["на_корне_ещё_нет_нового_модуля"] = not (
            корень / "CommonModules" / "ОбщийНовый" / "Ext" / "Module.bsl"
        ).exists()
        снимок["на_корне_есть_старый_код"] = (
            корень / "CommonModules" / "ОбщийПример" / "Ext" / "Module.bsl"
        ).exists()
        return исходное_построить(путь_сборки)

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
    (сегодняшнее поведение до этой задачи — `KIND_MODULES` только кладётся
    в `self.sources`), роняет этот тест: `контекст.modules` останется `None`.
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
