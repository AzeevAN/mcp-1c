"""RED-матрица платформенных реквизитов объектов и регистров."""

from __future__ import annotations

import importlib

import pytest

from mcp1c.graph import Graph
from mcp1c.intake_v2_converter import base_layer_data
from mcp1c.model import Configuration, Field, MetadataObject, ObjectRelation


def _project(configuration: Configuration) -> None:
    try:
        module = importlib.import_module("mcp1c.standard_attributes")
    except ModuleNotFoundError as error:
        if error.name != "mcp1c.standard_attributes":
            raise
        pytest.fail("RED: отсутствует единая проекция стандартных реквизитов")
    module.materialize_standard_attributes(configuration)


def _document(
    name: str,
    *,
    number_type: str,
    number_length: int,
    number_allowed_length: str = "",
) -> MetadataObject:
    return MetadataObject(
        full_name=f"Документ.{name}",
        kind="Документ",
        name=name,
        props={
            "posting": "Allow",
            "number_type": number_type,
            "number_length": number_length,
            "number_allowed_length": number_allowed_length,
        },
    )


def _configuration() -> Configuration:
    text = _document(
        "Text",
        number_type="Строка",
        number_length=11,
        number_allowed_length="Fixed",
    )
    numeric = _document(
        "Numeric",
        number_type="Число",
        number_length=9,
    )
    without_number = _document(
        "WithoutNumber",
        number_type="Строка",
        number_length=0,
    )
    without_number.props["posting"] = "Deny"
    owner = MetadataObject(
        full_name="Справочник.Owners",
        kind="Справочник",
        name="Owners",
        props={
            "hierarchical": False,
            "code_length": 0,
            "description_length": 80,
            "code_type": "Строка",
        },
    )
    child = MetadataObject(
        full_name="Справочник.Items",
        kind="Справочник",
        name="Items",
        props={
            "hierarchical": True,
            "code_length": 9,
            "code_type": "Строка",
            "code_allowed_length": "Variable",
            "description_length": 120,
        },
        owners=[owner.full_name],
        attributes=[Field("Custom", types=["Булево"])],
    )
    journal = MetadataObject(
        full_name="ЖурналДокументов.Ledger",
        kind="ЖурналДокументов",
        name="Ledger",
        extended={
            "registered_documents": [text.full_name, numeric.full_name],
            "standard_attributes": [
                {"name": "Type", "synonym": "Вид документа", "comment": ""},
                {"name": "Ref", "synonym": "Документ", "comment": ""},
                {"name": "Number", "synonym": "Номер", "comment": ""},
                {"name": "Date", "synonym": "Дата", "comment": ""},
                {"name": "Posted", "synonym": "Проведен", "comment": ""},
                {
                    "name": "DeletionMark",
                    "synonym": "Пометка удаления",
                    "comment": "",
                },
            ],
        },
        relations=[
            ObjectRelation("registers_document", text.full_name, "resolved"),
            ObjectRelation("registers_document", numeric.full_name, "resolved"),
        ],
    )
    objects = {
        item.full_name: item
        for item in (text, numeric, without_number, owner, child, journal)
    }
    return Configuration(name="Demo", objects=objects)


def test_единая_проекция_типизирует_все_три_вида_и_идемпотентна():
    configuration = _configuration()

    _project(configuration)
    _project(configuration)

    text = configuration.get("Документ.Text")
    assert text is not None
    assert [(item.name, item.type_spec()) for item in text.attributes] == [
        ("Ссылка", "Документ.Text"),
        ("Номер", "Строка(11, фикс.)"),
        ("Дата", "Дата"),
        ("Проведен", "Булево"),
        ("ПометкаУдаления", "Булево"),
    ]
    without_number = configuration.get("Документ.WithoutNumber")
    assert without_number is not None
    assert "Номер" not in {item.name for item in without_number.attributes}
    assert "Проведен" in {item.name for item in without_number.attributes}

    catalog = configuration.get("Справочник.Items")
    assert catalog is not None
    assert [item.name for item in catalog.attributes] == [
        "Ссылка",
        "Код",
        "Наименование",
        "Владелец",
        "Родитель",
        "ЭтоГруппа",
        "ПометкаУдаления",
        "Предопределенный",
        "ИмяПредопределенныхДанных",
        "Custom",
    ]
    assert catalog.attributes[3].types == ["Справочник.Owners"]
    assert catalog.attributes[8].type_spec() == "Строка (длина определяется платформой)"
    assert catalog.attributes[8].is_unlimited_string is False

    journal = configuration.get("ЖурналДокументов.Ledger")
    assert journal is not None
    fields = {item.name: item for item in journal.attributes}
    assert fields["Тип"].type_spec() == "Строка (длина определяется платформой)"
    assert fields["Ссылка"].types == ["Документ.Text", "Документ.Numeric"]
    assert fields["Номер"].types == ["Строка", "Число"]
    assert fields["Номер"].string_length == 11
    assert fields["Номер"].string_allowed_length == "Fixed"
    assert fields["Номер"].digits == 9
    assert fields["Дата"].types == ["Дата"]
    assert fields["Проведен"].types == ["Булево"]
    assert fields["ПометкаУдаления"].types == ["Булево"]


def test_стандартные_ссылки_не_дублируют_предметные_ребра():
    configuration = _configuration()
    _project(configuration)

    graph = Graph(configuration)

    owner_edges = [
        edge
        for edge in graph.outgoing("Справочник.Items")
        if edge.target == "Справочник.Owners"
    ]
    assert [(edge.kind, edge.via) for edge in owner_edges] == [("owner", "")]
    journal_edges = graph.outgoing("ЖурналДокументов.Ledger")
    assert [(edge.kind, edge.target) for edge in journal_edges] == [
        ("registers_document", "Документ.Text"),
        ("registers_document", "Документ.Numeric"),
    ]


def test_вычисляемые_поля_не_попадают_обратно_в_base_layer():
    configuration = _configuration()
    _project(configuration)

    semantic = base_layer_data(configuration)

    objects = {item["full_name"]: item for item in semantic["objects"]}
    assert [item["name"] for item in objects["Документ.Text"]["attributes"]] == []
    assert [
        item["name"] for item in objects["Справочник.Items"]["attributes"]
    ] == ["Custom"]


def test_обычный_реквизит_с_зарезервированным_именем_отклоняется():
    configuration = _configuration()
    document = configuration.get("Документ.Text")
    assert document is not None
    document.attributes.append(Field("ссылка", types=["Строка"]))

    with pytest.raises(ValueError, match="системн.*Ссылка"):
        _project(configuration)


def _field_names(obj: MetadataObject) -> list[str]:
    return [item.name for item in obj.attributes]


def test_регистры_сведений_и_накопления_получают_условные_системные_поля():
    document = _document("Recorder", number_type="Строка", number_length=9)
    information = MetadataObject(
        full_name="РегистрСведений.Цены",
        kind="РегистрСведений",
        name="Цены",
        props={"periodicity": "Day", "write_mode": "RecorderSubordinate"},
    )
    independent = MetadataObject(
        full_name="РегистрСведений.Настройки",
        kind="РегистрСведений",
        name="Настройки",
        props={"periodicity": "Nonperiodical", "write_mode": "Independent"},
    )
    accumulation = MetadataObject(
        full_name="РегистрНакопления.ОстаткиТоваров",
        kind="РегистрНакопления",
        name="ОстаткиТоваров",
        props={"register_kind": "Balance"},
    )
    document.movements = [information.full_name, accumulation.full_name]
    configuration = Configuration(
        name="Demo",
        objects={item.full_name: item for item in (document, information, independent, accumulation)},
    )

    _project(configuration)
    _project(configuration)

    assert _field_names(information) == [
        "Период",
        "Регистратор",
        "НомерСтроки",
        "Активность",
    ]
    assert information.attributes[0].types == ["Дата"]
    assert information.attributes[1].types == ["Документ.Recorder"]
    assert information.attributes[2].types == ["Число"]
    assert information.attributes[3].types == ["Булево"]
    assert _field_names(independent) == []
    assert _field_names(accumulation) == [
        "Период",
        "Регистратор",
        "НомерСтроки",
        "Активность",
        "ВидДвижения",
    ]
    assert accumulation.attributes[-1].types == ["ВидДвиженияНакопления"]


def test_бухгалтерский_регистр_получает_эффективные_поля_запроса():
    document = _document("Operation", number_type="Строка", number_length=9)
    chart = MetadataObject(
        full_name="ПланСчетов.Рабочий",
        kind="ПланСчетов",
        name="Рабочий",
        props={
            "max_ext_dimension_count": 2,
            "ext_dimension_types": "ПланВидовХарактеристик.ВидыСубконто",
        },
    )
    characteristics = MetadataObject(
        full_name="ПланВидовХарактеристик.ВидыСубконто",
        kind="ПланВидовХарактеристик",
        name="ВидыСубконто",
        value_type=Field("ТипЗначения", types=["Справочник.Контрагенты", "Строка"]),
    )
    register = MetadataObject(
        full_name="РегистрБухгалтерии.Проводки",
        kind="РегистрБухгалтерии",
        name="Проводки",
        props={
            "correspondence": True,
            "chart_of_accounts": chart.full_name,
            "period_adjustment_length": 2,
        },
    )
    document.movements = [register.full_name]
    configuration = Configuration(
        name="Demo",
        objects={item.full_name: item for item in (document, chart, characteristics, register)},
    )

    _project(configuration)

    assert _field_names(register) == [
        "Период",
        "УточнениеПериода",
        "Регистратор",
        "НомерСтроки",
        "Активность",
        "СчетДт",
        "СчетКт",
    ]
    fields = {item.name: item for item in register.attributes}
    assert fields["УточнениеПериода"].types == ["Число"]
    assert fields["СчетДт"].types == ["ПланСчетов.Рабочий"]


def test_бухгалтерский_регистр_без_плана_не_показывает_серый_счет():
    register = MetadataObject(
        full_name="РегистрБухгалтерии.Пустой",
        kind="РегистрБухгалтерии",
        name="Пустой",
        props={"correspondence": False, "chart_of_accounts": ""},
    )
    configuration = Configuration(name="Demo", objects={register.full_name: register})

    _project(configuration)

    assert _field_names(register) == [
        "Период",
        "Регистратор",
        "НомерСтроки",
        "Активность",
        "ВидДвижения",
    ]


def test_регистр_расчета_различает_период_действия_и_базовый_период():
    document = _document("Payroll", number_type="Строка", number_length=9)
    calculation = MetadataObject(
        full_name="РегистрРасчета.Начисления",
        kind="РегистрРасчета",
        name="Начисления",
        props={
            "action_period": True,
            "base_period": False,
            "chart_of_calculation_types": "ПланВидовРасчета.Начисления",
        },
    )
    document.movements = [calculation.full_name]
    configuration = Configuration(
        name="Demo", objects={document.full_name: document, calculation.full_name: calculation}
    )

    _project(configuration)

    assert _field_names(calculation) == [
        "ПериодРегистрации",
        "Регистратор",
        "НомерСтроки",
        "ВидРасчета",
        "ПериодДействия",
        "ПериодДействияНачало",
        "ПериодДействияКонец",
        "Активность",
        "Сторно",
    ]
    assert calculation.attributes[3].types == ["ПланВидовРасчета.Начисления"]
    assert "БазовыйПериодНачало" not in _field_names(calculation)
