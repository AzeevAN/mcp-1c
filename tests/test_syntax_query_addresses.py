"""Адреса полей и вариантов таблиц запроса в справке платформы.

У `query_field` в разобранной справке пустой `parent_ru`, поэтому адрес поля —
голое имя (`НомерСтроки`), и десять разных полей в выдаче выглядят одинаково.
`attach_query_field_owners` делает владельцем поля таблицу, чей id стоит в
идентификаторе поля до `/fields/`; имя поля не меняется. Применяется и при
разборе, и при чтении сохранённого индекса.
"""

from __future__ import annotations

from mcp1c.model import Field, MetadataObject
from mcp1c.registry import Registry
from mcp1c.store import load_syntax, save_syntax
from mcp1c.syntax_model import KIND_QUERY_FIELD, KIND_QUERY_TABLE, SyntaxIndex, SyntaxItem
from mcp1c.tools import get_object, get_syntax, search_syntax

from conftest import build_configuration, build_syntax_registry, write_export

ПЛАТФОРМА = "8.3.27.2130"
СУБКОНТО = "РегистрБухгалтерии.<Имя регистра бухгалтерии>.Субконто"
ОСТАТКИ = "РегистрБухгалтерии.<Имя регистра бухгалтерии>.Остатки"


def _таблица(table_id: str, имя: str, *, parent: str = "", en: str = "") -> SyntaxItem:
    return SyntaxItem(
        id=table_id,
        kind=KIND_QUERY_TABLE,
        name_ru=имя,
        name_en=en,
        parent_ru=parent,
        description=f"Таблица {имя}",
    )


def _поле(field_id: str, имя: str, *, parent: str = "") -> SyntaxItem:
    return SyntaxItem(
        id=field_id,
        kind=KIND_QUERY_FIELD,
        name_ru=имя,
        parent_ru=parent,
        description=f"Поле {имя}",
    )


def _справка(*items: SyntaxItem) -> SyntaxIndex:
    index = SyntaxIndex(platforms=[ПЛАТФОРМА], source="test")
    for item in items:
        index.add(item)
    return index


def _таблица_с_полями(номер: int, имя: str, поля: list[str]) -> list[SyntaxItem]:
    table_id = f"tables/c{номер}/t{номер}"
    return [
        _таблица(table_id, имя),
        *(
            _поле(f"{table_id}/fields/Field{позиция}", поле)
            for позиция, поле in enumerate(поля)
        ),
    ]


def _адреса(ответ: str) -> list[str]:
    """Адреса из строк выдачи вида «- `адрес` / `...`»."""
    return [
        строка.split("`")[1] for строка in ответ.splitlines() if строка.startswith("- `")
    ]


# ------------------------------------------------- attach_query_field_owners


def test_поле_получает_таблицу_владельца():
    from mcp1c.syntax_parser import attach_query_field_owners

    таблица = _таблица(
        "tables/c1/t1", СУБКОНТО, en="AccountingRegister.<Name>.ExtDimensions"
    )
    поле = _поле("tables/c1/t1/fields/LineNumber5", "НомерСтроки")
    справка = _справка(таблица, поле)

    attach_query_field_owners(справка)

    assert поле.address == f"{СУБКОНТО}.НомерСтроки"
    assert поле.parent_ru == таблица.full_ru
    assert поле.parent_en == таблица.full_en
    assert поле.name_ru == "НомерСтроки"


def test_владелец_берётся_по_id_а_не_по_имени_таблицы():
    from mcp1c.syntax_parser import attach_query_field_owners

    первая = _таблица("tables/c1/t1", "РегистрНакопления.<Имя регистра накопления>")
    вторая = _таблица("tables/c1/t2", "РегистрСведений.<Имя регистра сведений>")
    поле_первой = _поле("tables/c1/t1/fields/Field1", "НомерСтроки")
    поле_второй = _поле("tables/c1/t2/fields/Field1", "НомерСтроки")
    справка = _справка(первая, вторая, поле_первой, поле_второй)

    attach_query_field_owners(справка)

    assert поле_первой.address == "РегистрНакопления.<Имя регистра накопления>.НомерСтроки"
    assert поле_второй.address == "РегистрСведений.<Имя регистра сведений>.НомерСтроки"


def test_таблица_с_родителем_отдаёт_полю_полный_адрес():
    from mcp1c.syntax_parser import attach_query_field_owners

    таблица = _таблица("tables/c1/t1", "Остатки", parent="РегистрНакопления.<Имя>")
    поле = _поле("tables/c1/t1/fields/Field1", "Период")
    справка = _справка(таблица, поле)

    attach_query_field_owners(справка)

    assert поле.address == "РегистрНакопления.<Имя>.Остатки.Период"


def test_заполненный_родитель_поля_не_трогается():
    from mcp1c.syntax_parser import attach_query_field_owners

    таблица = _таблица("tables/c1/t1", "РегистрНакопления.<Имя регистра накопления>")
    поле = _поле("tables/c1/t1/fields/Field1", "Период", parent="Чужой родитель")
    справка = _справка(таблица, поле)

    attach_query_field_owners(справка)

    assert поле.address == "Чужой родитель.Период"


def test_поле_без_таблицы_в_справке_остаётся_голым():
    from mcp1c.syntax_parser import attach_query_field_owners

    поле = _поле("tables/c9/t9/fields/Field1", "НомерСтроки")
    справка = _справка(поле)

    attach_query_field_owners(справка)

    assert поле.address == "НомерСтроки"
    assert поле.parent_ru == ""


def test_повторный_вызов_не_меняет_адрес():
    from mcp1c.syntax_parser import attach_query_field_owners

    таблица = _таблица("tables/c1/t1", СУБКОНТО)
    поле = _поле("tables/c1/t1/fields/Field1", "НомерСтроки")
    справка = _справка(таблица, поле)

    attach_query_field_owners(справка)
    attach_query_field_owners(справка)

    assert поле.address == f"{СУБКОНТО}.НомерСтроки"


def test_не_поля_запроса_не_получают_владельца():
    from mcp1c.syntax_parser import attach_query_field_owners

    таблица = _таблица("tables/c1/t1", СУБКОНТО)
    метод = SyntaxItem(id="tables/c1/t1/fields/Field1", kind="method", name_ru="Метод")
    справка = _справка(таблица, метод)

    attach_query_field_owners(справка)

    assert метод.parent_ru == ""
    assert таблица.parent_ru == ""


def test_старый_индекс_получает_владельцев_при_чтении(tmp_path):
    # Так выглядит индекс, сохранённый до правки: у поля пустой parent.
    сохранённый = save_syntax(
        _справка(
            _таблица("tables/c1/t1", СУБКОНТО),
            _поле("tables/c1/t1/fields/LineNumber5", "НомерСтроки"),
        ),
        tmp_path / f"{ПЛАТФОРМА}.json.gz",
    )

    загруженный = load_syntax(сохранённый)

    поле = загруженный.items["tables/c1/t1/fields/LineNumber5"]
    assert поле.address == f"{СУБКОНТО}.НомерСтроки"
    assert поле.name_ru == "НомерСтроки"


# ------------------------------------------------------ выдача инструментов


def _реестр_номерстроки(tmp_path) -> Registry:
    return build_syntax_registry(
        tmp_path,
        [
            *_таблица_с_полями(
                1, "РегистрНакопления.<Имя регистра накопления>", ["НомерСтроки"]
            ),
            *_таблица_с_полями(
                2, "РегистрСведений.<Имя регистра сведений>", ["НомерСтроки"]
            ),
        ],
        ПЛАТФОРМА,
    )


def test_поиск_номерстроки_без_одинаковых_строк(tmp_path):
    реестр = _реестр_номерстроки(tmp_path)

    ответ = search_syntax(реестр, "НомерСтроки", kind="query_field")

    адреса = _адреса(ответ)
    assert len(адреса) == 2
    assert len(set(адреса)) == 2
    assert sorted(адреса) == [
        "РегистрНакопления.<Имя регистра накопления>.НомерСтроки",
        "РегистрСведений.<Имя регистра сведений>.НомерСтроки",
    ]


def test_get_syntax_номерстроки_список_адресов(tmp_path):
    реестр = _реестр_номерстроки(tmp_path)

    ответ = get_syntax(реестр, "НомерСтроки")

    assert ответ.startswith("# Одноимённых элементов: 2")
    assert "`РегистрНакопления.<Имя регистра накопления>.НомерСтроки`" in ответ
    assert "`РегистрСведений.<Имя регистра сведений>.НомерСтроки`" in ответ
    assert "Вариант 1 из 2" not in ответ


def test_варианты_таблицы_бухгалтерии_подписаны(tmp_path):
    реестр = build_syntax_registry(
        tmp_path,
        [
            *_таблица_с_полями(1, ОСТАТКИ, ["Счет", "СуммаОстаток"]),
            *_таблица_с_полями(
                2, ОСТАТКИ, ["Счет", "СуммаОстаток", "СуммаОстатокДт", "СуммаОстатокКт"]
            ),
        ],
        ПЛАТФОРМА,
    )

    ответ = search_syntax(реестр, "Остатки", kind="query_table")

    assert _адреса(ответ) == [ОСТАТКИ]
    assert "2 варианта: с корреспонденцией и без" in ответ


def test_одинаковые_варианты_таблицы_одна_строка(tmp_path):
    реестр = build_syntax_registry(
        tmp_path,
        [
            *_таблица_с_полями(1, ОСТАТКИ, ["Счет", "СуммаОстаток"]),
            *_таблица_с_полями(2, ОСТАТКИ, ["Счет", "СуммаОстаток"]),
        ],
        ПЛАТФОРМА,
    )

    ответ = search_syntax(реестр, "Остатки", kind="query_table")

    assert _адреса(ответ) == [ОСТАТКИ]
    assert "вариант" not in ответ.lower()


# --------------------------------------------- страж: таблицы запроса объекта


def test_таблицы_запроса_объекта_не_изменились(tmp_path):
    """Имена полей виртуальных таблиц не зависят от того, пуст ли parent у поля."""
    справка = _справка(
        _таблица("tables/catalog8/table10", "РегистрНакопления.<Имя регистра накопления>"),
        _поле("tables/catalog8/table10/fields/field0", "<Имя реквизита>"),
        _поле("tables/catalog8/table10/fields/field1", "Период"),
        _таблица(
            "tables/catalog8/table11",
            "РегистрНакопления.<Имя регистра накопления>.Остатки",
        ),
        _поле("tables/catalog8/table11/fields/field0", "<Имя реквизита>"),
    )
    конфигурация = build_configuration()
    конфигурация.platform = ПЛАТФОРМА
    конфигурация.objects = {
        "РегистрНакопления.ТоварыНаСкладах": MetadataObject(
            full_name="РегистрНакопления.ТоварыНаСкладах",
            kind="РегистрНакопления",
            name="ТоварыНаСкладах",
            synonym="Товары на складах",
            attributes=[Field(name="КодСтроки")],
            props={"register_kind": "Остатки"},
        )
    }
    входящие = tmp_path / "incoming"
    входящие.mkdir()
    реестр = Registry(tmp_path / "data")
    реестр.add_syntax(save_syntax(справка, входящие / f"{ПЛАТФОРМА}.json.gz"))
    реестр.add_configuration(write_export(входящие, конфигурация))

    ответ = get_object(реестр, "РегистрНакопления.ТоварыНаСкладах")

    assert "## Таблицы запроса" in ответ
    assert (
        "- `РегистрНакопления.ТоварыНаСкладах`\n"
        "  реквизиты: КодСтроки\n"
        "  служебные: Период\n"
        "- `РегистрНакопления.ТоварыНаСкладах.Остатки`\n"
        "  реквизиты: КодСтроки\n"
    ) in ответ
