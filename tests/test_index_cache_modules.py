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
from array import array
from pathlib import Path

import pytest

from conftest import build_configuration, modules_configuration_xml, write_export
from module_samples import v8_container_bytes
from mcp1c import index_cache, modules_index, tools
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


def _испортить_оглавление(state: dict) -> None:
    toc = state["toc"]
    toc["модуль"] = array("i", [999] * len(toc["имена"])).tobytes()


def _испортить_вызовы(state: dict) -> None:
    key = next(iter(state["по_имени"]))
    state["по_имени"][key] = array("i", [0, 1]).tobytes()


def _испортить_формы(state: dict) -> None:
    state["флаги"] = b""


def _подменить_модули_оглавления(state: dict) -> None:
    state["toc"]["модули"][0] = "ОбщийМодуль.Чужой"


def _подменить_модули_вызовов(state: dict) -> None:
    state["модули"][0] = "ОбщийМодуль.Чужой"


def _подменить_модули_форм(state: dict) -> None:
    state["модули"][0] = "Справочник.Чужой.Форма.Чужая"


def _добавить_неизвестный_флаг_формы(state: dict) -> None:
    флаги = array("i")
    флаги.frombytes(state["флаги"])
    флаги[0] |= 8
    state["флаги"] = флаги.tobytes()


def _добавить_неизвестный_флаг_оглавления(state: dict) -> None:
    флаги = array("i")
    флаги.frombytes(state["toc"]["флаги"])
    флаги[0] |= 1 << 8
    state["toc"]["флаги"] = флаги.tobytes()


def _добавить_отрицательный_маркер_формы(state: dict) -> None:
    маркеры = array("i")
    маркеры.frombytes(state["маркеры"])
    маркеры[0] = -2
    state["маркеры"] = маркеры.tobytes()


def _испортить_поиск(state: dict) -> None:
    документы = array("i")
    документы.frombytes(state["token_docs"])
    документы[0] = 999
    state["token_docs"] = документы.tobytes()


def _подменить_документ_поиска(state: dict) -> None:
    state["doc_ids"][0] = "ОбщийМодуль.Чужой::Чужая"


def _рассогласовать_счётчик_форм(state: dict) -> None:
    state["неизвестных_маркеров"] = 1


def _подменить_имя_вызова(state: dict) -> None:
    прежнее = next(iter(state["по_имени"]))
    state["по_имени"]["чужаяпроцедура"] = state["по_имени"].pop(прежнее)


def _объявить_обычный_модуль_скомпилированным(state: dict) -> None:
    state["toc"]["скомпилированные"].append(state["toc"]["модули"][0])


def _подменить_цель_вызова_на_недоказанный_self(state: dict) -> None:
    for key, raw in state["по_имени"].items():
        posting = array("i")
        posting.frombytes(raw)
        for offset in range(0, len(posting), 3):
            caller, _line, target = posting[offset : offset + 3]
            if target not in (-1, caller):
                posting[offset + 2] = caller
                state["по_имени"][key] = posting.tobytes()
                return
    raise AssertionError("в синтетическом корпусе нет разрешённого внешнего вызова")


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


@pytest.mark.parametrize(
    ("kind", "mutate"),
    [
        ("modules-toc", _испортить_оглавление),
        ("modules-calls", _испортить_вызовы),
        ("modules-forms", _испортить_формы),
        ("modules-toc", _подменить_модули_оглавления),
        ("modules-calls", _подменить_модули_вызовов),
        ("modules-forms", _подменить_модули_форм),
        ("modules-forms", _добавить_неизвестный_флаг_формы),
        ("modules-toc", _добавить_неизвестный_флаг_оглавления),
        ("modules-forms", _добавить_отрицательный_маркер_формы),
        ("modules-search", _испортить_поиск),
        ("modules-search", _подменить_документ_поиска),
        ("modules-forms", _рассогласовать_счётчик_форм),
        ("modules-calls", _подменить_имя_вызова),
        ("modules-toc", _объявить_обычный_модуль_скомпилированным),
        ("modules-calls", _подменить_цель_вызова_на_недоказанный_self),
    ],
)
def test_семантически_битый_кэш_индекса_становится_промахом(
    tmp_path, корень_кода, kind, mutate
):
    реестр, источник = _реестр_с_модулями(tmp_path, корень_кода)
    signature = (
        f"{источник.sha256}:selection={источник.selection_version}"
    )
    path = реестр._cache_path(источник.id, kind)
    state = index_cache.load_blob(
        path, source_sha256=signature, kind=kind
    )
    assert isinstance(state, dict)
    mutate(state)
    index_cache.save_blob(
        state, path, source_sha256=signature, kind=kind
    )

    assert modules_index.поднять_индексы(реестр, источник.id) is None


def test_поисковый_кэш_с_неверным_top_level_становится_промахом(
    tmp_path, корень_кода
):
    реестр, источник = _реестр_с_модулями(tmp_path, корень_кода)
    signature = f"{источник.sha256}:selection={источник.selection_version}"
    path = реестр._cache_path(источник.id, "modules-search")
    index_cache.save_blob(
        [], path, source_sha256=signature, kind="modules-search"
    )

    assert modules_index.поднять_индексы(реестр, источник.id) is None


def test_warm_кэш_принимает_квалифицированный_self_вызов_общего_модуля(
    tmp_path, корень_кода
):
    module = корень_кода / "CommonModules" / "ОбщийПример" / "Ext" / "Module.bsl"
    module.write_text(
        module.read_text(encoding="utf-8")
        + "\nПроцедура ВызватьЧужоеИмя()\n"
        + "\tОбщийПример.ПриЗаписи();\n"
        + "КонецПроцедуры\n",
        encoding="utf-8",
    )
    реестр, источник = _реестр_с_модулями(tmp_path, корень_кода)

    поднятое = modules_index.поднять_индексы(реестр, источник.id)

    assert поднятое is not None
    места = поднятое.вызовы.места("ПриЗаписи")
    assert any(
        место.модуль == "ОбщийМодуль.ОбщийПример"
        and место.цель == "ОбщийМодуль.ОбщийПример"
        for место in места
    )


def test_cold_warm_и_пересборка_битого_кэша_сохраняют_всё_покрытие(
    tmp_path, корень_кода, monkeypatch
):
    for index in range(25):
        form = (
            корень_кода
            / "CommonForms"
            / f"Форма{index:02d}"
            / "Ext"
            / "Form.xml"
        )
        form.parent.mkdir(parents=True)
        form.write_text("<Form>", encoding="utf-8")
    реестр, источник = _реестр_с_модулями(tmp_path, корень_кода)
    реестр.save()
    ожидаемое = tools.sources_snapshot(реестр).code
    signature = f"{источник.sha256}:selection={источник.selection_version}"
    cached_forms = index_cache.load_blob(
        реестр._cache_path(источник.id, "modules-forms"),
        source_sha256=signature,
        kind="modules-forms",
    )
    assert len(cached_forms["проблемы"]) == 20
    assert dict(cached_forms["problem_counts"])["form_xml_unreadable"] == 25
    assert len(cached_forms["object_problems"]) == 25

    warm = Registry(реестр.data_dir)

    def нельзя_строить(*_args, **_kwargs):
        raise AssertionError("warm-кэш не должен перестраиваться")

    monkeypatch.setattr(warm, "_построить_индекс_кода", нельзя_строить)
    assert warm.startup() == []
    assert tools.sources_snapshot(warm).code == ожидаемое

    forms_cache = реестр._cache_path(источник.id, "modules-forms")
    forms_cache.write_bytes("повреждено".encode())
    cold = Registry(реестр.data_dir)
    assert cold.startup() == []
    assert cold.wait_for_module_builds(timeout=10)
    assert tools.sources_snapshot(cold).code == ожидаемое
    coverage = tools.sources_snapshot(cold).code[0].coverage
    assert dict(coverage.problem_categories)["form_xml_unreadable"] == 25
    assert len(coverage.problems) == 20
    assert coverage.problems_omitted == 5


def test_кэш_каталога_хранит_агрегат_и_не_больше_двадцати_строк(
    tmp_path, корень_кода
):
    container = v8_container_bytes([("module", b""), ("form", b"{19}")])
    for index in range(25):
        (корень_кода / f"Unknown.Форма{index:02d}.Form").write_bytes(container)
    реестр, источник = _реестр_с_модулями(tmp_path, корень_кода)
    signature = f"{источник.sha256}:selection={источник.selection_version}"

    toc = index_cache.load_blob(
        реестр._cache_path(источник.id, "modules-toc"),
        source_sha256=signature,
        kind="modules-toc",
    )
    catalog = toc["catalog"]

    assert len(catalog["problems"]) == 20
    assert dict(catalog["problem_counts"])["unknown_address"] == 25
    assert catalog["object_problems"] == []


def test_warm_get_object_видит_все_локальные_причины_за_общим_лимитом(
    tmp_path, корень_кода, monkeypatch
):
    container = v8_container_bytes([("module", b""), ("form", b"{,19}")])
    for index in range(25):
        (корень_кода / f"Catalog.Контрагенты.Form.Форма{index:02d}.Form").write_bytes(
            container
        )
    реестр, _источник = _реестр_с_модулями(tmp_path, корень_кода)
    реестр.save()
    warm = Registry(реестр.data_dir)

    def нельзя_строить(*_args, **_kwargs):
        raise AssertionError("warm-кэш не должен перестраиваться")

    monkeypatch.setattr(warm, "_построить_индекс_кода", нельзя_строить)
    assert warm.startup() == []

    answer = tools.get_object(
        warm, "Справочник.Контрагенты", config="Пример", detail="full"
    )
    assert answer.count("[invalid_syntax]") == 25
    assert "Справочник.Контрагенты.Форма.Форма24" in answer


def test_read_only_кэш_не_мешает_холодной_диагностике(
    tmp_path, корень_кода, monkeypatch
):
    broken = корень_кода / "CommonForms" / "Ограниченная" / "Ext" / "Form.xml"
    broken.parent.mkdir(parents=True)
    broken.write_text("<Form>", encoding="utf-8")
    реестр, источник = _реестр_с_модулями(tmp_path, корень_кода)
    реестр.save()
    ожидаемое = tools.sources_snapshot(реестр).code
    for kind in реестр.CACHE_KINDS[источник.kind]:
        реестр._cache_path(источник.id, kind).unlink(missing_ok=True)

    monkeypatch.setattr(index_cache, "save_blob", lambda *_args, **_kwargs: None)
    cold = Registry(реестр.data_dir)
    assert cold.startup() == []
    assert cold.wait_for_module_builds(timeout=10)

    assert tools.sources_snapshot(cold).code == ожидаемое
    assert not any(
        path.is_file()
        for path in (
            cold._cache_path(источник.id, kind)
            for kind in cold.CACHE_KINDS[источник.kind]
        )
    )


def test_кэш_не_содержит_тела_сигнатуры_или_сырой_form_xml(
    tmp_path, корень_кода
):
    module = (
        корень_кода
        / "CommonModules"
        / "ОбщийПример"
        / "Ext"
        / "Module.bsl"
    )
    module.write_text(
        "Процедура Проверить(ПарамСекрет) Экспорт\n"
        "// BODY_SENTINEL\n"
        "КонецПроцедуры\n",
        encoding="utf-8",
    )
    form = корень_кода / "CommonForms" / "БезСырья" / "Ext" / "Form.xml"
    form.parent.mkdir(parents=True)
    form.write_text(
        "<Form><Unused>RAW_FORM_SENTINEL</Unused></Form>", encoding="utf-8"
    )
    реестр, источник = _реестр_с_модулями(tmp_path, корень_кода)
    signature = f"{источник.sha256}:selection={источник.selection_version}"

    states = [
        index_cache.load_blob(
            реестр._cache_path(источник.id, kind),
            source_sha256=signature,
            kind=kind,
        )
        for kind in реестр.CACHE_KINDS[источник.kind]
    ]
    cached = repr(states)

    assert "BODY_SENTINEL" not in cached
    assert "ПарамСекрет" not in cached
    assert "RAW_FORM_SENTINEL" not in cached
    assert str(корень_кода) not in cached


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
