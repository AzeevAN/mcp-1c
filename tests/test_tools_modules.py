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
from mcp1c.registry import STATUS_ERROR, Registry, RegistryError

from conftest import build_configuration, write_export


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
