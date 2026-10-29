"""`get_related`: все связи объекта достижимы курсором.

Раньше каждый раздел («Ссылается на», «Ссылаются на него») обрезался по
`limit` строкой «… ещё K», и у объекта со ста пятнадцатью входящими связями
остальные были недостижимы: продолжения не существовало. Теперь исходящие и
входящие связи — один упорядоченный список (сначала исходящие), страница режет
его по `limit`, а продолжение отдаётся блоком с аргументами следующего вызова.
"""

from __future__ import annotations

import json
import re

import pytest

from mcp1c import tools
from mcp1c.model import Field, MetadataObject
from mcp1c.registry import Registry, RegistryError

from conftest import build_configuration, write_export
from native_export import загрузить_код

ЦЕНТР = "Справочник.Центр"
ВХОДЯЩИХ = 115
ИСХОДЯЩИХ = 50
ЛИМИТ = 40

_СТРОКА = re.compile(r"^- `([^`]+)` — ", re.MULTILINE)
_ПРОДОЛЖЕНИЕ = re.compile(r"^`get_related\((\{.*\})\)`$", re.MULTILINE)


def _конфигурация(
    *, входящих: int = ВХОДЯЩИХ, исходящих: int = ИСХОДЯЩИХ, префикс: str = "Источник"
):
    """Центр: на него ссылаются `входящих` документов, он — на `исходящих` справочников."""
    конфигурация = build_configuration(name="Связи")
    цели = [
        MetadataObject(
            full_name=f"Справочник.Цель{номер:03d}",
            kind="Справочник",
            name=f"Цель{номер:03d}",
            synonym=f"Цель {номер}",
        )
        for номер in range(исходящих)
    ]
    центр = MetadataObject(
        full_name=ЦЕНТР,
        kind="Справочник",
        name="Центр",
        synonym="Центр",
        attributes=[
            Field(name=f"Ссылка{цель.name}", types=[цель.full_name]) for цель in цели
        ],
    )
    источники = [
        MetadataObject(
            full_name=f"Документ.{префикс}{номер:03d}",
            kind="Документ",
            name=f"{префикс}{номер:03d}",
            synonym=f"{префикс} {номер}",
            attributes=[Field(name="Центр", types=[ЦЕНТР])],
        )
        for номер in range(входящих)
    ]
    конфигурация.objects = {
        объект.full_name: объект for объект in [центр, *цели, *источники]
    }
    return конфигурация


def _реестр(каталог, конфигурация) -> Registry:
    входящие = каталог / "incoming"
    входящие.mkdir(parents=True, exist_ok=True)
    реестр = Registry(каталог / "data")
    реестр.add_configuration(write_export(входящие, конфигурация))
    return реестр


@pytest.fixture
def реестр(tmp_path) -> Registry:
    return _реестр(tmp_path, _конфигурация())


def _строки(ответ: str) -> list[str]:
    return _СТРОКА.findall(ответ)


def _аргументы_продолжения(ответ: str) -> dict | None:
    """Аргументы следующего вызова из блока «Продолжение», если он есть."""
    if "Продолжение (аргументы следующего вызова):" not in ответ:
        return None
    найдено = _ПРОДОЛЖЕНИЕ.search(ответ)
    assert найдено is not None, ответ
    return json.loads(найдено.group(1))


def _курсор(реестр: Registry, full_name: str = ЦЕНТР) -> str:
    аргументы = _аргументы_продолжения(
        tools.get_related(реестр, full_name, limit=ЛИМИТ)
    )
    assert аргументы is not None, "у первой страницы нет продолжения"
    return аргументы["cursor"]


def test_все_ссылки_достижимы_курсором(реестр):
    страницы = []
    аргументы: dict = {"full_name": ЦЕНТР}
    while True:
        ответ = tools.get_related(реестр, **аргументы, limit=ЛИМИТ)
        страницы.append(ответ)
        аргументы = _аргументы_продолжения(ответ)
        if аргументы is None:
            break
        assert len(страницы) < 10, "обход не заканчивается"
        аргументы.pop("limit")

    собрано = [строка for страница in страницы for строка in _строки(страница)]

    assert len(собрано) == ИСХОДЯЩИХ + ВХОДЯЩИХ == 165
    assert len(set(собрано)) == 165
    assert len(страницы) == 5
    assert "Продолжение" not in страницы[-1]
    assert "… ещё" not in "".join(страницы)


def test_продолжение_называет_инструмент_и_повторяет_аргументы(реестр):
    ответ = tools.get_related(реестр, ЦЕНТР, config="Связи", limit=ЛИМИТ)

    аргументы = _аргументы_продолжения(ответ)

    assert аргументы is not None
    assert set(аргументы) == {"full_name", "config", "cursor", "limit"}
    assert аргументы["full_name"] == ЦЕНТР
    assert аргументы["config"] == "Связи"
    assert аргументы["limit"] == ЛИМИТ
    assert аргументы["cursor"]


def test_первая_страница_без_курсора_начинается_как_раньше(реестр):
    граф = реестр.resolve(None).configuration.graph
    ожидаемые = [
        f"- `{ребро.target}` — {ребро.title}"
        for ребро in граф.outgoing(ЦЕНТР, include_weak=False)[:ЛИМИТ]
    ]
    assert len(ожидаемые) == ЛИМИТ

    ответ = tools.get_related(реестр, ЦЕНТР, limit=ЛИМИТ)

    напечатанные = [
        строка for строка in ответ.splitlines() if строка.startswith("- `")
    ]
    # Ровно сорок строк: входящие на первую страницу не попадают.
    assert напечатанные == ожидаемые
    assert "## Ссылается на (50)" in ответ
    assert "Ссылаются на него" not in ответ


def test_страница_режет_общий_список_исходящие_затем_входящие(реестр):
    аргументы = _аргументы_продолжения(tools.get_related(реестр, ЦЕНТР, limit=ЛИМИТ))
    assert аргументы is not None
    вторая = tools.get_related(реестр, ЦЕНТР, cursor=аргументы["cursor"], limit=ЛИМИТ)

    имена = _строки(вторая)

    assert len(имена) == ЛИМИТ
    assert all(имя.startswith("Справочник.Цель") for имя in имена[:10])
    assert all(имя.startswith("Документ.Источник") for имя in имена[10:])


def test_заголовок_раздела_с_полным_числом_на_каждой_странице(реестр):
    первая = tools.get_related(реестр, ЦЕНТР, limit=ЛИМИТ)
    вторая = tools.get_related(
        реестр, ЦЕНТР, cursor=_аргументы_продолжения(первая)["cursor"], limit=ЛИМИТ
    )
    третья = tools.get_related(
        реестр, ЦЕНТР, cursor=_аргументы_продолжения(вторая)["cursor"], limit=ЛИМИТ
    )

    # Вторая страница несёт хвост исходящих и начало входящих: оба заголовка.
    assert "## Ссылается на (50)" in вторая
    assert "## Ссылаются на него (115)" in вторая
    # Третья — только входящие, и число в заголовке остаётся полным.
    assert "## Ссылаются на него (115)" in третья
    assert "## Ссылается на" not in третья


def test_курсор_другого_объекта_отвергнут(реестр):
    курсор = _курсор(реестр)

    with pytest.raises(RegistryError, match="начните чтение заново"):
        tools.get_related(реестр, "Справочник.Цель001", cursor=курсор, limit=ЛИМИТ)


def test_курсор_после_изменения_связей_отвергнут(tmp_path):
    до = _реестр(tmp_path / "до", _конфигурация())
    курсор = _курсор(до)
    # То же число связей, но входящие теперь от других объектов.
    после = _реестр(tmp_path / "после", _конфигурация(префикс="Приёмник"))

    with pytest.raises(RegistryError, match="начните чтение заново"):
        tools.get_related(после, ЦЕНТР, cursor=курсор, limit=ЛИМИТ)


def test_курсор_после_изменения_числа_связей_отвергнут(tmp_path):
    до = _реестр(tmp_path / "до", _конфигурация())
    курсор = _курсор(до)
    после = _реестр(tmp_path / "после", _конфигурация(входящих=ВХОДЯЩИХ + 1))

    with pytest.raises(RegistryError, match="начните чтение заново"):
        tools.get_related(после, ЦЕНТР, cursor=курсор, limit=ЛИМИТ)


@pytest.mark.parametrize(
    "курсор",
    ["мусор", "AAAA", "e30", "WzFd"],
    ids=["текст", "не-json", "пустой-объект", "список"],
)
def test_испорченный_курсор_отвергнут(реестр, курсор):
    with pytest.raises(RegistryError, match="начните чтение заново"):
        tools.get_related(реестр, ЦЕНТР, cursor=курсор, limit=ЛИМИТ)


def test_get_object_full_ведёт_в_get_related(реестр):
    ответ = tools.get_object(реестр, ЦЕНТР, detail="full")

    assert f"## На объект ссылаются ({ВХОДЯЩИХ})" in ответ
    assert (
        f"- … ещё {ВХОДЯЩИХ - 20} — полный список: get_related"
        in ответ.splitlines()
    )


def test_get_object_без_обрезки_не_зовёт_get_related(tmp_path):
    реестр = _реестр(tmp_path, _конфигурация(входящих=5, исходящих=1))

    ответ = tools.get_object(реестр, ЦЕНТР, detail="full")

    assert "На объект ссылаются (5)" in ответ
    assert "полный список" not in ответ


def test_пометка_расширения_есть_и_на_второй_странице(
    tmp_path, реестр_из_кода, архив_кода
):
    конфигурация = build_configuration(name="Пример")
    документ = MetadataObject(
        full_name="Документ.Пример",
        kind="Документ",
        name="Пример",
        synonym="Пример",
        attributes=[Field(name="Другое", types=["Справочник.Другой"])],
    )
    другой = MetadataObject(
        full_name="Справочник.Другой", kind="Справочник", name="Другой", synonym="Другой"
    )
    связанный = MetadataObject(
        full_name="Справочник.Связанный",
        kind="Справочник",
        name="Связанный",
        synonym="Связанный",
        attributes=[Field(name="Владелец", types=["Документ.Пример"])],
    )
    конфигурация.objects = {
        объект.full_name: объект for объект in (документ, другой, связанный)
    }
    база = tmp_path / "base"
    (база / "Documents" / "Пример" / "Ext").mkdir(parents=True)
    (база / "Documents" / "Пример" / "Ext" / "ObjectModule.bsl").write_text(
        "Процедура Текущая()\nКонецПроцедуры\n", encoding="utf-8"
    )
    реестр = реестр_из_кода(база, configuration=конфигурация)
    расширение = tmp_path / "extension"
    (расширение / "Catalogs" / "Связанный" / "Ext").mkdir(parents=True)
    (расширение / "Catalogs" / "Связанный" / "Ext" / "ManagerModule.bsl").write_text(
        '&Вместо("Получить")\nПроцедура Получить()\nКонецПроцедуры\n',
        encoding="utf-8",
    )
    загрузить_код(
        реестр, архив_кода(расширение, extension="Доп"), configuration="Пример"
    )

    первая = tools.get_related(реестр, "Документ.Пример", config="Пример", limit=1)
    аргументы = _аргументы_продолжения(первая)
    assert аргументы is not None
    вторая = tools.get_related(
        реестр, "Документ.Пример", config="Пример", cursor=аргументы["cursor"], limit=1
    )

    assert "`Справочник.Другой`" in первая
    assert "Справочник.Связанный" not in первая
    строка = next(line for line in вторая.splitlines() if "Справочник.Связанный" in line)
    assert "*(перекрыто расширением `Доп`)*" in строка


_ОБА_ЧИСЛА = "Ссылается на: 50 · ссылаются на него: 115"


def test_первая_страница_называет_оба_числа(реестр):
    ответ = tools.get_related(реестр, ЦЕНТР, limit=ЛИМИТ)

    # На первой странице строк входящих ещё нет, но их число названо.
    assert "Ссылаются на него" not in ответ
    assert _ОБА_ЧИСЛА in ответ.splitlines()


def test_последняя_страница_называет_оба_числа(реестр):
    аргументы: dict = {"full_name": ЦЕНТР}
    последняя = ""
    for _ in range(10):
        последняя = tools.get_related(реестр, **аргументы, limit=ЛИМИТ)
        аргументы = _аргументы_продолжения(последняя)
        if аргументы is None:
            break
        аргументы.pop("limit")
    else:
        pytest.fail("обход не заканчивается")

    assert "Продолжение" not in последняя
    assert _ОБА_ЧИСЛА in последняя.splitlines()
