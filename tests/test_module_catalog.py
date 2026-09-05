"""Единый неизменяемый каталог модулей и доказательств форм."""

import copy

from pathlib import Path

import pytest

from module_samples import v8_container_bytes
from mcp1c import modules_index
from mcp1c.module_catalog import ModuleCatalog, build_catalog
from mcp1c.module_content import LocatorIdentity, read_bsl


def _write(root: Path, relative: str, payload: bytes | str) -> Path:
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(payload.encode("utf-8") if isinstance(payload, str) else payload)
    return path


def _identity() -> LocatorIdentity:
    return LocatorIdentity("Синтетика:modules", "a" * 64, 7)


def test_плоский_источник_видит_тексты_контейнеры_и_семь_compiled(tmp_path):
    for index in range(7):
        _write(
            tmp_path,
            f"CommonModule.Закрытый{index}.Module",
            v8_container_bytes([("image", b"compiled")]),
        )
    _write(
        tmp_path,
        "Document.Объект.ObjectModule.txt",
        "Процедура ИзФайла()\r\nКонецПроцедуры\r\n",
    )
    _write(
        tmp_path,
        "Document.Объект.Form.Основная.Form",
        v8_container_bytes(
            [
                ("module", "Процедура ИзКонтейнера()\nКонецПроцедуры".encode()),
                ("form", b"{19}"),
            ]
        ),
    )

    catalog = build_catalog(tmp_path, _identity())

    assert catalog.coverage.compiled == 7
    assert catalog.coverage.indexed == 2
    assert set(catalog.entries) >= {
        "Документ.Объект.МодульОбъекта",
        "Документ.Объект.Форма.Основная",
    }
    assert "ИзФайла" in read_bsl(
        tmp_path,
        "Документ.Объект.МодульОбъекта",
        catalog.entries["Документ.Объект.МодульОбъекта"].locator,
    )
    assert "ИзКонтейнера" in read_bsl(
        tmp_path,
        "Документ.Объект.Форма.Основная",
        catalog.entries["Документ.Объект.Форма.Основная"].locator,
    )
    toc = modules_index.Оглавление.построить(tmp_path, каталог=catalog)
    assert {item.имя for item in toc.все()} == {"ИзФайла", "ИзКонтейнера"}
    assert sum(toc.скомпилирован(address) for address in toc.модули) == 7


def test_каждый_кандидат_попадает_ровно_в_одну_именованную_категорию(tmp_path):
    _write(tmp_path, "CommonModule.Текст.Module.txt", "Процедура А() КонецПроцедуры")
    _write(tmp_path, "CommonModule.Закрытый.Module", b"compiled")
    _write(
        tmp_path,
        "CommonForm.Пустая.Form",
        v8_container_bytes([("module", b""), ("form", b"{19}")]),
    )
    _write(
        tmp_path,
        "CommonForm.БезМодуля.Form",
        v8_container_bytes([("form", b"{19}")]),
    )
    _write(tmp_path, "CommonForm.Битая.Form", b"broken")
    _write(tmp_path, "Unknown.Объект.Module.txt", "Процедура Б() КонецПроцедуры")

    catalog = build_catalog(tmp_path, _identity())

    counts = catalog.coverage.as_dict()
    assert counts == {
        "indexed": 1,
        "empty": 1,
        "missing_body": 1,
        "compiled": 1,
        "unknown_address": 1,
        "broken_container": 1,
        "unreadable_body": 0,
        "budget_exceeded": 0,
        "conflict": 0,
    }
    assert catalog.coverage.total_candidates == sum(counts.values())
    assert len(catalog.outcomes) == catalog.coverage.total_candidates
    assert all(outcome.category in counts for outcome in catalog.outcomes)
    assert str(tmp_path) not in repr(catalog)
    assert "Процедура Б" not in repr(catalog)
    unknown = next(p for p in catalog.problems if p.category == "unknown_address")
    assert unknown.address is None and unknown.ordinal > 0


def test_пустые_txt_и_container_сохраняют_локатор_модуля(tmp_path):
    _write(tmp_path, "CommonModule.Пустой.Module.txt", " \r\n")
    _write(
        tmp_path,
        "CommonForm.Пустая.Form",
        v8_container_bytes([("module", b" \r\n"), ("form", b"{19}")]),
    )

    catalog = build_catalog(tmp_path, _identity())

    assert catalog.entries["ОбщийМодуль.Пустой"].locator is not None
    form_locator = catalog.entries["ОбщаяФорма.Пустая"].locator
    assert form_locator is not None and form_locator.kind == "container"


@pytest.mark.parametrize(
    "damage", ["conflict_with_locator", "foreign_locator", "case_changed_locator"]
)
def test_семантически_битый_cache_каталога_становится_miss(tmp_path, damage):
    _write(
        tmp_path,
        "CommonModule.Первый.Module.txt",
        "Процедура Первая() КонецПроцедуры",
    )
    _write(
        tmp_path,
        "CommonModule.Второй.Module.txt",
        "Процедура Вторая() КонецПроцедуры",
    )
    catalog = build_catalog(tmp_path, _identity())
    state = copy.deepcopy(catalog.to_state())
    first = list(state["entries"][0])
    if damage == "conflict_with_locator":
        first[7] = True
    elif damage == "foreign_locator":
        first[2] = state["entries"][1][2]
    else:
        kind, relative, entry = first[2]
        name = first[0].rsplit(".", 1)[-1]
        first[2] = (kind, relative.replace(name, name.lower()), entry)
    state["entries"][0] = tuple(first)

    assert ModuleCatalog.from_state(state, catalog.identity) is None


def test_одинаковые_тела_дедуплицируются_а_разные_дают_конфликт(tmp_path):
    same = "Процедура Одинаковая()\nКонецПроцедуры"
    _write(
        tmp_path,
        "CommonForm.Одинаковая.Form.Module.txt",
        "\ufeff" + same.replace("\n", "\r\n"),
    )
    _write(
        tmp_path,
        "CommonForm.Одинаковая.Form",
        v8_container_bytes([("module", same.encode()), ("form", b"{19}")]),
    )
    _write(tmp_path, "CommonForm.Конфликт.Form.Module.txt", same)
    _write(
        tmp_path,
        "CommonForms/Конфликт/Ext/Form.xml",
        "<Form><Attributes><Attribute name=\"Поле\"/></Attributes></Form>",
    )
    _write(
        tmp_path,
        "CommonForm.Конфликт.Form",
        v8_container_bytes(
            [("module", "Процедура Другая()\nКонецПроцедуры".encode()), ("form", b"{19}")]
        ),
    )

    catalog = build_catalog(tmp_path, _identity())

    equal = catalog.entries["ОбщаяФорма.Одинаковая"]
    conflict = catalog.entries["ОбщаяФорма.Конфликт"]
    assert equal.locator is not None
    assert not equal.conflict
    assert conflict.locator is None
    assert conflict.conflict
    assert catalog.coverage.conflict == 2
    assert [p.address for p in catalog.problems if p.category == "conflict"] == [
        "ОбщаяФорма.Конфликт"
    ]
    form = modules_index.Формы.построить(
        tmp_path, каталог=catalog
    ).состав("ОбщаяФорма.Конфликт")
    assert form is not None and form.структура_доступна


def test_casefold_коллизия_не_выбирает_чужую_структуру_формы(tmp_path):
    body = "Процедура Открыть()\nКонецПроцедуры"
    _write(tmp_path, "CommonForms/Форма/Ext/Form.xml", "<Form/>")
    _write(tmp_path, "CommonForms/Форма/Ext/Form/Module.bsl", body)
    _write(
        tmp_path,
        "CommonForm.форма.Form",
        v8_container_bytes(
            [("module", body.encode()), ("form", b"{19}")]
        ),
    )

    catalog = build_catalog(tmp_path, _identity())
    entry = next(iter(catalog.entries.values()))
    forms = modules_index.Формы.построить(tmp_path, каталог=catalog)

    assert entry.conflict and entry.locator is None
    assert next(
        problem.reason
        for problem in catalog.problems
        if problem.category == "conflict"
    ) == "канонические адреса различаются только регистром"
    assert forms.состав(entry.address).структура_доступна is False


def test_дескриптор_form_xml_form_bin_и_модуль_сливаются_в_одну_форму(tmp_path):
    _write(tmp_path, "Catalogs/Объект/Forms/Основная.xml", "<MetaDataObject/>")
    _write(tmp_path, "Catalogs/Объект/Forms/Основная/Ext/Form.xml", "<Form/>")
    _write(
        tmp_path,
        "Catalogs/Объект/Forms/Основная/Ext/Form.bin",
        v8_container_bytes([("form", b"{19}")]),
    )
    _write(
        tmp_path,
        "Catalogs/Объект/Forms/Основная/Ext/Form/Module.bsl",
        "Процедура Открыть() КонецПроцедуры",
    )

    catalog = build_catalog(tmp_path, _identity())

    assert list(catalog.entries) == ["Справочник.Объект.Форма.Основная"]
    entry = catalog.entries["Справочник.Объект.Форма.Основная"]
    assert entry.is_form
    assert entry.form_evidence == ("descriptor", "form_bin", "form_xml", "module")
    assert entry.locator is not None


def test_filter_criteria_из_tree_получает_канонический_адрес(tmp_path):
    _write(
        tmp_path,
        "FilterCriteria/Отбор/Forms/Основная.xml",
        "<MetaDataObject/>",
    )
    _write(
        tmp_path,
        "FilterCriteria/Отбор/Forms/Основная/Ext/Form.xml",
        "<Form/>",
    )
    _write(
        tmp_path,
        "FilterCriteria/Отбор/Forms/Основная/Ext/Form.bin",
        v8_container_bytes([("form", b"{19}")]),
    )
    _write(
        tmp_path,
        "FilterCriteria/Отбор/Forms/Основная/Ext/Form/Module.bsl",
        "Процедура Открыть() КонецПроцедуры",
    )

    catalog = build_catalog(tmp_path, _identity())

    assert list(catalog.entries) == ["КритерийОтбора.Отбор.Форма.Основная"]
    assert catalog.entries[
        "КритерийОтбора.Отбор.Форма.Основная"
    ].form_evidence == ("descriptor", "form_bin", "form_xml", "module")


def test_старый_корень_не_публикует_частичные_хранилища_настроек(tmp_path):
    _write(
        tmp_path,
        "SettingsStorages/Настройки/Ext/ManagerModule.bsl",
        "Процедура Сохранить() КонецПроцедуры",
    )
    _write(
        tmp_path,
        "SettingsStorages/Настройки/Forms/Сохранение/Ext/Form/Module.bsl",
        "Процедура Записать() КонецПроцедуры",
    )
    _write(
        tmp_path,
        "Catalogs/Объект/Ext/ObjectModule.bsl",
        "Процедура Поддержанная() КонецПроцедуры",
    )

    catalog = build_catalog(tmp_path, _identity())

    assert list(catalog.entries) == ["Справочник.Объект.МодульОбъекта"]
    assert catalog.coverage.total_candidates == 1
    assert not catalog.problems


def test_каталог_неизменяем_и_порядок_не_зависит_от_создания(tmp_path):
    _write(tmp_path, "CommonModule.Б.Module.txt", "Процедура Б() КонецПроцедуры")
    _write(tmp_path, "CommonModule.А.Module.txt", "Процедура А() КонецПроцедуры")

    catalog = build_catalog(tmp_path, _identity())

    assert list(catalog.entries) == ["ОбщийМодуль.А", "ОбщийМодуль.Б"]
    with pytest.raises(TypeError):
        catalog.entries["ОбщийМодуль.В"] = catalog.entries["ОбщийМодуль.А"]


def test_четыре_индекса_не_обходят_диск_после_снимка(tmp_path, monkeypatch):
    _write(
        tmp_path,
        "CommonModule.Модуль.Module.txt",
        "Процедура Вызвать() Экспорт\nКонецПроцедуры",
    )
    _write(
        tmp_path,
        "CommonForm.Форма.Form",
        v8_container_bytes(
            [("module", b""), ("form", b"{19}")]
        ),
    )
    catalog = build_catalog(tmp_path, _identity())

    monkeypatch.setattr(
        Path,
        "rglob",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("повторный обход диска")
        ),
    )

    toc = modules_index.Оглавление.построить(tmp_path, каталог=catalog)
    calls = modules_index.Вызовы.построить(
        tmp_path, toc, каталог=catalog
    )
    forms = modules_index.Формы.построить(tmp_path, каталог=catalog)
    search = modules_index.построить_поиск(
        toc, tmp_path, каталог=catalog
    )

    assert calls is not None
    assert forms.состав("ОбщаяФорма.Форма") is not None
    assert search.search("вызвать")


@pytest.mark.parametrize("marker", [19, 20, 23, 25, 26, 27])
def test_контейнерная_форма_с_известным_маркером_остаётся_частичной(
    tmp_path, marker
):
    _write(
        tmp_path,
        f"CommonForm.Маркер{marker}.Form",
        v8_container_bytes(
            [("module", b""), ("form", f'{{{marker},"значение"}}'.encode())]
        ),
    )
    catalog = build_catalog(tmp_path, _identity())

    forms = modules_index.Формы.построить(tmp_path, каталог=catalog)
    form = forms.состав(f"ОбщаяФорма.Маркер{marker}")

    assert form is not None
    assert form.структура_доступна
    assert form.структура_частична
    assert form.маркер == marker
    assert form.реквизиты == []
    assert form.элементы == []
    assert form.события == {}
    assert forms.частичных == 1

    assert forms.известных_неполных == 1
    assert forms.неизвестных_маркеров == 0


def test_неизвестный_маркер_имеет_отдельную_диагностику(tmp_path):
    _write(
        tmp_path,
        "CommonForm.Новая.Form",
        v8_container_bytes([("module", b""), ("form", b"{99}")]),
    )
    catalog = build_catalog(tmp_path, _identity())

    forms = modules_index.Формы.построить(tmp_path, каталог=catalog)

    assert forms.частичных == 1
    assert forms.неизвестных_маркеров == 1
    assert [(problem.категория, problem.маркер) for problem in forms.проблемы] == [
        ("unknown_marker", 99)
    ]


def test_два_контейнерных_доказательства_считают_форму_а_не_файлы(tmp_path):
    payload = v8_container_bytes([("module", b""), ("form", b"{19}")])
    _write(tmp_path, "Catalogs/Объект/Forms/Основная/Ext/Form.bin", payload)
    _write(tmp_path, "Catalog.Объект.Form.Основная.Form", payload)
    catalog = build_catalog(tmp_path, _identity())

    forms = modules_index.Формы.построить(tmp_path, каталог=catalog)

    assert forms.частичных == 1
    assert forms.известных_неполных == 1
    assert [
        problem.категория
        for problem in forms.проблемы
        if problem.категория == "known_marker_semantics_deferred"
    ] == ["known_marker_semantics_deferred"]


def test_самостоятельный_дескриптор_публикует_только_доказанные_свойства(
    tmp_path,
):
    _write(
        tmp_path,
        "CommonForms/Самостоятельная.xml",
        '<MetaDataObject xmlns="http://v8.1c.ru/8.3/MDClasses" '
        'xmlns:v8="http://v8.1c.ru/8.1/data/core">'
        '<CommonForm uuid="00000000-0000-0000-0000-000000000001">'
        "<Properties><Name>Самостоятельная</Name>"
        "<Synonym><v8:item><v8:lang>ru</v8:lang>"
        "<v8:content>Самостоятельная форма</v8:content></v8:item></Synonym>"
        "<FormType>Managed</FormType></Properties>"
        "</CommonForm></MetaDataObject>",
    )
    catalog = build_catalog(tmp_path, _identity())

    forms = modules_index.Формы.построить(tmp_path, каталог=catalog)
    form = forms.состав("ОбщаяФорма.Самостоятельная")

    assert form is not None
    assert form.структура_доступна
    assert form.структура_частична
    assert form.идентификатор == "00000000-0000-0000-0000-000000000001"
    assert form.имя == "Самостоятельная"
    assert form.синоним == "Самостоятельная форма"
    assert form.тип == "Managed"
    assert form.реквизиты == []
    assert form.элементы == []
    assert form.события == {}
    assert forms.частичных == 1

    cache = tmp_path / "descriptor-forms.marshal"
    forms.записать(cache)
    restored = modules_index.Формы.прочитать(cache).состав(
        "ОбщаяФорма.Самостоятельная"
    )
    assert restored is not None
    assert (
        restored.идентификатор,
        restored.имя,
        restored.синоним,
        restored.тип,
    ) == (
        "00000000-0000-0000-0000-000000000001",
        "Самостоятельная",
        "Самостоятельная форма",
        "Managed",
    )


def test_модуль_формы_без_структуры_не_называется_полностью_прочитанным(
    tmp_path,
):
    _write(
        tmp_path,
        "CommonForm.ТолькоМодуль.Form.Module.txt",
        "Процедура Открыть()\nКонецПроцедуры",
    )
    catalog = build_catalog(tmp_path, _identity())

    forms = modules_index.Формы.построить(tmp_path, каталог=catalog)
    form = forms.состав("ОбщаяФорма.ТолькоМодуль")

    assert form is not None
    assert not form.структура_доступна
    assert forms.непрочитанных == 1
    assert [problem.категория for problem in forms.проблемы] == [
        "form_structure_missing"
    ]


def test_битый_контейнер_не_роняет_соседнюю_форму_и_считается_один_раз(
    tmp_path,
):
    _write(tmp_path, "CommonForm.Битая.Form", b"broken")
    _write(
        tmp_path,
        "CommonForm.Целая.Form",
        v8_container_bytes([("module", b""), ("form", b"{19}")]),
    )
    catalog = build_catalog(tmp_path, _identity())

    forms = modules_index.Формы.построить(tmp_path, каталог=catalog)

    assert forms.состав("ОбщаяФорма.Битая") is not None
    assert forms.состав("ОбщаяФорма.Целая").структура_частична
    assert forms.битых == 1
    assert forms.непрочитанных == 1
    assert forms.частичных == 1


def test_диагностика_формы_переживает_локальный_cache_roundtrip(tmp_path):
    _write(
        tmp_path,
        "CommonForm.Новая.Form",
        v8_container_bytes([("module", b""), ("form", b"{99}")]),
    )
    catalog = build_catalog(tmp_path, _identity())
    cache = tmp_path / "forms.marshal"
    modules_index.Формы.построить(tmp_path, каталог=catalog).записать(cache)

    restored = modules_index.Формы.прочитать(cache)
    form = restored.состав("ОбщаяФорма.Новая")

    assert form is not None and form.маркер == 99
    assert form.структура_частична
    assert restored.частичных == 1
    assert restored.неизвестных_маркеров == 1
    assert restored.проблемы == (
        modules_index.ПроблемаФормы(
            "unknown_marker", "ОбщаяФорма.Новая", "маркер form не поддержан", 99
        ),
    )
