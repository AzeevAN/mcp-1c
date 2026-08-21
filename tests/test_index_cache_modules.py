"""Кэш четырёх индексов провайдера `modules` (design doc, раздел 9).

`Registry.CACHE_KINDS` прежде держал для `KIND_MODULES` и
`KIND_EXTENSION` одно общее имя `("modules",)`, а `Registry._cached_names`
строит список файлов, которым `sweep` разрешено остаться на диске, только
из этого списка. Четыре структуры (оглавление, вызовы, формы, поиск) кладут
на диск четыре РАЗНЫХ файла — и с одним именем в `CACHE_KINDS` три из
четырёх были бы для `sweep` чужими и исчезали бы на первом же старте,
молча, каждый раз заново.

`tests/test_registry_extensions.py:218` уже проверяет, что `KIND_EXTENSION`
вообще есть в `CACHE_KINDS`, но делает это на одном виде кэша — на сломанном
варианте (одно общее имя вместо четырёх) тот тест прошёл бы. Здесь — тест,
которому для прохода нужны все четыре.

Уровень — единица, без сборки провайдера целиком: `add_modules` регистрирует
источник модулей (боевой путь, `Registry(tmp_path / "data")` +
`add_modules(zip, configuration=...)`, как в `test_registry_extensions.py`),
а `сохранить_индексы`/`поднять_индексы` вызываются напрямую поверх кода,
который `add_modules` уже положил на диск. В этом низкоуровневом тесте
`сохранить_индексы` вызывается напрямую; сквозная сборка при загрузке покрыта
тестами реестра.
"""

from __future__ import annotations

import zipfile
from pathlib import Path

from conftest import build_configuration, modules_configuration_xml, write_export
from mcp1c import index_cache, modules_index
from mcp1c.registry import Registry


def _корень_кода_в_zip(корень: Path, файл_zip: Path) -> Path:
    """Пакует дерево `корень_кода` в архив для `add_modules`.

    Список файлов снимается ДО открытия `zipfile.ZipFile` на запись: `корень`
    в тестах этого файла — сам `tmp_path` (фикстура `корень_кода` пишет
    дерево прямо в него), и если положить архив внутрь того же каталога и
    обойти дерево уже ПОСЛЕ его создания, `rglob` подхватит недописанный
    `.zip` как ещё один файл выгрузки.
    """
    файлы = [путь for путь in sorted(корень.rglob("*")) if путь.is_file()]
    with zipfile.ZipFile(файл_zip, "w") as zf:
        zf.writestr("Configuration.xml", modules_configuration_xml())
        for путь in файлы:
            zf.write(путь, путь.relative_to(корень).as_posix())
    return файл_zip


def _реестр_с_модулями(tmp_path: Path, корень_кода: Path, *, name: str = "Пример"):
    """Реестр с конфигурацией и загруженными модулями — боевым путём
    `add_modules`, а не выдуманным приёмом (см. докстроку модуля)."""
    входящее = tmp_path / "in"
    входящее.mkdir(exist_ok=True)
    реестр = Registry(tmp_path / "data")
    реестр.add_configuration(write_export(входящее, build_configuration(name=name)))
    архив = _корень_кода_в_zip(корень_кода, tmp_path / "модули.zip")
    источник = реестр.add_modules(архив, configuration=name)
    return реестр, источник


def _построить_индексы(корень: Path) -> modules_index.Индексы:
    оглавление = modules_index.Оглавление.построить(корень)
    return modules_index.Индексы(
        оглавление=оглавление,
        вызовы=modules_index.Вызовы.построить(корень, оглавление),
        формы=modules_index.Формы.построить(корень),
        поиск=modules_index.построить_поиск(оглавление, корень),
    )


def test_sweep_не_сносит_ни_один_индекс_модулей(tmp_path, корень_кода):
    """Один вид кэша на четыре структуры — три сносятся молча."""
    реестр, источник = _реестр_с_модулями(tmp_path, корень_кода)
    код_на_диске = реестр.modules_dir / "Пример"

    modules_index.сохранить_индексы(реестр, источник.id, _построить_индексы(код_на_диске))

    # Префикс — по идентификатору ИСТОЧНИКА МОДУЛЕЙ, не по имени
    # конфигурации: у конфигурации есть свои файлы кэша («Пример.objects»,
    # «Пример.fields»), которые тоже начинаются на «Пример» и не входят в
    # проверяемую четвёрку.
    префикс = index_cache.safe_name(источник.id)
    имена = {п.name for п in реестр.cache_dir.iterdir()}
    assert len([и for и in имена if и.startswith(префикс)]) == 4
    реестр.startup()
    assert {п.name for п in реестр.cache_dir.iterdir()} >= имена


def test_поднять_индексы_без_кэша_возвращает_none(tmp_path):
    """Источника не существует вовсе — не с чем сверять штамп, кэша нет."""
    реестр = Registry(tmp_path / "data")
    assert modules_index.поднять_индексы(реестр, "Пример:modules") is None


def test_selection_version_входит_в_штамп_кэша_при_том_же_sha(
    tmp_path, корень_кода
):
    """Тот же ZIP после v3 -> v4 содержит новый набор отобранных файлов."""
    реестр, источник = _реестр_с_модулями(tmp_path, корень_кода)
    код_на_диске = реестр.modules_dir / "Пример"
    источник.selection_version = 3
    modules_index.сохранить_индексы(
        реестр, источник.id, _построить_индексы(код_на_диске)
    )

    источник.selection_version = 4

    assert modules_index.поднять_индексы(реестр, источник.id) is None


def test_каталог_локаторов_переживает_warm_roundtrip_того_же_поколения(
    tmp_path, корень_кода
):
    реестр, источник = _реестр_с_модулями(tmp_path, корень_кода)
    исходный = реестр.modules[источник.id].каталог

    поднятые = modules_index.поднять_индексы(реестр, источник.id)

    assert исходный is not None
    assert поднятые is not None and поднятые.каталог is not None
    assert поднятые.каталог.identity == исходный.identity
    assert поднятые.каталог.coverage == исходный.coverage
    assert list(поднятые.каталог.entries) == list(исходный.entries)
    assert all(
        entry.locator is None or not hasattr(entry.locator, "body")
        for entry in поднятые.каталог.entries.values()
    )


def test_warm_restart_после_reparse_принимает_сохраненное_поколение(
    tmp_path, корень_кода, monkeypatch
):
    реестр, источник = _реестр_с_модулями(tmp_path, корень_кода)
    архив = tmp_path / "модули.zip"

    источник = реестр.add_modules(архив, configuration="Пример")
    assert источник.locator_generation > 1
    сохраненное_поколение = источник.locator_generation
    реестр.save()

    заново = Registry(реестр.data_dir)
    настоящая_сборка = заново._построить_индекс_кода

    def нельзя_перестраивать(*_args, **_kwargs):
        raise AssertionError("warm-кэш не должен перестраиваться")

    monkeypatch.setattr(заново, "_построить_индекс_кода", нельзя_перестраивать)
    проблемы = заново.startup()

    assert проблемы == []
    loaded = заново.modules[источник.id]
    assert loaded.готов
    assert loaded.каталог is not None
    assert loaded.каталог.identity.generation == сохраненное_поколение

    monkeypatch.setattr(заново, "_построить_индекс_кода", настоящая_сборка)
    следующий = заново.add_modules(архив, configuration="Пример")
    assert следующий.sha256 == источник.sha256
    assert следующий.locator_generation > сохраненное_поколение
    заново.save()

    финальный = Registry(реестр.data_dir)
    monkeypatch.setattr(
        финальный, "_построить_индекс_кода", нельзя_перестраивать
    )
    assert финальный.startup() == []
    assert (
        финальный.modules[источник.id].каталог.identity.generation
        == следующий.locator_generation
    )


def test_remove_readd_не_повторяет_identity_локаторов(
    tmp_path, корень_кода
):
    реестр, первый = _реестр_с_модулями(tmp_path, корень_кода)
    первая_identity = реестр.modules[первый.id].каталог.identity
    архив = tmp_path / "модули.zip"
    реестр.save()

    после_restart = Registry(реестр.data_dir)
    assert после_restart.startup() == []

    после_restart.remove(первый.id)
    второй = после_restart.add_modules(архив, configuration="Пример")
    вторая_identity = после_restart.modules[второй.id].каталог.identity

    assert второй.sha256 == первый.sha256
    assert вторая_identity != первая_identity
    assert вторая_identity.generation > первая_identity.generation


def test_поднятые_индексы_отвечают_как_построенные(tmp_path, корень_кода):
    """Круг «построить -> сохранить -> поднять» не должен терять данные.

    Особенно важен порядок оглавление-потом-поиск:
    полезная нагрузка документов поиска на диск не пишется и подставляется
    из свежеподнятого оглавления. Мутация, которая перепутает документы
    местами или забудет подставить нагрузку, роняет последний assert —
    `doc.payload` будет либо `None`, либо чужой записью.
    """
    form_xml = (
        корень_кода
        / "Catalogs"
        / "Пример"
        / "Forms"
        / "ФормаЭлемента"
        / "Ext"
        / "Form.xml"
    )
    form_xml.write_text(
        "<Form><Events><Event name=\"OnOpen\">ПриОткрытии</Event></Events>"
        "<ChildItems><Button name=\"Кнопка\"><Events>"
        "<Event name=\"OnClick\">ПриОткрытии</Event>"
        "</Events></Button></ChildItems></Form>",
        encoding="utf-8",
    )
    реестр, источник = _реестр_с_модулями(tmp_path, корень_кода)
    код_на_диске = реестр.modules_dir / "Пример"
    modules_index.сохранить_индексы(реестр, источник.id, _построить_индексы(код_на_диске))

    поднятое = modules_index.поднять_индексы(реестр, источник.id)

    assert поднятое is not None
    assert sorted(з.имя for з in поднятое.оглавление.все()) == [
        "Внутренняя",
        "ПриЗаписи",
        "ПриОткрытии",
        "Сложить",
    ]
    assert поднятое.вызовы.места("Сложить")
    assert поднятое.формы.обработчик(
        "Справочник.Пример.Форма.ФормаЭлемента", "ПриОткрытии"
    ) == ("OnOpen", "OnClick")
    assert [
        (item.элемент, item.событие)
        for item in поднятое.формы.привязки(
            "Справочник.Пример.Форма.ФормаЭлемента", "ПриОткрытии"
        )
    ] == [(None, "OnOpen"), ("Кнопка", "OnClick")]
    попадания = поднятое.поиск.search("сложить числа", limit=5)
    assert попадания and попадания[0].doc.payload.имя == "Сложить"
