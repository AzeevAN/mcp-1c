"""Виртуальные таблицы регистров: имена полей, которых нет в метаданных.

Ресурс регистра называется в запросе иначе, чем в конфигураторе: `Количество`
в основной таблице, `КоличествоОстаток` в `.Остатки`, `КоличествоПриход` в
`.Обороты`. Правило подстановки живёт в справке платформы (раздел `tables/`,
шаблоны вида `<Имя ресурса>Остаток`), реальные имена ресурсов — в метаданных
конфигурации. Ни одна половина в отдельности агенту не помогает.
"""

import pytest

from mcp1c.model import Configuration, Field, MetadataObject
from mcp1c.syntax_model import SyntaxIndex, SyntaxItem
from mcp1c.virtual_tables import build_table_index, virtual_tables


def _register(kind_of_register: str = "Остатки") -> MetadataObject:
    return MetadataObject(
        full_name="РегистрНакопления.ТоварыНаСкладах",
        kind="РегистрНакопления",
        name="ТоварыНаСкладах",
        synonym="Товары на складах",
        attributes=[Field(name="КодСтроки", synonym="Код строки")],
        dimensions=[
            Field(name="Склад", synonym="Склад"),
            Field(name="Номенклатура", synonym="Номенклатура"),
        ],
        resources=[
            Field(name="Количество", synonym="Количество"),
            Field(name="Резерв", synonym="Резерв"),
        ],
        props={"register_kind": kind_of_register},
    )


def _syntax_with_tables() -> SyntaxIndex:
    """Справка с тремя таблицами регистра накопления — как в настоящей.

    Имена и состав полей взяты из разобранного `shcntx_ru.hbk`: основная
    таблица отдаёт ресурс без суффикса, `.Остатки` — с суффиксом `Остаток`,
    `.Обороты` — тремя полями на каждый ресурс.
    """
    index = SyntaxIndex(platforms=["8.3.23.1997"], language="ru", source="test")

    def add_table(table_id: str, name: str, fields: list[str]) -> None:
        index.add(
            SyntaxItem(
                id=table_id,
                kind="query_table",
                name_ru=name,
                name_en="",
                description=f"Описание таблицы {name}",
            )
        )
        for number, field_name in enumerate(fields):
            index.add(
                SyntaxItem(
                    id=f"{table_id}/fields/field{number}",
                    kind="query_field",
                    name_ru=field_name,
                    name_en="",
                )
            )

    add_table(
        "tables/catalog8/table10",
        "РегистрНакопления.<Имя регистра накопления>",
        ["<Имя измерения>", "<Имя ресурса>", "<Имя реквизита>", "Период", "Регистратор"],
    )
    add_table(
        "tables/catalog8/table11",
        "РегистрНакопления.<Имя регистра накопления>.Остатки",
        ["<Имя измерения>", "<Имя общего реквизита>", "<Имя ресурса>Остаток"],
    )
    add_table(
        "tables/catalog8/table12",
        "РегистрНакопления.<Имя регистра накопления>.Обороты",
        [
            "<Имя измерения>",
            "<Имя ресурса>Приход",
            "<Имя ресурса>Расход",
            "<Имя ресурса>Оборот",
            "Период",
            "ПериодГод",
            "ПериодМесяц",
        ],
    )
    return index


def test_ресурс_получает_суффикс_таблицы():
    """Главный случай: `Количество` в `.Остатки` называется `КоличествоОстаток`."""
    tables = virtual_tables(_register(), build_table_index(_syntax_with_tables()))
    balance = next(t for t in tables if t.suffix == "Остатки")

    assert balance.resources == ["КоличествоОстаток", "РезервОстаток"]
    assert balance.name == "РегистрНакопления.ТоварыНаСкладах.Остатки"


def test_обороты_дают_три_поля_на_каждый_ресурс():
    tables = virtual_tables(_register(), build_table_index(_syntax_with_tables()))
    turnovers = next(t for t in tables if t.suffix == "Обороты")

    assert turnovers.resources == [
        "КоличествоПриход",
        "КоличествоРасход",
        "КоличествоОборот",
        "РезервПриход",
        "РезервРасход",
        "РезервОборот",
    ]


def test_измерения_подставляются_из_конфигурации():
    tables = virtual_tables(_register(), build_table_index(_syntax_with_tables()))
    balance = next(t for t in tables if t.suffix == "Остатки")

    assert balance.dimensions == ["Склад", "Номенклатура"]


def test_основная_таблица_отдаёт_ресурс_без_суффикса():
    """Ровно то имя, что видно в конфигураторе, — и только в этой таблице."""
    tables = virtual_tables(_register(), build_table_index(_syntax_with_tables()))
    main = next(t for t in tables if t.suffix == "")

    assert main.resources == ["Количество", "Резерв"]
    assert main.name == "РегистрНакопления.ТоварыНаСкладах"


def test_оборотный_регистр_не_получает_остатковых_таблиц():
    """`.Остатки` существует только у регистров остатков — так сказано в справке."""
    tables = virtual_tables(_register("Обороты"), build_table_index(_syntax_with_tables()))
    suffixes = {t.suffix for t in tables}

    assert "Остатки" not in suffixes
    assert "Обороты" in suffixes


def test_оборотный_регистр_не_получает_прихода_и_расхода():
    """Приход и расход следуют из вида движения, которого у оборотного нет.

    Единственное правило модуля строже справки: та перечисляет для `.Обороты`
    все три поля без оговорки про вид регистра.
    """
    tables = virtual_tables(_register("Обороты"), build_table_index(_syntax_with_tables()))
    turnovers = next(t for t in tables if t.suffix == "Обороты")

    assert turnovers.resources == ["КоличествоОборот", "РезервОборот"]


def test_общий_реквизит_не_выдумывается():
    """`<Имя общего реквизита>` в модели конфигурации не представлен."""
    tables = virtual_tables(_register(), build_table_index(_syntax_with_tables()))
    balance = next(t for t in tables if t.suffix == "Остатки")

    assert all("общего реквизита" not in name for name in balance.all_fields())


def test_детализация_периода_сворачивается():
    """ПериодГод/ПериодМесяц — шум на каждый вызов, разворачивать их незачем."""
    tables = virtual_tables(_register(), build_table_index(_syntax_with_tables()))
    turnovers = next(t for t in tables if t.suffix == "Обороты")

    assert "Период" in turnovers.service
    assert "ПериодГод" not in turnovers.service


def test_без_справки_ничего_не_выдумывается():
    """Нет источника — нет блока. Суффиксы наизусть не сочиняем."""
    assert virtual_tables(_register(), build_table_index(None)) == []


def test_пустая_таблица_справки_пропускается():
    """В справке 8.3.5 те же таблицы размечены без полей — это не источник."""
    index = SyntaxIndex(platforms=["8.3.5.1570"], language="ru", source="test")
    index.add(
        SyntaxItem(
            id="tables/catalog8/table99",
            kind="query_table",
            name_ru="РегистрНакопления.<Имя регистра>.Остатки",
            name_en="",
        )
    )

    assert virtual_tables(_register(), build_table_index(index)) == []


def _information_register(**props) -> MetadataObject:
    return MetadataObject(
        full_name="РегистрСведений.ЦеныНоменклатуры",
        kind="РегистрСведений",
        name="ЦеныНоменклатуры",
        synonym="Цены номенклатуры",
        dimensions=[Field(name="Номенклатура", synonym="Номенклатура")],
        resources=[Field(name="Цена", synonym="Цена")],
        props=props,
    )


def _syntax_with_slice() -> SyntaxIndex:
    index = SyntaxIndex(platforms=["8.3.23.1997"], language="ru", source="test")
    index.add(
        SyntaxItem(
            id="tables/catalog20/table21",
            kind="query_table",
            name_ru="РегистрСведений.<Имя регистра сведений>.СрезПоследних",
            name_en="",
        )
    )
    for number, field_name in enumerate(["<Имя измерения>", "<Имя ресурса>", "Период"]):
        index.add(
            SyntaxItem(
                id=f"tables/catalog20/table21/fields/field{number}",
                kind="query_field",
                name_ru=field_name,
                name_en="",
            )
        )
    return index


def test_периодический_регистр_сведений_получает_срез():
    """У регистра сведений `register_kind` нет, а таблицы есть.

    `СрезПоследних` — самая частая виртуальная таблица в отчётах; требовать
    для неё вид регистра значит не показать её никогда.
    """
    tables = virtual_tables(
        _information_register(periodicity="День"), build_table_index(_syntax_with_slice())
    )

    assert [t.suffix for t in tables] == ["СрезПоследних"]
    assert tables[0].resources == ["Цена"]


def test_непериодический_регистр_сведений_среза_не_получает():
    """Живой промах агента: срез предложен непериодическому регистру.

    У непериодического нет самого поля `Период`, по которому берётся срез, —
    запрос не скомпилируется.
    """
    tables = virtual_tables(
        _information_register(periodicity="Непериодический"),
        build_table_index(_syntax_with_slice()),
    )

    assert tables == []


def test_без_признака_периодичности_срез_не_показывается():
    """Так устроены все выгрузки до 2026-08-17: свойство терялось молча.

    Молчать безопаснее, чем предложить срез регистру, у которого его нет.
    """
    tables = virtual_tables(
        _information_register(), build_table_index(_syntax_with_slice())
    )

    assert tables == []


def _calculation_syntax() -> SyntaxIndex:
    """Две таблицы регистра расчёта: одна про период действия, другая про график."""
    index = SyntaxIndex(platforms=["8.3.27"], language="ru", source="test")
    for table_id, name, fields in (
        (
            "tables/catalog31/table50",
            "РегистрРасчета.<Имя регистра расчета>.ФактическийПериодДействия",
            ["<Имя измерения>", "<Имя ресурса>", "ВидРасчета"],
        ),
        (
            "tables/catalog31/table53",
            "РегистрРасчета.<Имя регистра расчета>.ДанныеГрафика",
            ["<Имя измерения>", "<Имя ресурса графика>ПериодДействия"],
        ),
    ):
        index.add(SyntaxItem(id=table_id, kind="query_table", name_ru=name, name_en=""))
        for number, field_name in enumerate(fields):
            index.add(
                SyntaxItem(
                    id=f"{table_id}/fields/field{number}",
                    kind="query_field",
                    name_ru=field_name,
                    name_en="",
                )
            )
    return index


def _calculation_register(**props) -> MetadataObject:
    return MetadataObject(
        full_name="РегистрРасчета.Начисления",
        kind="РегистрРасчета",
        name="Начисления",
        synonym="Начисления",
        dimensions=[Field(name="Сотрудник", synonym="Сотрудник")],
        resources=[Field(name="Результат", synonym="Результат")],
        props=props,
    )


def test_фактический_период_действия_только_при_периоде_действия():
    """У регистра без периода действия нет самого понятия «фактический период»."""
    tables = build_table_index(_calculation_syntax())

    с_периодом = virtual_tables(_calculation_register(action_period=True), tables)
    без_периода = virtual_tables(_calculation_register(action_period=False), tables)

    assert "ФактическийПериодДействия" in {t.suffix for t in с_периодом}
    assert "ФактическийПериодДействия" not in {t.suffix for t in без_периода}


def test_данные_графика_берут_ресурсы_графика():
    """`ДанныеГрафика` описывает ресурсы графика — отдельного регистра сведений."""
    tables = build_table_index(_calculation_syntax())
    register = _calculation_register(action_period=True, schedule="РегистрСведений.Графики")

    с_графиком = virtual_tables(
        register, tables, schedule_resources=["ОсновноеЗначение", "Норма"]
    )
    график = next(t for t in с_графиком if t.suffix == "ДанныеГрафика")

    assert "ОсновноеЗначениеПериодДействия" in график.service
    assert "НормаПериодДействия" in график.service
    # Ресурс самого регистра расчёта в эти поля не подставляется.
    assert "РезультатПериодДействия" not in график.service


def test_без_графика_таблица_данных_графика_не_показывается():
    """Назвать поля нечем — молчим, а не подставляем ресурсы регистра."""
    tables = build_table_index(_calculation_syntax())
    без = virtual_tables(_calculation_register(action_period=True), tables)

    assert "ДанныеГрафика" not in {t.suffix for t in без}


def test_два_шаблона_на_один_суффикс_пропускаются():
    """Так справка описывает регистр бухгалтерии: с корреспонденцией и без.

    Признака корреспонденции в выгрузке нет — показать наугад один из двух
    значит вернуться к той же ошибке, ради которой писался модуль.
    """
    index = SyntaxIndex(platforms=["8.3.23.1997"], language="ru", source="test")
    for number, (table_id, fields) in enumerate(
        (
            ("tables/catalog36/table39", ["<Имя ресурса>Остаток"]),
            ("tables/catalog43/table47", ["<Имя ресурса>ОстатокДт"]),
        )
    ):
        index.add(
            SyntaxItem(
                id=table_id,
                kind="query_table",
                name_ru="РегистрБухгалтерии.<Имя регистра бухгалтерии>.Остатки",
                name_en="",
            )
        )
        for position, field_name in enumerate(fields):
            index.add(
                SyntaxItem(
                    id=f"{table_id}/fields/field{position}",
                    kind="query_field",
                    name_ru=field_name,
                    name_en="",
                )
            )

    register = MetadataObject(
        full_name="РегистрБухгалтерии.Типовой",
        kind="РегистрБухгалтерии",
        name="Типовой",
        synonym="Журнал проводок",
        resources=[Field(name="Сумма", synonym="Сумма")],
    )

    assert virtual_tables(register, build_table_index(index)) == []


def test_объект_без_вида_регистра_не_обрабатывается():
    """Справочник виртуальных таблиц регистра не имеет."""
    catalog = MetadataObject(
        full_name="Справочник.Номенклатура",
        kind="Справочник",
        name="Номенклатура",
        synonym="Номенклатура",
    )

    assert virtual_tables(catalog, build_table_index(_syntax_with_tables())) == []
