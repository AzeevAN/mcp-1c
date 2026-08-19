"""Граф связей на дашборде: окрестность, раскладка, страница.

Проверяется то, что ломается молча. Красота картинки тестами не ловится — её
смотрят глазами; а вот «обрезали и не сказали», «подписи наехали», «стрелка
показывает не туда» ловятся и обязаны ловиться.
"""

from __future__ import annotations

import math

import pytest
from starlette.applications import Starlette

from mcp1c import dashboard
from mcp1c.graph import Graph
from mcp1c.graph_view import (
    DEFAULT_LIMIT,
    NODE_SPACING,
    TWO_RING_FROM,
    bounds,
    neighbourhood,
)
from mcp1c.model import Configuration, Field, MetadataObject
from mcp1c.registry import Registry

from conftest import build_configuration, write_export, живой_клиент


def _конфигурация_со_звездой(соседей: int) -> Configuration:
    """Один справочник, на который ссылается заданное число документов."""
    config = Configuration(
        name="Звезда", synonym="Звезда", version="1.0", platform="8.3.23.1997"
    )
    центр = MetadataObject(
        full_name="Справочник.Склады", kind="Справочник", name="Склады"
    )
    config.objects = {центр.full_name: центр}
    for n in range(соседей):
        документ = MetadataObject(
            full_name=f"Документ.Д{n:03d}",
            kind="Документ",
            name=f"Д{n:03d}",
            attributes=[Field(name="Склад", types=["Справочник.Склады"])],
        )
        config.objects[документ.full_name] = документ
    return config


def _окрестность(соседей: int, limit: int = DEFAULT_LIMIT):
    config = _конфигурация_со_звездой(соседей)
    return neighbourhood(Graph(config), "Справочник.Склады", limit=limit)


# ------------------------------------------------------------- окрестность


def test_собираются_прямые_соседи_с_подписью_ребра():
    область = _окрестность(3)

    assert область.total == 3
    assert {узел.name for узел in область.nodes} == {
        "Документ.Д000",
        "Документ.Д001",
        "Документ.Д002",
    }
    assert all(связь.title for связь in область.links)


def test_направление_ребра_сохраняется():
    """Кто на кого ссылается — половина смысла связи.

    Документы ссылаются на склад, значит для склада это входящие рёбра, и
    стрелка обязана смотреть в него, а не из него.
    """
    область = _окрестность(2)

    assert all(not связь.outgoing for связь in область.links)
    assert {связь.target for связь in область.links} == {"Справочник.Склады"}


def test_обрезка_видна_в_числах():
    """Молчаливое усечение читается как «связей больше нет».

    Ровно тот дефект, за который сняли `depth>1` у `get_related`: раздел
    обрывался на 199 объектах и об этом не говорил.
    """
    область = _окрестность(50, limit=10)

    assert область.total == 50
    assert область.shown == 10
    assert область.truncated


def test_без_обрезки_флаг_молчит():
    область = _окрестность(5, limit=10)

    assert not область.truncated
    assert область.shown == область.total == 5


def test_изолированный_объект_даёт_пустую_окрестность():
    config = Configuration(name="Пусто", version="1.0", platform="8.3.23.1997")
    одинокий = MetadataObject(
        full_name="Константа.Ставка", kind="Константа", name="Ставка"
    )
    config.objects = {одинокий.full_name: одинокий}

    область = neighbourhood(Graph(config), "Константа.Ставка")

    assert область.total == 0
    assert область.nodes == []


def test_сам_объект_соседом_себе_не_становится():
    """Самоссылка (иерархический справочник) не должна рисовать петлю."""
    config = Configuration(name="Сам", version="1.0", platform="8.3.23.1997")
    объект = MetadataObject(
        full_name="Справочник.Склады",
        kind="Справочник",
        name="Склады",
        attributes=[Field(name="Родитель", types=["Справочник.Склады"])],
    )
    config.objects = {объект.full_name: объект}

    область = neighbourhood(Graph(config), "Справочник.Склады")

    assert "Справочник.Склады" not in {узел.name for узел in область.nodes}


# ---------------------------------------------------------------- раскладка


def test_объект_стоит_в_центре():
    область = _окрестность(5)

    assert (область.subject.x, область.subject.y) == (0.0, 0.0)


@pytest.mark.parametrize("соседей", [5, 19, 30, 60, 150, 400])
def test_соседи_не_налезают_друг_на_друга(соседей):
    """Первая версия задавала радиус константой, и подписи наезжали — увидели
    на первой же картинке. Радиус считается из числа узлов на кольце.

    Размеры перебираются до четырёхсот намеренно: при тридцати константный
    радиус ещё проходит проверку, и тест, написанный на одном размере, регресс
    пропустил бы. Границы колец (19 и 20) взяты обе.
    """
    область = _окрестность(соседей, limit=соседей)

    кольца: dict[int, list[tuple[float, float]]] = {}
    for узел in область.nodes:
        радиус = round(math.hypot(узел.x, узел.y))
        кольца.setdefault(радиус, []).append((узел.x, узел.y))

    for точки in кольца.values():
        if len(точки) < 2:
            continue
        по_углу = sorted(точки, key=lambda p: math.atan2(p[1], p[0]))
        for (x1, y1), (x2, y2) in zip(по_углу, по_углу[1:]):
            # Хорда короче дуги, поэтому сравниваем с ослабленным шагом: на
            # плотном кольце они почти равны, на редком хорда заметно меньше.
            assert math.hypot(x2 - x1, y2 - y1) >= NODE_SPACING * 0.6


def test_много_соседей_раскладывается_в_два_кольца():
    область = _окрестность(TWO_RING_FROM + 5)

    радиусы = {round(math.hypot(узел.x, узел.y)) for узел in область.nodes}

    assert len(радиусы) == 2


def test_мало_соседей_остаётся_одним_кольцом():
    область = _окрестность(TWO_RING_FROM - 5)

    радиусы = {round(math.hypot(узел.x, узел.y)) for узел in область.nodes}

    assert len(радиусы) == 1


def test_границы_охватывают_все_узлы():
    """`viewBox` считается по узлам: иначе при одном соседе половина холста
    пустая, а при трёхстах узлы уезжают за край."""
    область = _окрестность(40)
    слева, сверху, ширина, высота = bounds(область)

    for узел in область.nodes + [область.subject]:
        assert слева <= узел.x <= слева + ширина
        assert сверху <= узел.y <= сверху + высота


# ------------------------------------------------------------------ страница


@pytest.fixture
def клиент(tmp_path):
    data_dir = tmp_path / "data"
    incoming = tmp_path / "incoming"
    data_dir.mkdir()
    incoming.mkdir()
    registry = Registry(data_dir)
    registry.add_configuration(write_export(incoming, build_configuration()))
    return живой_клиент(Starlette(routes=dashboard.routes(registry)))


def test_страница_без_имени_показывает_форму(клиент):
    ответ = клиент.get("/graph")

    assert ответ.status_code == 200
    assert "<form" in ответ.text
    assert "<svg" not in ответ.text


def test_страница_рисует_граф_объекта(клиент):
    ответ = клиент.get("/graph", params={"name": "Справочник.Контрагенты"})

    assert ответ.status_code == 200
    assert "<svg" in ответ.text
    assert "РеализацияТоваровУслуг" in ответ.text


def test_неизвестный_объект_не_роняет_страницу(клиент):
    ответ = клиент.get("/graph", params={"name": "Справочник.Нетакого"})

    assert ответ.status_code == 200
    assert "нет объекта" in ответ.text


def test_изолированный_объект_объясняется_словами(клиент):
    """Пустой холст без пояснения читается как поломка страницы."""
    ответ = клиент.get("/graph", params={"name": "Документ.РеализацияТоваровУслуг"})

    assert ответ.status_code == 200
    if "<svg" not in ответ.text:
        assert "не ссылается" in ответ.text


def test_предел_из_адреса_доходит_до_картинки(клиент):
    ответ = клиент.get("/graph", params={"name": "Справочник.Контрагенты", "limit": 1})

    assert ответ.status_code == 200
    assert "<option value=1 selected>" not in ответ.text  # 1 нет среди вариантов
    assert ответ.text.count("<circle") <= 2  # объект плюс один сосед


def test_ссылка_на_граф_есть_в_навигации(клиент):
    ответ = клиент.get("/")

    assert "/graph" in ответ.text


# ------------------------------- страница без выбранной конфигурации

@pytest.fixture
def клиент_двух_конфигураций(tmp_path):
    """Две конфигурации: ровно тот случай, где реестр отказывается угадывать."""
    data_dir = tmp_path / "data"
    incoming = tmp_path / "incoming"
    data_dir.mkdir()
    incoming.mkdir()
    registry = Registry(data_dir)
    registry.add_configuration(
        write_export(incoming, build_configuration("АльфаКонфигурация"))
    )
    registry.add_configuration(
        write_export(incoming, build_configuration("БетаКонфигурация"))
    )
    return живой_клиент(Starlette(routes=dashboard.routes(registry)))


def test_форма_даёт_выбрать_конфигурацию(клиент_двух_конфигураций):
    """Тупик, найденный владельцем на живом дашборде 2026-08-18.

    По ссылке из навигации `config` пуст, реестр отказывался угадывать при
    нескольких конфигурациях, и страница печатала «укажите нужную явно» без
    единого способа её указать. Прежние тесты этого не ловили: все они ходили
    с уже заданным именем.
    """
    ответ = клиент_двух_конфигураций.get("/graph")

    assert ответ.status_code == 200
    assert "<select name=config>" in ответ.text
    assert "АльфаКонфигурация" in ответ.text
    assert "БетаКонфигурация" in ответ.text


def test_без_конфигурации_страница_не_упирается_в_отказ(клиент_двух_конфигураций):
    ответ = клиент_двух_конфигураций.get("/graph")

    assert "укажите нужную" not in ответ.text


def test_граф_строится_без_явной_конфигурации(клиент_двух_конфигураций):
    """Подставляется первая по алфавиту — как на «Запросах» и «Словаре»."""
    ответ = клиент_двух_конфигураций.get(
        "/graph", params={"name": "Справочник.Контрагенты"}
    )

    assert ответ.status_code == 200
    assert "<svg" in ответ.text


def test_пустой_реестр_объясняется_словами(tmp_path):
    """Ни одной конфигурации — не ошибка, а состояние, и оно называется."""
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    клиент = живой_клиент(Starlette(routes=dashboard.routes(Registry(data_dir))))

    ответ = клиент.get("/graph")

    assert ответ.status_code == 200
    assert "Не загружено ни одной конфигурации" in ответ.text
    assert "/sources" in ответ.text


def test_опечатка_в_имени_предлагает_похожее(клиент):
    """Имена объектов 1С длинные, опечатка — самый частый способ промахнуться.

    Отказ без подсказки заставляет уходить на другую страницу за именем.
    """
    ответ = клиент.get("/graph", params={"name": "Справочник.Контрагент"})

    assert ответ.status_code == 200
    assert "нет объекта" in ответ.text
    assert "Возможно, имелось в виду" in ответ.text
    assert "Справочник.Контрагенты" in ответ.text
