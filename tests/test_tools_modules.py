"""Публичный поиск процедур работает поверх боевого Registry.

Архивы кода и реестр собирают фикстуры `conftest.py`: прямой подстановки
`LoadedModules` или придуманных обходных методов здесь нет.
"""

from __future__ import annotations

from dataclasses import replace
import threading
import time

import pytest

from mcp1c import modules_index, tools
from mcp1c.module_address import путь_модуля
from mcp1c.model import Field, MetadataObject
from mcp1c.registry import STATUS_ERROR, Registry, RegistryError

from conftest import build_configuration, write_export, write_syntax


def _добавить_одноимённые(корень, *, имя="Одинаковая", экспорт=False, количество=1):
    окончание = " Экспорт" if экспорт else ""
    for номер in range(количество):
        каталог = корень / "CommonModules" / f"Общий{номер:02d}" / "Ext"
        каталог.mkdir(parents=True, exist_ok=True)
        (каталог / "Module.bsl").write_text(
            f"Процедура {имя}(){окончание}\nКонецПроцедуры\n", encoding="utf-8"
        )


def test_точное_имя_находит_неэкспортную_и_читает_сигнатуру_с_диска(
    реестр_с_кодом,
):
    загружено = реестр_с_кодом.resolve("Пример").modules
    путь = загружено.корень / путь_модуля("ОбщийМодуль.ОбщийПример")
    путь.write_text(
        путь.read_text(encoding="utf-8").replace(
            "Процедура Внутренняя()", "Процедура Внутренняя(НовоеЗначение)"
        ),
        encoding="utf-8",
    )

    ответ = tools.search_procedures(
        реестр_с_кодом, "Внутренняя", config="Пример"
    )

    assert "ОбщийМодуль.ОбщийПример::Внутренняя" in ответ
    assert "Процедура Внутренняя(НовоеЗначение)" in ответ
    assert "неэкспортная" in ответ


def test_слова_находят_только_экспортные(реестр_с_кодом):
    ответ = tools.search_procedures(
        реестр_с_кодом, "складывает числа", config="Пример"
    )

    assert "ОбщийМодуль.ОбщийПример::Сложить" in ответ
    assert "::Внутренняя" not in ответ


def test_пустая_выдача_объясняет_отбор(реестр_с_кодом):
    ответ = tools.search_procedures(
        реестр_с_кодом, "несуществующее слово", config="Пример"
    )

    assert "только по экспортным" in ответ
    assert "get_procedure(" in ответ


def test_limit_ограничивает_уровень_точного_имени(
    корень_кода, реестр_из_кода
):
    _добавить_одноимённые(корень_кода, имя="ПриОткрытии", количество=7)
    реестр = реестр_из_кода(корень_кода)

    ответ = tools.search_procedures(
        реестр, "ПриОткрытии", config="Пример", limit=3
    )

    assert ответ.count("::ПриОткрытии") == 3
    assert "ещё 5" in ответ  # семь добавленных плюс процедура формы


def test_limit_ограничивает_поиск_по_словам(корень_кода, реестр_из_кода):
    for номер in range(6):
        каталог = корень_кода / "CommonModules" / f"Платежи{номер}" / "Ext"
        каталог.mkdir(parents=True)
        (каталог / "Module.bsl").write_text(
            "// Обрабатывает входящий платеж.\n"
            f"Процедура ОбработатьПлатеж{номер}() Экспорт\n"
            "КонецПроцедуры\n",
            encoding="utf-8",
        )
    реестр = реестр_из_кода(корень_кода)

    ответ = tools.search_procedures(
        реестр, "входящий платеж", config="Пример", limit=2
    )

    assert ответ.count("::ОбработатьПлатеж") == 2
    assert "ещё" in ответ


def test_scope_объекта_и_точного_модуля_меняет_приоритет(
    корень_кода, реестр_из_кода
):
    общий = корень_кода / "CommonModules" / "ОбщийПример" / "Ext" / "Module.bsl"
    общий.write_text(
        общий.read_text(encoding="utf-8")
        + "\nПроцедура Одинаковая() Экспорт\nКонецПроцедуры\n",
        encoding="utf-8",
    )
    объект = корень_кода / "Documents" / "Пример" / "Ext" / "ObjectModule.bsl"
    объект.write_text(
        объект.read_text(encoding="utf-8")
        + "\nПроцедура Одинаковая() Экспорт\nКонецПроцедуры\n",
        encoding="utf-8",
    )
    реестр = реестр_из_кода(корень_кода)

    глобально = tools.search_procedures(
        реестр, "Одинаковая", config="Пример", limit=2
    )
    объект_первым = tools.search_procedures(
        реестр,
        "Одинаковая",
        config="Пример",
        scope="Документ.Пример",
        limit=2,
    )
    общий_первым = tools.search_procedures(
        реестр,
        "Одинаковая",
        config="Пример",
        scope="ОбщийМодуль.ОбщийПример",
        limit=2,
    )

    общий_адрес = "ОбщийМодуль.ОбщийПример::Одинаковая"
    объект_адрес = "Документ.Пример.МодульОбъекта::Одинаковая"
    assert глобально.index(общий_адрес) < глобально.index(объект_адрес)
    assert глобально.count(общий_адрес) == 1
    assert глобально.count(объект_адрес) == 1
    assert объект_первым.index(объект_адрес) < объект_первым.index(общий_адрес)
    assert общий_первым.index(общий_адрес) < общий_первым.index(объект_адрес)


def test_scope_не_угадывается_из_query(корень_кода, реестр_из_кода):
    общий = корень_кода / "CommonModules" / "ОбщийПример" / "Ext" / "Module.bsl"
    общий.write_text(
        общий.read_text(encoding="utf-8")
        + "\nПроцедура ДокументПример() Экспорт\nКонецПроцедуры\n",
        encoding="utf-8",
    )
    объект = корень_кода / "Documents" / "Пример" / "Ext" / "ObjectModule.bsl"
    объект.write_text(
        объект.read_text(encoding="utf-8")
        + "\nПроцедура ДокументПример() Экспорт\nКонецПроцедуры\n",
        encoding="utf-8",
    )
    реестр = реестр_из_кода(корень_кода)

    ответ = tools.search_procedures(
        реестр, "ДокументПример", config="Пример", limit=2
    )

    assert ответ.index("ОбщийМодуль.ОбщийПример::ДокументПример") < ответ.index(
        "Документ.Пример.МодульОбъекта::ДокументПример"
    )


def test_scope_поднимает_словесное_попадание_вне_глобального_limit(
    корень_кода, реестр_из_кода
):
    общий = корень_кода / "CommonModules" / "Первый" / "Ext"
    общий.mkdir(parents=True)
    (общий / "Module.bsl").write_text(
        "// Ищет специальное значение.\n"
        "Процедура НайтиВОбщем() Экспорт\nКонецПроцедуры\n",
        encoding="utf-8",
    )
    объект = корень_кода / "Documents" / "Пример" / "Ext" / "ObjectModule.bsl"
    объект.write_text(
        объект.read_text(encoding="utf-8")
        + "\n// Ищет специальное значение.\n"
        "Процедура НайтиВДокументе() Экспорт\nКонецПроцедуры\n",
        encoding="utf-8",
    )
    реестр = реестр_из_кода(корень_кода)

    глобально = tools.search_procedures(
        реестр, "специальное значение", config="Пример", limit=1
    )
    в_объекте = tools.search_procedures(
        реестр,
        "специальное значение",
        config="Пример",
        scope="Документ.Пример",
        limit=1,
    )

    assert "ОбщийМодуль.Первый::НайтиВОбщем" in глобально
    assert "Документ.Пример.МодульОбъекта::НайтиВДокументе" in в_объекте


def test_сигнатуры_не_читаются_сверх_limit(
    корень_кода, реестр_из_кода, monkeypatch
):
    _добавить_одноимённые(корень_кода, количество=8)
    реестр = реестр_из_кода(корень_кода)
    настоящая = tools._сигнатура
    прочитано = 0

    def считать(*args):
        nonlocal прочитано
        прочитано += 1
        return настоящая(*args)

    monkeypatch.setattr(tools, "_сигнатура", считать)

    tools.search_procedures(
        реестр, "Одинаковая", config="Пример", limit=2
    )

    assert прочитано == 2


@pytest.mark.parametrize("extension", [None, "Доп"], ids=["modules", "extension"])
@pytest.mark.parametrize("action", ["reparse", "remove"])
def test_смена_поколения_во_время_чтения_сигнатуры_не_смешивает_ответ(
    корень_кода,
    реестр_из_кода,
    архив_кода,
    monkeypatch,
    extension,
    action,
):
    модуль = корень_кода / "CommonModules" / "Гонка" / "Ext"
    модуль.mkdir(parents=True)
    файл = модуль / "Module.bsl"
    файл.write_text(
        "Процедура Сменить(СтарыйПараметр) Экспорт\nКонецПроцедуры\n",
        encoding="utf-8",
    )
    реестр = реестр_из_кода(корень_кода, extension=extension)

    файл.write_text(
        "// новое поколение\n"
        "Процедура Сменить(НовыйПараметр) Экспорт\nКонецПроцедуры\n",
        encoding="utf-8",
    )
    новый = архив_кода(корень_кода, extension=extension)

    чтение_начато = threading.Event()
    отпустить = threading.Event()
    настоящее_чтение = tools.прочитать_модуль
    первый = True

    def задержать(путь):
        nonlocal первый
        if первый and путь.name == "Module.bsl" and "Гонка" in путь.parts:
            первый = False
            чтение_начато.set()
            отпустить.wait(timeout=3)
        return настоящее_чтение(путь)

    monkeypatch.setattr(tools, "прочитать_модуль", задержать)
    ответы: list[str] = []
    ошибки: list[BaseException] = []

    def искать():
        try:
            ответы.append(
                tools.search_procedures(
                    реестр,
                    "Сменить",
                    config="Пример",
                    extension=extension,
                )
            )
        except BaseException as ошибка:
            ошибки.append(ошибка)

    поток = threading.Thread(target=искать)
    поток.start()
    try:
        assert чтение_начато.wait(timeout=1)
        source_id = "Пример:ext:Доп" if extension else "Пример:modules"
        if action == "reparse":
            реестр.add_modules(новый, configuration="Пример")
        else:
            реестр.remove(source_id)
    finally:
        отпустить.set()
        поток.join(timeout=3)

    assert not поток.is_alive()
    if action == "reparse":
        assert not ошибки
        assert "НовыйПараметр" in ответы[0]
        assert "СтарыйПараметр" not in ответы[0]
        assert "строка 2" in ответы[0]
    elif extension:
        assert not ответы
        assert len(ошибки) == 1
        assert isinstance(ошибки[0], RegistryError)
        assert "не загружено" in str(ошибки[0])
        assert "/private/" not in str(ошибки[0])
    else:
        assert not ошибки
        assert "выгрузка в файлы не загружена" in ответы[0]


def test_две_смены_поколения_дают_стабильную_ошибку_повтора(
    корень_кода, реестр_из_кода, архив_кода, monkeypatch
):
    модуль = корень_кода / "CommonModules" / "Гонка" / "Ext"
    модуль.mkdir(parents=True)
    файл = модуль / "Module.bsl"
    файл.write_text(
        "Процедура Сменить(Поколение0) Экспорт\nКонецПроцедуры\n",
        encoding="utf-8",
    )
    реестр = реестр_из_кода(корень_кода)

    архивы = []
    for номер in (1, 2):
        файл.write_text(
            f"Процедура Сменить(Поколение{номер}) Экспорт\nКонецПроцедуры\n",
            encoding="utf-8",
        )
        архивы.append(архив_кода(корень_кода))

    начаты = [threading.Event(), threading.Event()]
    отпустить = [threading.Event(), threading.Event()]
    настоящее_чтение = tools.прочитать_модуль
    номер_чтения = 0

    def задержать(путь):
        nonlocal номер_чтения
        if путь.name == "Module.bsl" and "Гонка" in путь.parts and номер_чтения < 2:
            номер = номер_чтения
            номер_чтения += 1
            начаты[номер].set()
            отпустить[номер].wait(timeout=3)
        return настоящее_чтение(путь)

    monkeypatch.setattr(tools, "прочитать_модуль", задержать)
    ошибки: list[BaseException] = []

    def искать():
        try:
            tools.search_procedures(реестр, "Сменить", config="Пример")
        except BaseException as ошибка:
            ошибки.append(ошибка)

    поток = threading.Thread(target=искать)
    поток.start()
    for номер, архив in enumerate(архивы):
        assert начаты[номер].wait(timeout=1)
        реестр.add_modules(архив, configuration="Пример")
        отпустить[номер].set()
    поток.join(timeout=3)

    assert not поток.is_alive()
    assert len(ошибки) == 1
    assert isinstance(ошибки[0], RegistryError)
    assert "изменился во время поиска" in str(ошибки[0])
    assert "повторите" in str(ошибки[0]).lower()
    assert "/private/" not in str(ошибки[0])


def test_текущая_ошибка_чтения_не_раскрывает_путь(
    реестр_с_кодом, monkeypatch
):
    def отказ(_):
        raise PermissionError(13, "секретная причина", "/private/секрет/Module.bsl")

    monkeypatch.setattr(tools, "прочитать_модуль", отказ)

    with pytest.raises(RegistryError) as информация:
        tools.search_procedures(
            реестр_с_кодом, "Внутренняя", config="Пример"
        )

    текст = str(информация.value)
    assert "/private/" not in текст
    assert "секрет" not in текст
    assert "недоступен" in текст


def test_сигнатура_не_захватывает_тело_с_той_же_строки(
    корень_кода, реестр_из_кода
):
    файл = корень_кода / "CommonModules" / "ОбщийПример" / "Ext" / "Module.bsl"
    файл.write_text(
        "Процедура Найти() Экспорт Секрет=42; КонецПроцедуры\n",
        encoding="utf-8",
    )
    реестр = реестр_из_кода(корень_кода)

    ответ = tools.search_procedures(реестр, "Найти", config="Пример")

    assert "Сигнатура: `Процедура Найти() Экспорт`" in ответ
    assert "Секрет" not in ответ
    assert "КонецПроцедуры" not in ответ


@pytest.mark.parametrize(
    ("объявление", "ожидаемая"),
    [
        (
            "Процедура Найти() Экспорт// комментарий\n",
            "Процедура Найти() Экспорт",
        ),
        (
            "Procedure Найти() Export// comment\n",
            "Procedure Найти() Export",
        ),
        (
            "Процедура Найти(\n    Параметр\n) Экспорт// комментарий\n",
            "Процедура Найти( Параметр ) Экспорт",
        ),
        (
            "Процедура Найти() Экспорт; Секрет = 42;\n",
            "Процедура Найти() Экспорт",
        ),
    ],
    ids=["ru-comment", "en-comment", "multiline-comment", "semicolon-body"],
)
def test_сигнатура_берёт_export_перед_комментарием_и_не_тело(
    корень_кода, реестр_из_кода, объявление, ожидаемая
):
    файл = корень_кода / "CommonModules" / "ОбщийПример" / "Ext" / "Module.bsl"
    файл.write_text(
        объявление + "КонецПроцедуры\n", encoding="utf-8"
    )
    реестр = реестр_из_кода(корень_кода)

    ответ = tools.search_procedures(реестр, "Найти", config="Пример")

    assert f"Сигнатура: `{ожидаемая}`" in ответ
    assert "комментарий" not in ответ
    assert "comment" not in ответ
    assert "Секрет" not in ответ


def test_многострочная_сигнатура_учитывает_строки_комментарии_crlf_и_lone_cr(
    корень_кода, реестр_из_кода
):
    файл = корень_кода / "CommonModules" / "ОбщийПример" / "Ext" / "Module.bsl"
    текст = (
        "// одиночный CR\rне меняет номер\r\n"
        "Функция Сложная(Первый = \"скобки ) и (\", // комментарий )\r\n"
        "    Второй = Новый Массив()) Экспорт Секрет=42;\r\n"
        "КонецФункции\r\n"
    )
    файл.write_bytes(текст.encode("utf-8-sig"))
    реестр = реестр_из_кода(корень_кода)

    ответ = tools.search_procedures(реестр, "Сложная", config="Пример")

    assert "скобки ) и (" in ответ
    assert "Второй = Новый Массив()) Экспорт" in ответ
    assert "Секрет" not in ответ
    assert "строка 2" in ответ


def test_неразрешённые_одноимённые_вызовы_не_выглядят_нулём(
    корень_кода, реестр_из_кода
):
    for имя in ("ЦельА", "ЦельБ"):
        каталог = корень_кода / "CommonModules" / имя / "Ext"
        каталог.mkdir(parents=True)
        (каталог / "Module.bsl").write_text(
            "Процедура Одинаковая() Экспорт\nКонецПроцедуры\n",
            encoding="utf-8",
        )
    вызывающий = корень_кода / "CommonModules" / "Вызывающий" / "Ext"
    вызывающий.mkdir(parents=True)
    (вызывающий / "Module.bsl").write_text(
        "Процедура Одинаковая() Экспорт\nКонецПроцедуры\n"
        "Процедура Позвать() Экспорт\n"
        "    ЦельА.Одинаковая();\n"
        "    Одинаковая();\n"
        "    Неизвестный.Одинаковая();\n"
        "КонецПроцедуры\n",
        encoding="utf-8",
    )
    реестр = реестр_из_кода(корень_кода)

    ответ = tools.search_procedures(
        реестр, "Одинаковая", config="Пример", limit=10
    )

    первое = ответ.index("::Одинаковая")
    предупреждение = ответ.index("не удалось разрешить")
    assert предупреждение < первое
    assert "не удалось разрешить: 1" in ответ
    assert "подтверждённых мест вызова: 1" in ответ
    assert "подтверждённых мест вызова: 0" in ответ


@pytest.mark.parametrize("annotation", ["Вместо", "После", "Перед"])
def test_поиск_нейтрально_называет_аннотацию_расширения(
    корень_кода, реестр_из_кода, annotation
):
    файл = корень_кода / "CommonModules" / "ОбщийПример" / "Ext" / "Module.bsl"
    файл.write_text(
        f"&{annotation}\nПроцедура Аннотированная() Экспорт\nКонецПроцедуры\n",
        encoding="utf-8",
    )
    реестр = реестр_из_кода(корень_кода, extension="Доп")

    ответ = tools.search_procedures(
        реестр,
        "Аннотированная",
        config="Пример",
        extension="Доп",
    )

    assert "есть аннотация расширения" in ответ
    assert "перекрывает исходную" not in ответ


def test_аннотация_на_строке_объявления_не_подменяет_сигнатуру(
    корень_кода, реестр_из_кода
):
    файл = корень_кода / "CommonModules" / "ОбщийПример" / "Ext" / "Module.bsl"
    файл.write_text(
        '&После("Исходная") Процедура Аннотированная(Параметр) Экспорт\n'
        "КонецПроцедуры\n",
        encoding="utf-8",
    )
    реестр = реестр_из_кода(корень_кода, extension="Доп")

    ответ = tools.search_procedures(
        реестр,
        "Аннотированная",
        config="Пример",
        extension="Доп",
    )

    assert "Сигнатура: `Процедура Аннотированная(Параметр) Экспорт`" in ответ
    assert "&После" not in ответ


def test_при_limit_50_нет_невыполнимого_совета_увеличить(
    корень_кода, реестр_из_кода
):
    for номер in range(51):
        каталог = корень_кода / "CommonModules" / f"Массовый{номер:02d}" / "Ext"
        каталог.mkdir(parents=True)
        (каталог / "Module.bsl").write_text(
            "// Находит массовое значение.\n"
            f"Процедура Массовая{номер:02d}() Экспорт\nКонецПроцедуры\n",
            encoding="utf-8",
        )
    реестр = реестр_из_кода(корень_кода)

    ответ = tools.search_procedures(
        реестр, "массовое значение", config="Пример", limit=50
    )

    assert "увеличьте `limit`" not in ответ
    assert "максимум" in ответ


@pytest.mark.parametrize("limit", [0, -1, 51, "много", True])
def test_неверный_limit_отклоняется(реестр_с_кодом, limit):
    with pytest.raises(RegistryError, match="limit.*1.*50"):
        tools.search_procedures(
            реестр_с_кодом, "Сложить", config="Пример", limit=limit
        )


def test_неизвестные_config_extension_scope_отклоняются(
    реестр_с_кодом, tmp_path
):
    with pytest.raises(RegistryError, match="Конфигурация не загружена"):
        tools.search_procedures(
            реестр_с_кодом, "Сложить", config="НетТакой"
        )
    with pytest.raises(RegistryError, match="Расширение.*не загружено"):
        tools.search_procedures(
            реестр_с_кодом,
            "Сложить",
            config="Пример",
            extension="НетТакого",
        )
    with pytest.raises(RegistryError, match="Область поиска.*не найдена"):
        tools.search_procedures(
            реестр_с_кодом,
            "Сложить",
            config="Пример",
            scope="Документ.НетТакого",
        )

    входящее = tmp_path / "second"
    входящее.mkdir()
    реестр_с_кодом.add_configuration(
        write_export(входящее, build_configuration(name="Вторая"))
    )
    with pytest.raises(RegistryError, match="несколько конфигураций"):
        tools.search_procedures(реестр_с_кодом, "Сложить")


def test_extension_выбирает_отдельный_корпус(
    реестр_с_кодом, корень_кода, архив_кода
):
    модуль = корень_кода / "CommonModules" / "ТолькоДоп" / "Ext"
    модуль.mkdir(parents=True)
    (модуль / "Module.bsl").write_text(
        "Процедура ТолькоВРасширении() Экспорт\nКонецПроцедуры\n",
        encoding="utf-8",
    )
    реестр_с_кодом.add_modules(
        архив_кода(корень_кода, extension="Доп"), configuration="Пример"
    )

    база = tools.search_procedures(
        реестр_с_кодом, "ТолькоВРасширении", config="Пример"
    )
    расширение = tools.search_procedures(
        реестр_с_кодом,
        "ТолькоВРасширении",
        config="Пример",
        extension="Доп",
    )

    assert "::ТолькоВРасширении" not in база
    assert "ОбщийМодуль.ТолькоДоп::ТолькоВРасширении" in расширение


def test_без_кода_отвечает_честной_причиной(tmp_path):
    входящее = tmp_path / "incoming"
    входящее.mkdir()
    реестр = Registry(tmp_path / "data")
    реестр.add_configuration(
        write_export(входящее, build_configuration(name="Пример"))
    )

    ответ = tools.search_procedures(реестр, "что угодно", config="Пример")

    assert "выгрузка в файлы не загружена" in ответ
    assert "код" in ответ.lower()


def test_строящийся_индекс_показывает_этап_и_фактический_прогресс(
    реестр_с_кодом, monkeypatch
):
    реестр_с_кодом.save()
    for вид in реестр_с_кодом.CACHE_KINDS["modules"]:
        реестр_с_кодом._cache_path("Пример:modules", вид).unlink(missing_ok=True)

    второй_файл = threading.Event()
    отпустить = threading.Event()
    прочитано = 0
    настоящее_чтение = modules_index.прочитать_модуль

    def прочитать(путь):
        nonlocal прочитано
        прочитано += 1
        if прочитано == 2:
            второй_файл.set()
            отпустить.wait(timeout=3)
        return настоящее_чтение(путь)

    monkeypatch.setattr(modules_index, "прочитать_модуль", прочитать)
    заново = Registry(реестр_с_кодом.data_dir)
    try:
        assert not заново.startup()
        assert второй_файл.wait(timeout=1)

        ответ = tools.search_procedures(
            заново, "Сложить", config="Пример"
        )

        assert "этап 1/4" in ответ
        assert "оглавление" in ответ
        assert "обработано 1 из 3" in ответ
    finally:
        отпустить.set()

    край = time.monotonic() + 3
    while not заново.resolve("Пример").modules.готов:
        assert time.monotonic() < край, "фоновая сборка не завершилась"
        time.sleep(0.01)


@pytest.mark.parametrize("extension", [None, "Доп"], ids=["modules", "extension"])
@pytest.mark.parametrize("state", ["progress", "error"])
def test_состояние_сборки_читается_одним_атомарным_снимком(
    корень_кода, реестр_из_кода, monkeypatch, extension, state
):
    реестр = реестр_из_кода(корень_кода, extension=extension)
    loaded = реестр.resolve("Пример", extension=extension)
    loaded = loaded.extension if extension else loaded.modules
    loaded.готов = False

    выбор_завершён = threading.Event()
    продолжить_поиск = threading.Event()
    промежуточная_запись = threading.Event()
    завершить_запись = threading.Event()
    поиск_завершён = threading.Event()
    настоящий_выбор = tools._selected_modules

    def задержать_после_выбора(*args, **kwargs):
        результат = настоящий_выбор(*args, **kwargs)
        выбор_завершён.set()
        продолжить_поиск.wait(timeout=3)
        return результат

    monkeypatch.setattr(tools, "_selected_modules", задержать_после_выбора)
    ответы: list[str] = []

    def писать():
        with реестр._lock:
            if state == "progress":
                loaded.этап = (2, 4)
            else:
                loaded.source.status = STATUS_ERROR
            промежуточная_запись.set()
            завершить_запись.wait(timeout=3)
            if state == "progress":
                loaded.название_этапа = "вызовы"
                loaded.прогресс = (1, 3)
            else:
                loaded.source.error = "детерминированный отказ"

    def искать():
        ответы.append(
            tools.search_procedures(
                реестр, "Сложить", config="Пример", extension=extension
            )
        )
        поиск_завершён.set()

    поток_поиска = threading.Thread(target=искать)
    поток_поиска.start()
    assert выбор_завершён.wait(timeout=1)
    поток_записи = threading.Thread(target=писать)
    поток_записи.start()
    assert промежуточная_запись.wait(timeout=1)
    продолжить_поиск.set()
    вернулся_в_промежутке = поиск_завершён.wait(timeout=0.1)
    завершить_запись.set()
    поток_поиска.join(timeout=3)
    поток_записи.join(timeout=3)

    assert not вернулся_в_промежутке
    assert not поток_поиска.is_alive()
    assert not поток_записи.is_alive()
    if state == "progress":
        assert "этап 2/4" in ответы[0]
        assert "«вызовы»" in ответы[0]
        assert "обработано 1 из 3" in ответы[0]
    else:
        assert "детерминированный отказ" in ответы[0]
        assert "причина не записана" not in ответы[0]


@pytest.mark.parametrize("extension", [None, "Доп"], ids=["modules", "extension"])
def test_устаревший_снимок_состояния_повторяет_поиск(
    корень_кода, реестр_из_кода, monkeypatch, extension
):
    реестр = реестр_из_кода(корень_кода, extension=extension)
    context = реестр.resolve("Пример", extension=extension)
    старые = context.extension if extension else context.modules
    старые.готов = False
    новые = replace(старые, готов=True)
    настоящий_выбор = tools._selected_modules
    первый = True

    def сменить_после_выбора(*args, **kwargs):
        nonlocal первый
        результат = настоящий_выбор(*args, **kwargs)
        if первый:
            первый = False
            with реестр._lock:
                реестр.modules[старые.source.id] = новые
        return результат

    monkeypatch.setattr(tools, "_selected_modules", сменить_после_выбора)

    ответ = tools.search_procedures(
        реестр, "Сложить", config="Пример", extension=extension
    )

    assert "::Сложить" in ответ
    assert "индекс кода строится" not in ответ.lower()


# ------------------------------------------------------ карточка процедуры


def test_get_procedure_адрес_модуля_отдаёт_только_оглавление(
    реестр_с_кодом,
):
    ответ = tools.get_procedure(
        реестр_с_кодом, "ОбщийМодуль.ОбщийПример", config="Пример"
    )

    assert "::Сложить" in ответ
    assert "::Внутренняя" in ответ
    assert "Сигнатура" in ответ
    assert "Возврат Первый + Второй" not in ответ


def test_get_procedure_оглавление_разбирает_текст_модуля_один_раз(
    реестр_с_кодом, monkeypatch
):
    настоящий_разбор = tools.разобрать
    разборов = 0

    def считать(текст):
        nonlocal разборов
        разборов += 1
        return настоящий_разбор(текст)

    monkeypatch.setattr(tools, "разобрать", считать)

    tools.get_procedure(
        реестр_с_кодом, "ОбщийМодуль.ОбщийПример", config="Пример"
    )

    assert разборов == 1


def test_get_procedure_пустой_модуль_отличается_от_промаха(
    корень_кода, реестр_из_кода
):
    каталог = корень_кода / "CommonModules" / "Пустой" / "Ext"
    каталог.mkdir(parents=True)
    (каталог / "Module.bsl").write_text(
        "// В модуле намеренно нет процедур.\n", encoding="utf-8"
    )
    реестр = реестр_из_кода(корень_кода)

    ответ = tools.get_procedure(
        реестр, "ОбщийМодуль.Пустой", config="Пример"
    )

    assert "В модуле нет разобранных процедур" in ответ
    assert "не найден" not in ответ


def test_get_procedure_две_процедуры_на_одной_строке_не_смешивают_тела(
    корень_кода, реестр_из_кода
):
    файл = корень_кода / "CommonModules" / "ОбщийПример" / "Ext" / "Module.bsl"
    файл.write_text(
        "Процедура Первая(Первый) Экспорт МаркерПервой = 1; КонецПроцедуры "
        "Процедура Вторая(Второй) Экспорт МаркерВторой = 2; КонецПроцедуры\n",
        encoding="utf-8",
    )
    реестр = реестр_из_кода(корень_кода)

    первая = tools.get_procedure(
        реестр, "ОбщийМодуль.ОбщийПример::Первая", config="Пример"
    )
    вторая = tools.get_procedure(
        реестр, "ОбщийМодуль.ОбщийПример::Вторая", config="Пример"
    )

    assert "Первая(Первый) Экспорт" in первая
    assert "МаркерПервой" in первая and "МаркерВторой" not in первая
    assert "Вторая(Второй) Экспорт" in вторая
    assert "МаркерВторой" in вторая and "МаркерПервой" not in вторая


def test_get_procedure_одноимённые_на_одной_строке_сопоставляются_по_порядку(
    корень_кода, реестр_из_кода
):
    файл = корень_кода / "CommonModules" / "ОбщийПример" / "Ext" / "Module.bsl"
    файл.write_text(
        "Процедура Дубль(Первый) Экспорт ПервыйМаркер = 1; КонецПроцедуры "
        "Процедура дУбЛь(Второй) Экспорт ВторойМаркер = 2; КонецПроцедуры\n",
        encoding="utf-8",
    )
    реестр = реестр_из_кода(корень_кода)

    оглавление = tools.get_procedure(
        реестр, "ОбщийМодуль.ОбщийПример", config="Пример"
    )
    карточка = tools.get_procedure(
        реестр, "ОбщийМодуль.ОбщийПример::ДУБЛЬ", config="Пример"
    )

    assert оглавление.count("Процедура Дубль(Первый) Экспорт") == 1
    assert оглавление.count("Процедура дУбЛь(Второй) Экспорт") == 1
    assert "ПервыйМаркер" in карточка and "ВторойМаркер" not in карточка


def test_get_procedure_точные_границы_не_путают_аннотацию_и_строки_с_кодом(
    корень_кода, реестр_из_кода
):
    файл = корень_кода / "CommonModules" / "ОбщийПример" / "Ext" / "Module.bsl"
    файл.write_text(
        '&НаСервере Процедура Первая(Текст = "Процедура Ложная()") Экспорт '
        'Сообщить("// КонецПроцедуры Процедура Ложная()"); КонецПроцедуры '
        "Процедура Вторая() Экспорт МаркерВторой = 2; КонецПроцедуры\n",
        encoding="utf-8",
    )
    реестр = реестр_из_кода(корень_кода)

    первая = tools.get_procedure(
        реестр, "ОбщийМодуль.ОбщийПример::Первая", config="Пример"
    )

    assert '&НаСервере' in первая
    assert 'Текст = "Процедура Ложная()"' in первая
    assert 'Сообщить("// КонецПроцедуры Процедура Ложная()")' in первая
    assert "МаркерВторой" not in первая


def test_get_procedure_комментарий_с_ложными_границами_остаётся_в_своём_теле(
    корень_кода, реестр_из_кода
):
    файл = корень_кода / "CommonModules" / "ОбщийПример" / "Ext" / "Module.bsl"
    файл.write_text(
        "Процедура Первая() Экспорт\n"
        "    // Процедура Ложная() КонецПроцедуры\n"
        "    МаркерПервой = 1;\n"
        "КонецПроцедуры Процедура Вторая() Экспорт "
        "МаркерВторой = 2; КонецПроцедуры\n",
        encoding="utf-8",
    )
    реестр = реестр_из_кода(корень_кода)

    первая = tools.get_procedure(
        реестр, "ОбщийМодуль.ОбщийПример::Первая", config="Пример"
    )
    вторая = tools.get_procedure(
        реестр, "ОбщийМодуль.ОбщийПример::Вторая", config="Пример"
    )

    assert "Процедура Ложная() КонецПроцедуры" in первая
    assert "МаркерПервой" in первая and "МаркерВторой" not in первая
    assert "МаркерВторой" in вторая and "МаркерПервой" not in вторая


def test_get_procedure_многострочная_и_частичная_границы_остаются_точными(
    корень_кода, реестр_из_кода
):
    файл = корень_кода / "CommonModules" / "ОбщийПример" / "Ext" / "Module.bsl"
    файл.write_text(
        "Процедура Закрытая(\n    Первый,\n    Второй) Экспорт\n"
        "    МаркерЗакрытой = 1;\n"
        "КонецПроцедуры Процедура Обрыв()\n"
        "    МаркерОбрыва = 2;\n",
        encoding="utf-8",
    )
    реестр = реестр_из_кода(корень_кода)

    закрытая = tools.get_procedure(
        реестр, "ОбщийМодуль.ОбщийПример::Закрытая", config="Пример"
    )
    обрыв = tools.get_procedure(
        реестр, "ОбщийМодуль.ОбщийПример::Обрыв", config="Пример"
    )

    assert "Первый, Второй) Экспорт" in закрытая
    assert "МаркерЗакрытой" in закрытая and "МаркерОбрыва" not in закрытая
    assert "Процедура Обрыв" not in закрытая
    assert "разобран не до конца" in обрыв
    assert "МаркерОбрыва" in обрыв and "МаркерЗакрытой" not in обрыв
    тело_обрыва = обрыв.split("```bsl\n", 1)[1].split("\n```", 1)[0]
    assert тело_обрыва.startswith("Процедура Обрыв()")


def test_get_procedure_адрес_процедуры_отдаёт_тело_и_контекст(
    корень_кода, реестр_из_кода
):
    configuration = build_configuration(name="Пример")
    configuration.objects["ОбщийМодуль.ОбщийПример"] = MetadataObject(
        full_name="ОбщийМодуль.ОбщийПример",
        kind="ОбщийМодуль",
        name="ОбщийПример",
        props={
            "server": True,
            "client_managed": False,
            "server_call": True,
            "global": False,
            "privileged": True,
            "external_connection": True,
            "return_values_reuse": "DuringCall",
        },
    )
    реестр = реестр_из_кода(
        корень_кода, configuration=configuration
    )

    ответ = tools.get_procedure(
        реестр,
        "ОбщийМодуль.ОбщийПример::Сложить",
        config="Пример",
    )

    до_тела = ответ.split("Возврат", 1)[0]
    assert "Сигнатура" in до_тела
    assert "Сервер" in до_тела
    assert "Вызов сервера" in до_тела
    assert "Привилегированный" in до_тела
    assert "Внешнее соединение" not in до_тела
    assert "Повторное использование" not in до_тела
    assert "DuringCall" not in до_тела
    assert "Возврат Первый + Второй" in ответ


def test_get_procedure_директива_формы_и_событие_рядом(реестр_с_кодом):
    адрес = "Справочник.Пример.Форма.ФормаЭлемента"

    оглавление = tools.get_procedure(
        реестр_с_кодом, адрес, config="Пример"
    )
    карточка = tools.get_procedure(
        реестр_с_кодом, f"{адрес}::ПриОткрытии", config="Пример"
    )

    assert "ПриОткрытии" in оглавление
    assert "OnOpen" in оглавление
    до_тела = карточка.split("КонецПроцедуры", 1)[0]
    assert "&НаКлиенте" in до_тела
    assert "OnOpen" in до_тела


def test_get_procedure_длинное_тело_даёт_окно_и_готовое_продолжение(
    корень_кода, реестр_из_кода
):
    каталог = корень_кода / "CommonModules" / "Длинный" / "Ext"
    каталог.mkdir(parents=True)
    строки = ["Процедура Огромная() Экспорт"]
    строки += [f"    Маркер{n:03d} = {n};" for n in range(240)]
    строки += ["КонецПроцедуры"]
    (каталог / "Module.bsl").write_text("\n".join(строки) + "\n", encoding="utf-8")
    реестр = реестр_из_кода(корень_кода)
    адрес = "ОбщийМодуль.Длинный::Огромная"

    первая = tools.get_procedure(реестр, адрес, config="Пример")
    вторая = tools.get_procedure(
        реестр, адрес, config="Пример", start_line=200, lines=200
    )

    assert "Маркер000" in первая
    assert "Маркер239" not in первая
    assert 'start_line=200, lines=200' in первая
    assert "Маркер239" in вторая


def test_get_procedure_окна_используют_одни_нормализованные_строки_без_разрыва(
    корень_кода, реестр_из_кода
):
    файл = корень_кода / "CommonModules" / "ОбщийПример" / "Ext" / "Module.bsl"
    файл.write_bytes(
        "Процедура Окна() Экспорт\r\n"
        "    Маркер0 = 0; // одиночный CR\rостаётся внутри строки\r\n"
        "    Маркер1 = 1;\r\n"
        "    Маркер2 = 2;\r\n"
        "КонецПроцедуры\r\n".encode("utf-8")
    )
    реестр = реестр_из_кода(корень_кода)
    адрес = "ОбщийМодуль.ОбщийПример::Окна"

    первое = tools.get_procedure(
        реестр, адрес, config="Пример", start_line=0, lines=3
    )
    второе = tools.get_procedure(
        реестр, адрес, config="Пример", start_line=3, lines=3
    )

    assert "Маркер0" in первое and "Маркер1" in первое
    assert "Маркер2" not in первое
    assert "Маркер1" not in второе and "Маркер2" in второе
    assert "start_line=3, lines=3" in первое


def test_get_procedure_граница_markdown_длиннее_кавычек_в_теле(
    корень_кода, реестр_из_кода
):
    файл = корень_кода / "CommonModules" / "ОбщийПример" / "Ext" / "Module.bsl"
    файл.write_text(
        "Процедура Разметка() Экспорт\n"
        "    // ``` не должна закрыть блок кода\n"
        "КонецПроцедуры\n",
        encoding="utf-8",
    )
    реестр = реестр_из_кода(корень_кода)

    ответ = tools.get_procedure(
        реестр, "ОбщийМодуль.ОбщийПример::Разметка", config="Пример"
    )

    assert "````bsl\n" in ответ
    assert "\n````\n" in ответ
    assert "// ``` не должна закрыть блок кода" in ответ


@pytest.mark.parametrize(
    ("start_line", "lines"),
    [(-1, 200), (0, 0), (0, 201), (True, 20), (0, True), ("0", 20)],
)
def test_get_procedure_неверное_окно_отклоняется(
    реестр_с_кодом, start_line, lines
):
    with pytest.raises(RegistryError, match="start_line|lines"):
        tools.get_procedure(
            реестр_с_кодом,
            "ОбщийМодуль.ОбщийПример::Сложить",
            config="Пример",
            start_line=start_line,
            lines=lines,
        )


def test_get_procedure_промах_предлагает_похожий_адрес(реестр_с_кодом):
    ответ = tools.get_procedure(
        реестр_с_кодом,
        "ОбщийМодуль.ОбщийПример::Слжить",
        config="Пример",
    )

    assert "возможно, имелось в виду" in ответ.lower()
    assert "ОбщийМодуль.ОбщийПример::Сложить" in ответ


def test_get_procedure_частичный_разбор_и_расхождение_версий_над_телом(
    корень_кода, реестр_из_кода
):
    файл = корень_кода / "CommonModules" / "ОбщийПример" / "Ext" / "Module.bsl"
    файл.write_text(
        "Процедура Оборванная()\n    МаркерТела = 1;\n", encoding="utf-8"
    )
    configuration = build_configuration(name="Пример", version="2.0")
    реестр = реестр_из_кода(
        корень_кода, configuration=configuration, code_version="1.0"
    )

    ответ = tools.get_procedure(
        реестр,
        "ОбщийМодуль.ОбщийПример::Оборванная",
        config="Пример",
    )

    позиция_тела = ответ.index("МаркерТела")
    assert ответ.index("разобран не до конца") < позиция_тела
    assert ответ.index("версии 1.0") < позиция_тела
    assert "версии 2.0" in ответ


@pytest.mark.parametrize("state", ["missing", "building", "error"])
def test_get_procedure_честно_показывает_состояние_кода(
    tmp_path, корень_кода, реестр_из_кода, state
):
    if state == "missing":
        incoming = tmp_path / "incoming"
        incoming.mkdir()
        реестр = Registry(tmp_path / "data")
        реестр.add_configuration(
            write_export(incoming, build_configuration(name="Пример"))
        )
    else:
        реестр = реестр_из_кода(корень_кода)
        loaded = реестр.resolve("Пример").modules
        with реестр._lock:
            loaded.готов = False
            loaded.этап = (2, 4)
            loaded.название_этапа = "вызовы"
            loaded.прогресс = (1, 3)
            if state == "error":
                loaded.source.status = STATUS_ERROR
                loaded.source.error = "отказ сборки"

    ответ = tools.get_procedure(
        реестр, "ОбщийМодуль.ОбщийПример", config="Пример"
    )

    if state == "missing":
        assert "выгрузка в файлы не загружена" in ответ
    elif state == "building":
        assert "этап 2/4" in ответ and "1 из 3" in ответ
    else:
        assert "отказ сборки" in ответ


@pytest.mark.parametrize(
    ("annotation", "procedure_name", "base_expected"),
    [
        ("ИзменениеИКонтроль", "Правка", True),
        ("После", "Цель", False),
        ("Перед", "Цель", False),
        ("Вместо", "Цель", False),
    ],
)
def test_get_procedure_четыре_семантики_аннотаций_расширения(
    корень_кода,
    реестр_из_кода,
    архив_кода,
    annotation,
    procedure_name,
    base_expected,
):
    файл = корень_кода / "CommonModules" / "ОбщийПример" / "Ext" / "Module.bsl"
    файл.write_text(
        "Процедура Цель() Экспорт\n"
        "    БазоваяСтрока = 1;\n"
        "КонецПроцедуры\n",
        encoding="utf-8",
    )
    реестр = реестр_из_кода(корень_кода)

    if annotation == "ИзменениеИКонтроль":
        extension_text = (
            '&ИзменениеИКонтроль("Цель")\n'
            "Процедура Правка()\n"
            "#Удаление\n    УдаляемаяСтрока = 1;\n#КонецУдаления\n"
            "#Вставка\n    ВставленнаяСтрока = 2;\n#КонецВставки\n"
            "    ВнеДельты = 3;\n"
            "КонецПроцедуры\n"
        )
    else:
        extension_text = (
            f'&{annotation}("Цель")\n'
            "Процедура Цель() Экспорт\n"
            f"    Тело{annotation} = 1;\n"
            "КонецПроцедуры\n"
        )
    файл.write_text(extension_text, encoding="utf-8")
    реестр.add_modules(
        архив_кода(корень_кода, extension="Доп"), configuration="Пример"
    )

    ответ = tools.get_procedure(
        реестр,
        f"ОбщийМодуль.ОбщийПример::{procedure_name}",
        config="Пример",
        extension="Доп",
    )

    assert f"&{annotation}" in ответ
    if base_expected:
        assert "БазоваяСтрока" in ответ
        assert "#Удаление" in ответ
        assert "УдаляемаяСтрока" in ответ
        assert "#Вставка" in ответ
        assert "ВставленнаяСтрока" in ответ
        assert "ВнеДельты" not in ответ
    else:
        assert f"Тело{annotation}" in ответ
        assert "БазоваяСтрока" not in ответ
    if annotation == "Вместо":
        assert "основной конфигурации" in ответ.lower()


def test_get_procedure_чужое_расширение_предупреждает_над_телом_без_текста(
    корень_кода, реестр_из_кода, архив_кода
):
    файл = корень_кода / "CommonModules" / "ОбщийПример" / "Ext" / "Module.bsl"
    файл.write_text(
        "Процедура Цель() Экспорт\n    БазовоеТело = 1;\nКонецПроцедуры\n",
        encoding="utf-8",
    )
    реестр = реестр_из_кода(корень_кода)
    for extension, annotation in (("ДопА", "После"), ("ДопБ", "Вместо")):
        файл.write_text(
            f'&{annotation}("Цель")\nПроцедура Цель()\n'
            f"    СекретноеТело{extension} = 1;\nКонецПроцедуры\n",
            encoding="utf-8",
        )
        реестр.add_modules(
            архив_кода(корень_кода, extension=extension),
            configuration="Пример",
        )

    ответ = tools.get_procedure(
        реестр, "ОбщийМодуль.ОбщийПример::Цель", config="Пример"
    )

    позиция_тела = ответ.index("БазовоеТело")
    assert ответ.index("ДопА") < позиция_тела
    assert ответ.index("ДопБ") < позиция_тела
    assert "&После" in ответ and "&Вместо" in ответ
    assert "СекретноеТело" not in ответ


@pytest.mark.parametrize(
    "annotation", ["ИзменениеИКонтроль", "ChangeAndValidate"]
)
def test_get_procedure_чужое_изменение_и_контроль_не_названо_отдельным_вызовом(
    корень_кода, реестр_из_кода, архив_кода, annotation
):
    файл = корень_кода / "CommonModules" / "ОбщийПример" / "Ext" / "Module.bsl"
    файл.write_text(
        "Процедура Цель() Экспорт\n    БазовоеТело = 1;\nКонецПроцедуры\n",
        encoding="utf-8",
    )
    реестр = реестр_из_кода(корень_кода)
    файл.write_text(
        f'&{annotation}("Цель")\nПроцедура Правка()\n'
        "#Вставка\n    ЧужаяДельта = 1;\n#КонецВставки\n"
        "КонецПроцедуры\n",
        encoding="utf-8",
    )
    реестр.add_modules(
        архив_кода(корень_кода, extension="Доп"), configuration="Пример"
    )

    ответ = tools.get_procedure(
        реестр, "ОбщийМодуль.ОбщийПример::Цель", config="Пример"
    )

    позиция_тела = ответ.index("БазовоеТело")
    assert ответ.index("меняет типовое тело блоками") < позиция_тела
    assert "тоже выполняется" not in ответ
    assert "ЧужаяДельта" not in ответ


@pytest.mark.parametrize("extension", [None, "Доп"], ids=["modules", "extension"])
@pytest.mark.parametrize("action", ["reparse", "remove"])
def test_get_procedure_смена_поколения_не_смешивает_тело(
    корень_кода,
    реестр_из_кода,
    архив_кода,
    monkeypatch,
    extension,
    action,
):
    каталог = корень_кода / "CommonModules" / "ГонкаКарточки" / "Ext"
    каталог.mkdir(parents=True)
    файл = каталог / "Module.bsl"
    файл.write_text(
        "Процедура Сменить(Старый) Экспорт\n    СтароеТело = 1;\nКонецПроцедуры\n",
        encoding="utf-8",
    )
    реестр = реестр_из_кода(корень_кода, extension=extension)
    файл.write_text(
        "// новое\nПроцедура Сменить(Новый) Экспорт\n    НовоеТело = 2;\nКонецПроцедуры\n",
        encoding="utf-8",
    )
    новый = архив_кода(корень_кода, extension=extension)
    начато = threading.Event()
    отпустить = threading.Event()
    настоящее_чтение = tools.прочитать_модуль
    первый = True

    def задержать(путь):
        nonlocal первый
        if первый and "ГонкаКарточки" in путь.parts:
            первый = False
            начато.set()
            отпустить.wait(timeout=3)
        return настоящее_чтение(путь)

    monkeypatch.setattr(tools, "прочитать_модуль", задержать)
    ответы: list[str] = []
    ошибки: list[BaseException] = []

    def читать():
        try:
            ответы.append(
                tools.get_procedure(
                    реестр,
                    "ОбщийМодуль.ГонкаКарточки::Сменить",
                    config="Пример",
                    extension=extension,
                )
            )
        except BaseException as error:
            ошибки.append(error)

    поток = threading.Thread(target=читать)
    поток.start()
    try:
        assert начато.wait(timeout=1)
        source_id = "Пример:ext:Доп" if extension else "Пример:modules"
        if action == "reparse":
            реестр.add_modules(новый, configuration="Пример")
        else:
            реестр.remove(source_id)
    finally:
        отпустить.set()
        поток.join(timeout=3)

    assert not поток.is_alive()
    if action == "reparse":
        assert not ошибки
        assert "Новый" in ответы[0] and "НовоеТело" in ответы[0]
        assert "Старый" not in ответы[0] and "СтароеТело" not in ответы[0]
    elif extension:
        assert len(ошибки) == 1 and "не загружено" in str(ошибки[0])
    else:
        assert not ошибки
        assert "выгрузка в файлы не загружена" in ответы[0]


@pytest.mark.parametrize("extension", [None, "Доп"], ids=["modules", "extension"])
def test_get_procedure_не_раскрывает_путь_при_ошибке_текущего_файла(
    корень_кода, реестр_из_кода, monkeypatch, extension
):
    реестр = реестр_из_кода(корень_кода, extension=extension)

    def отказ(_путь):
        raise PermissionError(13, "Permission denied", "/private/secret/Module.bsl")

    monkeypatch.setattr(tools, "прочитать_модуль", отказ)

    with pytest.raises(RegistryError) as ошибка:
        tools.get_procedure(
            реестр,
            "ОбщийМодуль.ОбщийПример::Сложить",
            config="Пример",
            extension=extension,
        )

    assert "файл недоступен" in str(ошибка.value)
    assert "/private/" not in str(ошибка.value)
    assert "Permission denied" not in str(ошибка.value)


@pytest.mark.parametrize(
    "changed", ["base", "extension"], ids=["base-changed", "extension-changed"]
)
def test_get_procedure_все_части_двух_корпусов_проходят_единый_cas(
    корень_кода,
    реестр_из_кода,
    архив_кода,
    monkeypatch,
    changed,
):
    файл = корень_кода / "CommonModules" / "ОбщийПример" / "Ext" / "Module.bsl"
    файл.write_text(
        "Процедура Цель() Экспорт\n"
        "    СтараяБаза = 1;\n"
        "КонецПроцедуры\n",
        encoding="utf-8",
    )
    реестр = реестр_из_кода(корень_кода)
    файл.write_text(
        '&ИзменениеИКонтроль("Цель")\n'
        "Процедура Правка()\n"
        "#Вставка\n    СтараяДельта = 1;\n#КонецВставки\n"
        "КонецПроцедуры\n",
        encoding="utf-8",
    )
    реестр.add_modules(
        архив_кода(корень_кода, extension="Доп"), configuration="Пример"
    )

    if changed == "base":
        файл.write_text(
            "Процедура Цель() Экспорт\n"
            "    НоваяБаза = 2;\n"
            "КонецПроцедуры\n",
            encoding="utf-8",
        )
        новый = архив_кода(корень_кода)
    else:
        файл.write_text(
            '&ИзменениеИКонтроль("Цель")\n'
            "Процедура Правка()\n"
            "#Вставка\n    НоваяДельта = 2;\n#КонецВставки\n"
            "КонецПроцедуры\n",
            encoding="utf-8",
        )
        новый = архив_кода(корень_кода, extension="Доп")

    дошли_до_cas = threading.Event()
    отпустить = threading.Event()
    настоящий_cas = tools._modules_package_is_current
    первый = True

    def задержать_cas(registry, loaded_modules):
        nonlocal первый
        if первый:
            первый = False
            дошли_до_cas.set()
            отпустить.wait(timeout=3)
        return настоящий_cas(registry, loaded_modules)

    monkeypatch.setattr(tools, "_modules_package_is_current", задержать_cas)
    ответы: list[str] = []
    ошибки: list[BaseException] = []

    def читать():
        try:
            ответы.append(
                tools.get_procedure(
                    реестр,
                    "ОбщийМодуль.ОбщийПример::Правка",
                    config="Пример",
                    extension="Доп",
                    start_line=0,
                    lines=50,
                )
            )
        except BaseException as error:
            ошибки.append(error)

    поток = threading.Thread(target=читать)
    поток.start()
    try:
        assert дошли_до_cas.wait(timeout=1)
        реестр.add_modules(новый, configuration="Пример")
    finally:
        отпустить.set()
        поток.join(timeout=3)

    assert not поток.is_alive()
    assert not ошибки
    assert len(ответы) == 1
    if changed == "base":
        assert "НоваяБаза" in ответы[0] and "СтараяБаза" not in ответы[0]
        assert "СтараяДельта" in ответы[0]
    else:
        assert "НоваяДельта" in ответы[0] and "СтараяДельта" not in ответы[0]
        assert "СтараяБаза" in ответы[0]


def test_ранжирование_стабильно(корень_кода, реестр_из_кода):
    for имя in ("Альфа", "Бета", "Гамма"):
        каталог = корень_кода / "CommonModules" / имя / "Ext"
        каталог.mkdir(parents=True)
        (каталог / "Module.bsl").write_text(
            "// Проверяет общий остаток.\n"
            f"Процедура Проверить{имя}() Экспорт\nКонецПроцедуры\n",
            encoding="utf-8",
        )
    реестр = реестр_из_кода(корень_кода)
    ответы = [
        tools.search_procedures(
            реестр, "общий остаток", config="Пример", limit=10
        )
        for _ in range(3)
    ]

    assert ответы[0] == ответы[1] == ответы[2]
    assert ответы[0].count("::Проверить") == 3


# --- Блоки кода в существующих ответах (задача 15) ----------------------


def _конфигурация_для_блоков() -> object:
    конфигурация = build_configuration(name="Пример")
    документ = MetadataObject(
        full_name="Документ.Пример",
        kind="Документ",
        name="Пример",
        synonym="Пример",
        attributes=[Field(name="Ссылка")],
    )
    связанный = MetadataObject(
        full_name="Справочник.Связанный",
        kind="Справочник",
        name="Связанный",
        synonym="Связанный",
    )
    документ.attributes[0].types = [связанный.full_name]
    конфигурация.objects = {
        документ.full_name: документ,
        связанный.full_name: связанный,
    }
    return конфигурация


def _код_объекта(корень, *, module="ObjectModule.bsl", marker="Текущая"):
    каталог = корень / "Documents" / "Пример" / "Ext"
    каталог.mkdir(parents=True, exist_ok=True)
    (каталог / module).write_text(
        f"Процедура {marker}()\nКонецПроцедуры\n", encoding="utf-8"
    )


def _форма_объекта(корень):
    каталог = корень / "Documents" / "Пример" / "Forms" / "Основная" / "Ext"
    (каталог / "Form").mkdir(parents=True, exist_ok=True)
    (каталог / "Form" / "Module.bsl").write_text(
        "Процедура СобытиеФормы()\nКонецПроцедуры\n", encoding="utf-8"
    )
    (каталог / "Form.xml").write_text("<Form/>", encoding="utf-8")


def _форма_без_модуля(корень):
    каталог = корень / "Documents" / "Пример" / "Forms" / "БезКода" / "Ext"
    каталог.mkdir(parents=True, exist_ok=True)
    (каталог / "Form.xml").write_text("<Form/>", encoding="utf-8")


def _код_расширения(корень, *, variant="full"):
    первый = корень / "Documents" / "Пример" / "Ext"
    первый.mkdir(parents=True, exist_ok=True)
    тексты = {
        "full": (
            '&Вместо("Старая")\nПроцедура ВместоСтарой()\nКонецПроцедуры\n'
            '&После("ПослеСтарой")\nПроцедура ПослеСтарой()\nКонецПроцедуры\n'
            "Процедура Собственная() Экспорт\nКонецПроцедуры\n"
        ),
        "new": (
            '&Перед("Новая")\nПроцедура ПередНовой()\nКонецПроцедуры\n'
            "Процедура НоваяСобственная() Экспорт\nКонецПроцедуры\n"
        ),
        "related": (
            '&Вместо("Получить")\nПроцедура Получить()\nКонецПроцедуры\n'
        ),
        "unrelated": (
            '&Вместо("Получить")\nПроцедура Получить()\nКонецПроцедуры\n'
        ),
    }
    (первый / "ObjectModule.bsl").write_text(тексты[variant], encoding="utf-8")
    if variant == "full":
        второй = корень / "Catalogs" / "Связанный" / "Ext"
        второй.mkdir(parents=True, exist_ok=True)
        (второй / "ManagerModule.bsl").write_text(
            '&ИзменениеИКонтроль("Проверить")\n'
            "Процедура ИзменитьПроверку()\nКонецПроцедуры\n"
            '&Перед("Записать")\nПроцедура ПередЗаписью()\nКонецПроцедуры\n',
            encoding="utf-8",
        )
    elif variant == "related":
        путь = корень / "Catalogs" / "Связанный" / "Ext"
        путь.mkdir(parents=True, exist_ok=True)
        (путь / "ManagerModule.bsl").write_text(
            (первый / "ObjectModule.bsl").read_text(encoding="utf-8"),
            encoding="utf-8",
        )
        (первый / "ObjectModule.bsl").unlink()


@pytest.mark.parametrize("state", ["ready", "building", "error", "missing"])
def test_list_configurations_показывает_реальное_состояние_индекса_кода(
    tmp_path, корень_кода, реестр_из_кода, state
):
    if state == "missing":
        incoming = tmp_path / "incoming"
        incoming.mkdir()
        реестр = Registry(tmp_path / "data")
        реестр.add_configuration(
            write_export(incoming, build_configuration(name="Пример"))
        )
    else:
        реестр = реестр_из_кода(корень_кода)
        loaded = реестр.resolve("Пример").modules
        if state != "ready":
            with реестр._lock:
                loaded.готов = False
                loaded.этап = (2, 4)
                loaded.название_этапа = "вызовы"
                loaded.прогресс = (1, 3)
                if state == "error":
                    loaded.source.status = STATUS_ERROR
                    loaded.source.error = "отказ сборки"

    ответ = tools.list_configurations(реестр)

    if state == "ready":
        assert "Индекс кода: готов" in ответ
        assert "процедур" in ответ and "модулей" in ответ and "форм" in ответ
    elif state == "building":
        assert "Индекс кода: строится" in ответ
        assert "этап 2/4" in ответ and "1 из 3" in ответ
    elif state == "error":
        assert "Индекс кода: ошибка" in ответ and "отказ сборки" in ответ
    else:
        assert "Индекс кода: не загружен" in ответ


def test_overview_показывает_подключённый_провайдер_модулей(реестр_с_кодом):
    строка = реестр_с_кодом.overview()[0]

    assert строка["providers"]["modules"] is True


def test_list_configurations_не_раскрывает_путь_из_ошибки(
    реестр_с_кодом,
):
    loaded = реестр_с_кодом.resolve("Пример").modules
    with реестр_с_кодом._lock:
        loaded.готов = False
        loaded.source.status = STATUS_ERROR
        loaded.source.error = "сбой /private/tmp/secret/Module.bsl"

    ответ = tools.list_configurations(реестр_с_кодом)

    assert "Индекс кода: ошибка" in ответ
    assert "/private/" not in ответ and "Module.bsl" not in ответ


def test_list_configurations_даёт_полную_сводку_расширения(
    tmp_path, корень_кода, реестр_из_кода, архив_кода
):
    реестр = реестр_из_кода(
        корень_кода, configuration=_конфигурация_для_блоков()
    )
    расширение = tmp_path / "extension"
    _код_расширения(расширение)
    реестр.add_modules(
        архив_кода(расширение, extension="Доп"), configuration="Пример"
    )

    ответ = tools.list_configurations(реестр)

    assert "Расширение `Доп`" in ответ
    assert "перекрытий: 4 в 2 модулях" in ответ.lower()
    for вид in ("Вместо: 1", "После: 1", "Перед: 1", "ИзменениеИКонтроль: 1"):
        assert вид in ответ
    assert "собственных процедур: 1" in ответ.lower()
    assert 'extension="Доп"' in ответ


def test_расширения_в_сводке_упорядочены_стабильно(
    tmp_path, корень_кода, реестр_из_кода, архив_кода
):
    реестр = реестр_из_кода(корень_кода)
    for имя in ("Бета", "Альфа"):
        корень = tmp_path / имя
        _код_расширения(корень, variant="new")
        реестр.add_modules(
            архив_кода(корень, extension=имя), configuration="Пример"
        )

    ответы = [tools.list_configurations(реестр) for _ in range(3)]

    assert ответы[0] == ответы[1] == ответы[2]
    assert ответы[0].index("Расширение `Альфа`") < ответы[0].index(
        "Расширение `Бета`"
    )


@pytest.mark.parametrize("state", ["building", "error"])
def test_list_configurations_показывает_состояние_расширения(
    tmp_path, корень_кода, реестр_из_кода, архив_кода, state
):
    реестр = реестр_из_кода(корень_кода)
    расширение = tmp_path / "extension"
    _код_расширения(расширение, variant="new")
    реестр.add_modules(
        архив_кода(расширение, extension="Доп"), configuration="Пример"
    )
    loaded = реестр.resolve("Пример", extension="Доп").extension
    with реестр._lock:
        loaded.готов = False
        loaded.этап = (1, 4)
        loaded.название_этапа = "оглавление"
        loaded.прогресс = (7, 11)
        if state == "error":
            loaded.source.status = STATUS_ERROR
            loaded.source.error = "ошибка расширения"

    ответ = tools.list_configurations(реестр)

    assert "Расширение `Доп`" in ответ
    if state == "building":
        assert "строится" in ответ and "этап 1/4" in ответ and "7 из 11" in ответ
    else:
        assert "ошибка" in ответ and "ошибка расширения" in ответ


def test_сводка_расширения_не_материализует_все_записи(
    tmp_path, корень_кода, реестр_из_кода, архив_кода, monkeypatch
):
    реестр = реестр_из_кода(корень_кода)
    расширение = tmp_path / "extension"
    _код_расширения(расширение, variant="full")
    реестр.add_modules(
        архив_кода(расширение, extension="Доп"), configuration="Пример"
    )
    monkeypatch.setattr(
        modules_index.Оглавление,
        "_запись",
        lambda *_: pytest.fail("полный корпус не должен материализоваться"),
    )

    ответ = tools.list_configurations(реестр)

    assert "Расширение `Доп`" in ответ and "перекрытий" in ответ.lower()


def test_get_object_показывает_код_по_уровню_детализации(
    tmp_path, реестр_из_кода
):
    корень = tmp_path / "code"
    _код_объекта(корень)
    _форма_объекта(корень)
    _форма_без_модуля(корень)
    реестр = реестр_из_кода(
        корень, configuration=_конфигурация_для_блоков()
    )

    brief = tools.get_object(реестр, "Документ.Пример", config="Пример", detail="brief")
    fields = tools.get_object(реестр, "Документ.Пример", config="Пример", detail="fields")
    full = tools.get_object(реестр, "Документ.Пример", config="Пример", detail="full")

    assert "Код объекта" not in brief and "Модули объекта" not in brief
    строки = [line for line in fields.splitlines() if line.startswith("Код объекта:")]
    assert строки == ["Код объекта: модулей 1, форм 2."]
    assert "МодульОбъекта" not in fields and "Форма.Основная" not in fields
    assert "## Модули объекта" in full and "Документ.Пример.МодульОбъекта" in full
    assert "## Формы объекта" in full and "Документ.Пример.Форма.Основная" in full
    assert "Документ.Пример.Форма.БезКода" in full
    assert "Текущая" not in full and "СобытиеФормы" not in full


def test_get_object_brief_вообще_не_обращается_к_провайдеру(
    tmp_path, реестр_из_кода, monkeypatch
):
    корень = tmp_path / "code"
    _код_объекта(корень)
    реестр = реестр_из_кода(
        корень, configuration=_конфигурация_для_блоков()
    )
    monkeypatch.setattr(
        tools,
        "_configuration_code_snapshot",
        lambda *_: pytest.fail("brief не должен обращаться к индексу кода"),
        raising=False,
    )

    ответ = tools.get_object(
        реестр, "Документ.Пример", config="Пример", detail="brief"
    )

    assert "Код объекта" not in ответ


@pytest.mark.parametrize("state", ["missing", "building", "error"])
def test_get_object_честно_показывает_недоступный_индекс_кода(
    tmp_path, реестр_из_кода, state
):
    корень = tmp_path / "code"
    _код_объекта(корень)
    конфигурация = _конфигурация_для_блоков()
    if state == "missing":
        incoming = tmp_path / "incoming"
        incoming.mkdir()
        реестр = Registry(tmp_path / "data")
        реестр.add_configuration(write_export(incoming, конфигурация))
    else:
        реестр = реестр_из_кода(корень, configuration=конфигурация)
        loaded = реестр.resolve("Пример").modules
        with реестр._lock:
            loaded.готов = False
            loaded.этап = (3, 4)
            loaded.название_этапа = "формы"
            loaded.прогресс = (2, 5)
            if state == "error":
                loaded.source.status = STATUS_ERROR
                loaded.source.error = "отказ сборки"

    ответ = tools.get_object(
        реестр, "Документ.Пример", config="Пример", detail="fields"
    )

    assert ответ.count("Код объекта:") == 1
    if state == "missing":
        assert "код не загружен" in ответ.lower()
    elif state == "building":
        assert "строится" in ответ and "этап 3/4" in ответ and "2 из 5" in ответ
    else:
        assert "ошибка" in ответ and "отказ сборки" in ответ


def test_get_related_помечает_перекрытый_объект_расширением(
    tmp_path, реестр_из_кода, архив_кода
):
    база = tmp_path / "base"
    _код_объекта(база)
    реестр = реестр_из_кода(
        база, configuration=_конфигурация_для_блоков()
    )
    расширение = tmp_path / "extension"
    _код_расширения(расширение, variant="related")
    реестр.add_modules(
        архив_кода(расширение, extension="Доп"), configuration="Пример"
    )
    второе = tmp_path / "second-extension"
    _код_расширения(второе, variant="related")
    реестр.add_modules(
        архив_кода(второе, extension="Второе"), configuration="Пример"
    )
    своё = tmp_path / "own-extension"
    каталог = своё / "Catalogs" / "Связанный" / "Ext"
    каталог.mkdir(parents=True)
    (каталог / "ManagerModule.bsl").write_text(
        "Процедура Собственная()\nКонецПроцедуры\n", encoding="utf-8"
    )
    реестр.add_modules(
        архив_кода(своё, extension="ТолькоСвоя"), configuration="Пример"
    )

    ответ = tools.get_related(реестр, "Документ.Пример", config="Пример")

    строка = next(line for line in ответ.splitlines() if "Справочник.Связанный" in line)
    assert "перекрыто расширениями `Второе`, `Доп`" in строка
    assert "ТолькоСвоя" not in строка


def test_существующие_блоки_не_читают_тела_с_диска(
    tmp_path, реестр_из_кода, архив_кода, monkeypatch
):
    база = tmp_path / "base"
    _код_объекта(база)
    реестр = реестр_из_кода(
        база, configuration=_конфигурация_для_блоков()
    )
    расширение = tmp_path / "extension"
    _код_расширения(расширение, variant="related")
    реестр.add_modules(
        архив_кода(расширение, extension="Доп"), configuration="Пример"
    )
    monkeypatch.setattr(
        tools,
        "прочитать_модуль",
        lambda *_: pytest.fail("тело модуля не должно читаться"),
    )

    tools.list_configurations(реестр)
    tools.get_object(реестр, "Документ.Пример", config="Пример", detail="full")
    tools.get_related(реестр, "Документ.Пример", config="Пример")


@pytest.mark.parametrize("action", ["reparse", "remove"])
def test_get_object_не_смешивает_поколения_модулей(
    tmp_path, реестр_из_кода, архив_кода, monkeypatch, action
):
    старый = tmp_path / "old-base"
    _код_объекта(старый, module="ObjectModule.bsl", marker="Старый")
    реестр = реестр_из_кода(
        старый, configuration=_конфигурация_для_блоков()
    )
    новый = tmp_path / "new-base"
    _код_объекта(новый, module="ManagerModule.bsl", marker="Новый")
    новый_архив = архив_кода(новый)

    начато = threading.Event()
    отпустить = threading.Event()
    настоящее = tools._summarize_code
    первый = True

    def задержать(loaded):
        nonlocal первый
        результат = настоящее(loaded)
        if первый and loaded.source.id == "Пример:modules":
            первый = False
            начато.set()
            отпустить.wait(timeout=3)
        return результат

    monkeypatch.setattr(tools, "_summarize_code", задержать)
    ответы: list[str] = []
    ошибки: list[BaseException] = []
    поток = threading.Thread(
        target=lambda: _собрать_ответ(
            ответы,
            ошибки,
            lambda: tools.get_object(
                реестр, "Документ.Пример", config="Пример", detail="full"
            ),
        )
    )
    поток.start()
    try:
        assert начато.wait(timeout=1)
        if action == "reparse":
            реестр.add_modules(новый_архив, configuration="Пример")
        else:
            реестр.remove("Пример:modules")
    finally:
        отпустить.set()
        поток.join(timeout=3)

    assert not поток.is_alive() and not ошибки
    if action == "reparse":
        assert "МодульМенеджера" in ответы[0]
        assert "МодульОбъекта" not in ответы[0]
    else:
        assert "код не загружен" in ответы[0].lower()


@pytest.mark.parametrize("action", ["reparse", "remove"])
def test_list_configurations_не_смешивает_поколения_расширения(
    tmp_path, корень_кода, реестр_из_кода, архив_кода, monkeypatch, action
):
    реестр = реестр_из_кода(
        корень_кода, configuration=_конфигурация_для_блоков()
    )
    старый = tmp_path / "old-extension"
    _код_расширения(старый, variant="full")
    реестр.add_modules(
        архив_кода(старый, extension="Доп"), configuration="Пример"
    )
    новый = tmp_path / "new-extension"
    _код_расширения(новый, variant="new")
    новый_архив = архив_кода(новый, extension="Доп")

    начато = threading.Event()
    отпустить = threading.Event()
    настоящее = tools._summarize_code
    первый = True

    def задержать(loaded):
        nonlocal первый
        результат = настоящее(loaded)
        if первый and ":ext:" in loaded.source.id:
            первый = False
            начато.set()
            отпустить.wait(timeout=3)
        return результат

    monkeypatch.setattr(tools, "_summarize_code", задержать)
    ответы: list[str] = []
    ошибки: list[BaseException] = []
    поток = threading.Thread(
        target=lambda: _собрать_ответ(
            ответы, ошибки, lambda: tools.list_configurations(реестр)
        )
    )
    поток.start()
    try:
        assert начато.wait(timeout=1)
        if action == "reparse":
            реестр.add_modules(новый_архив, configuration="Пример")
        else:
            реестр.remove("Пример:ext:Доп")
    finally:
        отпустить.set()
        поток.join(timeout=3)

    assert not поток.is_alive() and not ошибки
    if action == "reparse":
        assert "Перед: 1" in ответы[0]
        assert "Вместо: 1" not in ответы[0]
    else:
        assert "Расширение `Доп`" not in ответы[0]


@pytest.mark.parametrize("action", ["reparse", "remove"])
def test_get_related_не_показывает_устаревшее_перекрытие(
    tmp_path, реестр_из_кода, архив_кода, monkeypatch, action
):
    база = tmp_path / "base"
    _код_объекта(база)
    реестр = реестр_из_кода(
        база, configuration=_конфигурация_для_блоков()
    )
    старое = tmp_path / "old-extension"
    _код_расширения(старое, variant="related")
    реестр.add_modules(
        архив_кода(старое, extension="Доп"), configuration="Пример"
    )
    новое = tmp_path / "new-extension"
    _код_расширения(новое, variant="new")
    новый_архив = архив_кода(новое, extension="Доп")

    начато = threading.Event()
    отпустить = threading.Event()
    настоящее = tools._summarize_code
    первый = True

    def задержать(loaded):
        nonlocal первый
        результат = настоящее(loaded)
        if первый and ":ext:" in loaded.source.id:
            первый = False
            начато.set()
            отпустить.wait(timeout=3)
        return результат

    monkeypatch.setattr(tools, "_summarize_code", задержать)
    ответы: list[str] = []
    ошибки: list[BaseException] = []
    поток = threading.Thread(
        target=lambda: _собрать_ответ(
            ответы,
            ошибки,
            lambda: tools.get_related(
                реестр, "Документ.Пример", config="Пример"
            ),
        )
    )
    поток.start()
    try:
        assert начато.wait(timeout=1)
        if action == "reparse":
            реестр.add_modules(новый_архив, configuration="Пример")
        else:
            реестр.remove("Пример:ext:Доп")
    finally:
        отпустить.set()
        поток.join(timeout=3)

    assert not поток.is_alive() and not ошибки
    строка = next(
        line for line in ответы[0].splitlines() if "Справочник.Связанный" in line
    )
    assert "перекрыто расшир" not in строка


def _собрать_ответ(ответы, ошибки, вызов):
    try:
        ответы.append(вызов())
    except BaseException as error:
        ошибки.append(error)


# --- Ревью задачи 15: согласованность списка и классификация форм --------


def _добавить_конфигурацию(реестр, каталог, *, name, version="1.0", platform="8.3.21"):
    каталог.mkdir(parents=True, exist_ok=True)
    конфигурация = build_configuration(name=name, version=version)
    конфигурация.platform = platform
    return реестр.add_configuration(
        write_export(каталог, конфигурация)
    )


def test_list_configurations_повторяет_весь_снимок_при_удалении_второй_конфигурации(
    tmp_path, monkeypatch
):
    реестр = Registry(tmp_path / "data")
    _добавить_конфигурацию(реестр, tmp_path / "а", name="А")
    _добавить_конфигурацию(реестр, tmp_path / "б", name="Б")
    настоящее = tools._configuration_code_snapshot
    дошли_до_а = threading.Event()
    продолжить = threading.Event()
    первый = True

    def задержать(registry, name):
        nonlocal первый
        результат = настоящее(registry, name)
        if name == "А" and первый:
            первый = False
            дошли_до_а.set()
            продолжить.wait(timeout=3)
        return результат

    monkeypatch.setattr(tools, "_configuration_code_snapshot", задержать)
    ответы: list[str] = []
    ошибки: list[BaseException] = []
    поток = threading.Thread(
        target=lambda: _собрать_ответ(
            ответы, ошибки, lambda: tools.list_configurations(реестр)
        )
    )
    поток.start()
    try:
        assert дошли_до_а.wait(timeout=1)
        реестр.remove("Б")
    finally:
        продолжить.set()
        поток.join(timeout=3)

    assert not поток.is_alive() and not ошибки
    assert "## А" in ответы[0] and "## Б" not in ответы[0]
    assert "config` явно" not in ответы[0]


def test_list_configurations_при_двух_сменах_возвращает_стабильную_ошибку(
    tmp_path, monkeypatch
):
    реестр = Registry(tmp_path / "data")
    _добавить_конфигурацию(реестр, tmp_path / "а", name="А")
    _добавить_конфигурацию(реестр, tmp_path / "б-0", name="Б", version="0")
    настоящее = tools._configuration_code_snapshot
    поколение = 0

    def менять_после_а(registry, name):
        nonlocal поколение
        результат = настоящее(registry, name)
        if name == "А":
            поколение += 1
            registry.remove("Б")
            _добавить_конфигурацию(
                registry,
                tmp_path / f"б-{поколение}",
                name="Б",
                version=str(поколение),
            )
        return результат

    monkeypatch.setattr(tools, "_configuration_code_snapshot", менять_после_а)

    with pytest.raises(RegistryError, match="изменились дважды; повторите") as caught:
        tools.list_configurations(реестр)

    assert "/" not in str(caught.value) and "\\" not in str(caught.value)


def test_list_configurations_не_смешивает_старое_отношение_справки_с_новым_покрытием(
    tmp_path, monkeypatch
):
    реестр = Registry(tmp_path / "data")
    _добавить_конфигурацию(
        реестр, tmp_path / "config", name="А", platform="8.3.21.1"
    )
    источник = реестр.add_syntax(
        write_syntax(tmp_path / "syntax", platform="8.3.21.1")
    )
    настоящее = tools._configuration_code_snapshot
    дошли_до_строки = threading.Event()
    продолжить = threading.Event()
    первый = True

    def задержать(registry, name):
        nonlocal первый
        результат = настоящее(registry, name)
        if первый:
            первый = False
            дошли_до_строки.set()
            продолжить.wait(timeout=3)
        return результат

    monkeypatch.setattr(tools, "_configuration_code_snapshot", задержать)
    ответы: list[str] = []
    ошибки: list[BaseException] = []
    поток = threading.Thread(
        target=lambda: _собрать_ответ(
            ответы, ошибки, lambda: tools.list_configurations(реестр)
        )
    )
    поток.start()
    try:
        assert дошли_до_строки.wait(timeout=1)
        реестр.remove(источник.id)
    finally:
        продолжить.set()
        поток.join(timeout=3)

    assert not поток.is_alive() and not ошибки
    assert "Синтаксис платформы: не подключён" in ответы[0]
    assert "Не загружено ни одной справки" in ответы[0]
    assert "версия совпадает" not in ответы[0]


def test_list_configurations_не_перечитывает_удалённую_справку_без_конфигураций(
    tmp_path, monkeypatch
):
    реестр = Registry(tmp_path / "data")
    источник = реестр.add_syntax(
        write_syntax(tmp_path / "syntax", platform="8.3.21.1")
    )
    настоящее = tools._syntax_only_overview
    начали = threading.Event()
    продолжить = threading.Event()
    первый = True

    def задержать(*args):
        nonlocal первый
        if первый:
            первый = False
            начали.set()
            продолжить.wait(timeout=3)
        return настоящее(*args)

    monkeypatch.setattr(tools, "_syntax_only_overview", задержать)
    ответы: list[str] = []
    ошибки: list[BaseException] = []
    поток = threading.Thread(
        target=lambda: _собрать_ответ(
            ответы, ошибки, lambda: tools.list_configurations(реестр)
        )
    )
    поток.start()
    try:
        assert начали.wait(timeout=1)
        реестр.remove(источник.id)
    finally:
        продолжить.set()
        поток.join(timeout=3)

    assert not поток.is_alive() and not ошибки
    assert ответы == [
        "Не загружено ни одной конфигурации, нет ни справки платформы, ни "
        "справки по языку запросов.\n\n"
        "Выгрузите структуру обработкой из `exporter-1c/` и загрузите архив."
    ]


def test_get_object_ставит_расхождение_версий_только_над_блоком_кода(
    tmp_path, реестр_из_кода
):
    корень = tmp_path / "code"
    _код_объекта(корень)
    конфигурация = _конфигурация_для_блоков()
    конфигурация.version = "2.0"
    реестр = реестр_из_кода(
        корень, configuration=конфигурация, code_version="1.0"
    )
    with реестр._lock:
        реестр.configurations["Пример"].config.warnings.append(
            "Обычная оговорка метаданных."
        )

    brief = tools.get_object(
        реестр, "Документ.Пример", config="Пример", detail="brief"
    )
    fields = tools.get_object(
        реестр, "Документ.Пример", config="Пример", detail="fields"
    )
    full = tools.get_object(
        реестр, "Документ.Пример", config="Пример", detail="full"
    )

    assert "Обычная оговорка метаданных" in brief
    assert "Код модулей выгружен для версии" not in brief
    assert fields.index("Код модулей выгружен для версии") < fields.index(
        "Код объекта:"
    )
    assert full.index("Код модулей выгружен для версии") < full.index(
        "## Модули объекта"
    )
    for ответ in (fields, full):
        assert ответ.count("Код модулей выгружен для версии") == 1
        assert ответ.index("Обычная оговорка метаданных") > ответ.index(
            "Код объекта" if ответ is fields else "## Модули объекта"
        )


def _модуль_формы(корень, *, object_name, form_name, common=False):
    if common:
        каталог = корень / "CommonForms" / object_name / "Ext" / "Form"
    else:
        каталог = (
            корень / "Documents" / object_name / "Forms" / form_name / "Ext" / "Form"
        )
    каталог.mkdir(parents=True, exist_ok=True)
    (каталог / "Module.bsl").write_text(
        "Процедура Обработчик()\nКонецПроцедуры\n", encoding="utf-8"
    )


def _xml_формы(корень, *, object_name, form_name, broken=False):
    каталог = корень / "Documents" / object_name / "Forms" / form_name / "Ext"
    каталог.mkdir(parents=True, exist_ok=True)
    (каталог / "Form.xml").write_text(
        "<Form>" if broken else "<Form/>", encoding="utf-8"
    )


def test_get_object_объединяет_формы_из_оглавления_и_form_xml_после_кэша(
    tmp_path, реестр_из_кода
):
    корень = tmp_path / "forms"
    _модуль_формы(корень, object_name="А", form_name="ТолькоМодуль")
    _xml_формы(корень, object_name="А", form_name="ТолькоXML")
    _модуль_формы(корень, object_name="А", form_name="Обе")
    _xml_формы(корень, object_name="А", form_name="Обе")
    _xml_формы(корень, object_name="А", form_name="Битая", broken=True)
    _модуль_формы(корень, object_name="АБ", form_name="Чужая")
    конфигурация = build_configuration(name="Пример")
    конфигурация.objects = {
        name: MetadataObject(full_name=name, kind="Документ", name=name.split(".")[1])
        for name in ("Документ.А", "Документ.АБ")
    }
    реестр = реестр_из_кода(корень, configuration=конфигурация)
    реестр.save()
    заново = Registry(реестр.data_dir)
    заново.startup()

    ответ = tools.get_object(
        заново, "Документ.А", config="Пример", detail="full"
    )

    for форма in ("ТолькоМодуль", "ТолькоXML", "Обе", "Битая"):
        assert f"Документ.А.Форма.{форма}" in ответ
    assert "Документ.АБ.Форма.Чужая" not in ответ
    блок_модулей = ответ.split("## Модули объекта", 1)[1].split(
        "## Формы объекта", 1
    )[0]
    assert "ТолькоМодуль" not in блок_модулей


def test_get_object_считает_модуль_общей_формы_формой(tmp_path, реестр_из_кода):
    корень = tmp_path / "common-form"
    _модуль_формы(
        корень, object_name="Панель", form_name="", common=True
    )
    конфигурация = build_configuration(name="Пример")
    конфигурация.objects = {
        "ОбщаяФорма.Панель": MetadataObject(
            full_name="ОбщаяФорма.Панель",
            kind="ОбщаяФорма",
            name="Панель",
        )
    }
    реестр = реестр_из_кода(корень, configuration=конфигурация)

    fields = tools.get_object(
        реестр, "ОбщаяФорма.Панель", config="Пример", detail="fields"
    )
    full = tools.get_object(
        реестр, "ОбщаяФорма.Панель", config="Пример", detail="full"
    )

    assert "Код объекта: модулей 0, форм 1." in fields
    assert "ОбщаяФорма.Панель" in full.split("## Формы объекта", 1)[1]
