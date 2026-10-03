"""Что видит агент в публичной карточке с версионной навигацией."""

from __future__ import annotations

import gzip
import json

import pytest

from mcp1c.registry import Registry, RegistryError
from mcp1c.store import save_syntax
from mcp1c.syntax_model import SyntaxIndex, SyntaxItem, SyntaxLink, SyntaxLinkSnapshot
from mcp1c.tools import get_syntax, syntax_navigation_data

from conftest import build_configuration, write_export


OWNER_ID = "objects/Query/Item"


def _owner(platform: str, *links: SyntaxLink) -> SyntaxItem:
    return SyntaxItem(
        id=OWNER_ID,
        kind="object",
        name_ru="Запрос",
        name_en="Query",
        description="Объект для выполнения запросов.",
        link_snapshots=[SyntaxLinkSnapshot(platform=platform, source_id=OWNER_ID, links=list(links))],
    )


def _method(name: str, *, id: str = "", since: str = "") -> SyntaxItem:
    return SyntaxItem(
        id=id or f"objects/Query/methods/{name}",
        kind="method",
        name_ru=name,
        parent_ru="Запрос",
        description=f"Метод {name}.",
        since=since,
    )


def _link(name: str, *, target_id: str = "", href: str = "") -> SyntaxLink:
    return SyntaxLink(
        section="methods",
        label=name,
        href=href or f"methods/{name}.html",
        target_id=target_id or f"objects/Query/methods/{name}",
    )


def _registry(tmp_path, snapshots: list[tuple[str, list[SyntaxItem]]], config_platform: str = ""):
    incoming = tmp_path / "incoming"
    incoming.mkdir()
    registry = Registry(tmp_path / "data")
    for platform, items in snapshots:
        index = SyntaxIndex(platforms=[platform], source=f"synthetic-{platform}")
        for item in items:
            index.add(item)
        registry.add_syntax(save_syntax(index, incoming / f"syntax-{platform}.json.gz"))
    if config_platform:
        config = build_configuration()
        config.platform = config_platform
        registry.add_configuration(write_export(incoming, config))
        return registry, config.name
    return registry, None


def test_full_даёт_точные_адреса_только_из_справки_версии_конфигурации(tmp_path):
    names = ("Первый", "Второй", "Третий", "Четвертый")
    registry, config = _registry(
        tmp_path,
        [
            ("8.3.5", [_owner("8.3.5", *(_link(name) for name in names)), *(_method(name) for name in names)]),
            (
                "8.3.19",
                [_owner("8.3.19", *(_link(name) for name in (*names, "Пятый"))),
                 *(_method(name) for name in names), _method("Пятый", since="8.3.8")],
            ),
        ],
        "8.3.5",
    )

    full = get_syntax(registry, "Запрос", config=config, detail="full")
    fields = get_syntax(registry, "Запрос", config=config, detail="fields")

    for name in names:
        assert f"`Запрос.{name}`" in full
    assert "Запрос.Пятый" not in full
    assert "справка 8.3.5" in full
    assert "## Навигация" not in fields
    assert "Запрос.Первый" not in fields


def test_без_конфигурации_ответ_обозначает_источник_и_непроверенную_доступность(tmp_path):
    registry, _ = _registry(
        tmp_path,
        [
            ("8.3.5", [_owner("8.3.5", _link("Первый")), _method("Первый")]),
            ("8.3.19", [_owner("8.3.19", _link("Второй")), _method("Второй")]),
        ],
    )

    answer = get_syntax(registry, "Запрос", detail="full")

    assert "справка 8.3.19" in answer
    assert "`Запрос.Второй`" in answer
    assert "`Запрос.Первый`" not in answer
    assert "доступность целей не проверена" in answer.lower()


def test_между_загруженными_справками_состав_не_объявляется_точным(tmp_path):
    registry, config = _registry(
        tmp_path,
        [
            ("8.3.5", [_owner("8.3.5", _link("Первый")), _method("Первый")]),
            ("8.3.19", [_owner("8.3.19", _link("Первый"), _link("Второй")),
                        _method("Первый"), _method("Второй", since="8.3.8")]),
        ],
        "8.3.10",
    )

    answer = get_syntax(registry, "Запрос", config=config, detail="full")

    assert "нет подтверждённого состава ссылок" in answer.lower()
    assert "`Запрос.Второй`" not in answer


def test_неразрешённая_и_неоднозначная_цели_не_выдаются_за_точный_переход(tmp_path):
    ambiguous = "objects/Query/methods/A"
    missing = SyntaxLink(
        section="methods", label="НетСтраницы", href="methods/Missing.html", target_id=""
    )
    registry, config = _registry(
        tmp_path,
        [(
            "8.3.19",
            [
                _owner("8.3.19", _link("Одинаковый", target_id=ambiguous), missing),
                _method("Одинаковый", id=ambiguous),
                _method("Одинаковый", id="objects/Query/methods/B"),
            ],
        )],
        "8.3.19",
    )

    answer = get_syntax(registry, "Запрос", config=config, detail="full")

    assert "публичный адрес неоднозначен" in answer.lower()
    assert "цель не разрешена" in answer.lower()
    assert "`Запрос.Одинаковый`" not in answer
    assert "`Запрос.НетСтраницы`" not in answer


def test_повтор_одного_href_выводится_один_раз(tmp_path):
    repeated = _link("Выполнить")
    registry, config = _registry(
        tmp_path,
        [("8.3.19", [_owner("8.3.19", repeated, _link("Выполнить")), _method("Выполнить")])],
        "8.3.19",
    )

    answer = get_syntax(registry, "Запрос", config=config, detail="full")

    assert answer.count("- `Запрос.Выполнить`") == 1


def test_разные_разделы_одной_цели_сохраняют_обе_подписанные_связи(tmp_path):
    method = _link("Выполнить")
    related = SyntaxLink(
        section="see_also", label="Выполнение запроса",
        href="methods/Выполнить.html", target_id=method.target_id,
    )
    registry, config = _registry(
        tmp_path,
        [("8.3.19", [_owner("8.3.19", method, related, method), _method("Выполнить")])],
        "8.3.19",
    )

    answer = get_syntax(registry, "Запрос", config=config, detail="full")

    assert answer.count("- `Запрос.Выполнить`") == 2
    assert "### Методы" in answer
    assert "### См. также" in answer
    assert "— Выполнение запроса" in answer


def test_старый_индекс_без_снимков_просит_повторный_разбор(tmp_path):
    registry, config = _registry(
        tmp_path,
        [(
            "8.3.19",
            [SyntaxItem(id=OWNER_ID, kind="object", name_ru="Запрос", description="Объект запроса.")],
        )],
        "8.3.19",
    )

    answer = get_syntax(registry, "Запрос", config=config, detail="full")

    assert "повторный разбор" in answer.lower()
    assert "ссылок в этой карточке нет" not in answer.lower()


def test_старая_справка_в_смешанном_индексе_просит_повторный_разбор(tmp_path):
    incoming = tmp_path / "incoming"
    incoming.mkdir()
    registry = Registry(tmp_path / "data")
    old = SyntaxIndex(platforms=["8.3.5"], source="synthetic-8.3.5")
    old.add(_owner("8.3.5", _link("Старый")))
    old_path = save_syntax(old, incoming / "syntax-8.3.5.json.gz")
    with gzip.open(old_path, "rt", encoding="utf-8") as stream:
        payload = json.load(stream)
    payload["items"][0].pop("link_snapshots")
    payload["items"][0].pop("empty_link_mask")
    with gzip.open(old_path, "wt", encoding="utf-8") as stream:
        json.dump(payload, stream)
    registry.add_syntax(old_path)

    current = SyntaxIndex(platforms=["8.3.27"], source="synthetic-8.3.27")
    current.add(_owner("8.3.27", _link("Новый")))
    current.add(_method("Новый", since="8.3.27"))
    registry.add_syntax(save_syntax(current, incoming / "syntax-8.3.27.json.gz"))
    config = build_configuration()
    config.platform = "8.3.5"
    registry.add_configuration(write_export(incoming, config))
    current_config = build_configuration("НоваяКонфигурация")
    current_config.platform = "8.3.27"
    registry.add_configuration(write_export(incoming, current_config))
    missing_config = build_configuration("ПромежуточнаяКонфигурация")
    missing_config.platform = "8.3.19"
    registry.add_configuration(write_export(incoming, missing_config))

    answer = get_syntax(registry, "Запрос", config=config.name, detail="full")
    navigation = syntax_navigation_data(registry, "Запрос", config=config.name)
    latest = syntax_navigation_data(registry, "Запрос", config=current_config.name)
    missing = syntax_navigation_data(registry, "Запрос", config=missing_config.name)

    assert "повторный разбор" in answer.lower()
    assert "Запрос.Новый" not in answer
    assert navigation[0]["state"] == "legacy"
    assert navigation[0]["items"] == []
    assert latest[0]["state"] == "known"
    assert latest[0]["platform"] == "8.3.27"
    assert missing[0]["state"] == "unknown"


def test_короткое_имя_показывает_варианты_объекта_и_их_навигацию(tmp_path):
    shell = SyntaxItem(
        id="objects/Query/Shell",
        kind="object",
        name_ru="Запрос",
        description="Обзорная карточка запроса.",
        link_snapshots=[SyntaxLinkSnapshot(platform="8.3.19", source_id="objects/Query/Shell")],
    )
    linked = _owner("8.3.19", _link("Выполнить"))
    unrelated = SyntaxItem(
        id="objects/Other/properties/Query",
        kind="property",
        name_ru="Запрос",
        parent_ru="ДругойОбъект",
        description="Свойство другого объекта.",
    )
    registry, _ = _registry(
        tmp_path,
        [("8.3.19", [shell, linked, unrelated, _method("Выполнить")])],
    )

    answer = get_syntax(registry, "Запрос", detail="full")

    assert "Одноимённые варианты: 2" in answer
    assert "Публичный адрес вариантов совпадает" in answer
    assert "## Вариант 1 из 2" in answer
    assert "## Вариант 2 из 2" in answer
    assert "`Запрос.Выполнить`" in answer
    assert "ДругойОбъект.Запрос" not in answer
    assert "Повторите вызов с адресом из списка" not in answer


def test_полная_карточка_ограничивает_список_связей_и_даёт_продолжение(tmp_path):
    names = [f"Метод{i:02d}" for i in range(51)]
    registry, config = _registry(
        tmp_path,
        [("8.3.19", [_owner("8.3.19", *(_link(name) for name in names)),
                       *(_method(name) for name in names)])],
        "8.3.19",
    )

    full = get_syntax(registry, "Запрос", config=config, detail="full")
    second = get_syntax(
        registry, "Запрос", config=config, detail="links", links_offset=50
    )

    assert "`Запрос.Метод49`" in full
    assert "`Запрос.Метод50`" not in full
    assert "Связи 1–50 из 51" in full
    assert "`detail=links`, `links_offset=50`" in full
    assert "`Запрос.Метод49`" not in second
    assert "`Запрос.Метод50`" in second
    assert "Связи 51–51 из 51" in second
    assert "Следующая страница" not in second


@pytest.mark.parametrize(
    ("detail", "offset"),
    [("links", -1), ("full", 1), ("fields", 1)],
)
def test_некорректное_смещение_связей_отклоняется(tmp_path, detail, offset):
    registry, _ = _registry(
        tmp_path, [("8.3.19", [_owner("8.3.19", _link("Выполнить")), _method("Выполнить")])]
    )

    with pytest.raises(RegistryError, match="links_offset"):
        get_syntax(registry, "Запрос", detail=detail, links_offset=offset)


def _section(id: str, name: str, **content) -> SyntaxItem:
    """Страница раздела дерева справки: `objects/catalog2/catalog259`.

    Разбор принимает её за объект, хотя это папка оглавления без описания.
    """
    return SyntaxItem(id=id, kind="object", name_ru=name, **content)


def test_пустой_раздел_справки_не_считается_вариантом(tmp_path):
    # Справка 8.3.26 на живых данных 2026-10-03: `get_syntax("Запрос", full)`
    # давал «Одноимённые варианты: 2», и первый вариант был пуст — это раздел
    # оглавления «Запрос», а не второй тип платформы. Перейти к нему нечем.
    empty = _section("objects/catalog2/catalog259", "Запрос")
    registry, _ = _registry(
        tmp_path,
        [("8.3.19", [empty, _owner("8.3.19", _link("Выполнить")), _method("Выполнить")])],
    )

    answer = get_syntax(registry, "Запрос", detail="full")

    assert "Одноимённые варианты" not in answer
    assert "Вариант 1 из" not in answer
    assert "Объект для выполнения запросов." in answer
    assert "`Запрос.Выполнить`" in answer


def test_пустой_раздел_старой_справки_с_суффиксом_версии_отсеивается(tmp_path):
    # Слияние справок даёт разделу старой версии ID `…/catalogN@<версия>`,
    # когда его путь уже занят: в слитом индексе 2026-10-03 таких два.
    empty = _section("objects/catalog2/catalog259@8.3.5.1570", "Запрос")
    registry, _ = _registry(
        tmp_path,
        [("8.3.19", [empty, _owner("8.3.19", _link("Выполнить")), _method("Выполнить")])],
    )

    answer = get_syntax(registry, "Запрос", detail="full")

    assert "Одноимённые варианты" not in answer
    assert "Объект для выполнения запросов." in answer


def test_раздел_справки_с_примером_остаётся_вариантом(tmp_path):
    # Шесть страниц разделов в справке 2026-10-03 несут пример кода («Диаграмма»,
    # «Константы»): выбросить их значит потерять содержимое.
    sample = _section(
        "objects/catalog63/catalog247", "Запрос",
        examples=["Результат = Запрос.Выполнить();"],
    )
    registry, _ = _registry(
        tmp_path,
        [("8.3.19", [sample, _owner("8.3.19", _link("Выполнить")), _method("Выполнить")])],
    )

    answer = get_syntax(registry, "Запрос", detail="full")

    assert "Одноимённые варианты: 2" in answer
    assert "Результат = Запрос.Выполнить();" in answer


def test_одинокий_раздел_справки_по_имени_по_прежнему_виден(tmp_path):
    registry, _ = _registry(
        tmp_path,
        # Справку без единого описания реестр отвергает, поэтому рядом
        # нужен хоть один описанный элемент с другим именем.
        [("8.3.19", [_section("objects/catalog2/catalog259", "Запрос"), _method("Выполнить")])],
    )

    answer = get_syntax(registry, "Запрос", detail="full")

    assert "Запрос" in answer
    assert "нет элемента" not in answer


def test_ссылка_на_элемент_рядом_с_пустым_разделом_не_неоднозначна(tmp_path):
    builder = SyntaxItem(
        id="objects/Builder",
        kind="object",
        name_ru="Построитель",
        description="Строит запрос.",
        link_snapshots=[SyntaxLinkSnapshot(
            platform="8.3.19", source_id="objects/Builder",
            links=[_link("Запрос", target_id=OWNER_ID, href="Query.html")],
        )],
    )
    registry, _ = _registry(
        tmp_path,
        [("8.3.19", [builder, _section("objects/catalog2/catalog259", "Запрос"),
                     _owner("8.3.19")])],
    )

    (page,) = syntax_navigation_data(registry, "Построитель")

    assert [(entry["address"], entry["status"]) for entry in page["items"]] == [
        ("Запрос", "ready")
    ]


def test_пустой_раздел_не_заслоняет_недоступность_в_версии(tmp_path):
    # Справка 8.3.5 на живых данных: «Планировщик» появился в 8.3.6, рядом
    # два пустых раздела оглавления с тем же именем — и ответ состоял из двух
    # пустых «вариантов» вместо сообщения о версии.
    planner = SyntaxItem(
        id="objects/catalog63/catalog2269/Planner", kind="object",
        name_ru="Планировщик", description="Представляет собой планировщик.",
        since="8.3.6",
    )
    registry, config = _registry(
        tmp_path,
        [("8.3.19", [planner, _section("objects/catalog2/catalog2284", "Планировщик"),
                     _section("objects/catalog63/catalog2269", "Планировщик")])],
        "8.3.5",
    )

    answer = get_syntax(registry, "Планировщик", config=config, detail="full")

    assert "Одноимённые варианты" not in answer
    assert "8.3.6" in answer
