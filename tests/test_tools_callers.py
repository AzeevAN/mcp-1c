"""Публичный обратный поиск вызовов поверх боевого ``Registry``.

Каждый корпус загружается через ``add_configuration``/``add_modules``.
Тесты не подставляют придуманный ``LoadedModules`` и не читают ``data/``.
"""

from __future__ import annotations

import threading

import pytest

from mcp1c import modules_index, tools
from mcp1c.model import MetadataObject
from mcp1c.registry import STATUS_ERROR, Registry, RegistryError

from conftest import build_configuration, write_export
from module_samples import v8_container_bytes


TARGET = "ОбщийМодуль.Цель::Обработать"


def _модуль(корень, имя: str, текст: str):
    каталог = корень / "CommonModules" / имя / "Ext"
    каталог.mkdir(parents=True, exist_ok=True)
    (каталог / "Module.bsl").write_text(текст, encoding="utf-8")


def _цель(корень):
    _модуль(
        корень,
        "Цель",
        "Процедура Обработать() Экспорт\nКонецПроцедуры\n",
    )


def _конфигурация_с_привязками():
    config = build_configuration(name="Пример")
    общий = MetadataObject(
        full_name="ОбщийМодуль.Цель",
        kind="ОбщийМодуль",
        name="Цель",
    )
    подписка = MetadataObject(
        full_name="ПодпискаНаСобытие.ПослеЗаписи",
        kind="ПодпискаНаСобытие",
        name="ПослеЗаписи",
        props={"handler": "Цель.Обработать"},
    )
    задание = MetadataObject(
        full_name="РегламентноеЗадание.НочнойОбмен",
        kind="РегламентноеЗадание",
        name="НочнойОбмен",
        props={"method": "Цель.Обработать"},
    )
    config.objects.update(
        {объект.full_name: объект for объект in (общий, подписка, задание)}
    )
    return config


def test_get_callers_группирует_подтверждённые_места_и_называет_владельца(
    корень_кода, реестр_из_кода
):
    _цель(корень_кода)
    _модуль(
        корень_кода,
        "Зовущий",
        "Процедура Первый()\n"
        "    Цель.Обработать();\n"
        "КонецПроцедуры\n"
        "Процедура Второй()\n"
        "    Цель.Обработать();\n"
        "КонецПроцедуры\n",
    )
    реестр = реестр_из_кода(корень_кода)

    ответ = tools.get_callers(реестр, TARGET.lower(), config="Пример")

    заголовки = [
        "## Места вызова в коде",
        "## Привязки из метаданных",
        "## Обработчик формы",
    ]
    assert [ответ.index(заголовок) for заголовок in заголовки] == sorted(
        ответ.index(заголовок) for заголовок in заголовки
    )
    assert ответ.count("### `ОбщийМодуль.Зовущий`") == 1
    assert "ОбщийМодуль.Зовущий::Первый" in ответ
    assert "ОбщийМодуль.Зовущий::Второй" in ответ
    assert "строка 2" in ответ and "строка 5" in ответ


def test_get_callers_различает_подтверждённые_локальные_и_неразрешённые(
    корень_кода, реестр_из_кода
):
    _модуль(
        корень_кода,
        "Цель",
        "Процедура Обработать() Экспорт\nКонецПроцедуры\n"
        "Процедура ЛокальныйВладелец()\n"
        "    Обработать();\n"
        "КонецПроцедуры\n",
    )
    _модуль(
        корень_кода,
        "ЧужаяЦель",
        "Процедура Обработать() Экспорт\nКонецПроцедуры\n",
    )
    _модуль(
        корень_кода,
        "Неясный",
        "Процедура Зовущая()\n"
        "    Объект.Обработать();\n"
        "КонецПроцедуры\n",
    )
    реестр = реестр_из_кода(корень_кода)

    ответ = tools.get_callers(реестр, TARGET, config="Пример")

    assert "ОбщийМодуль.Цель::ЛокальныйВладелец" in ответ
    assert "одноимён" in ответ.lower()
    assert "ОбщийМодуль.Неясный" in ответ
    assert "цель не удалось разрешить" in ответ.lower()
    assert "подтверждённых мест: 1" in ответ.lower()


def test_get_callers_предел_общий_для_мест_и_остаток_точен(
    корень_кода, реестр_из_кода
):
    _цель(корень_кода)
    for номер in range(4):
        _модуль(
            корень_кода,
            f"Зовущий{номер}",
            "Процедура Вызвать()\n"
            "    Цель.Обработать();\n"
            "    Цель.Обработать();\n"
            "КонецПроцедуры\n",
        )
    реестр = реестр_из_кода(корень_кода)

    ответ = tools.get_callers(реестр, TARGET, config="Пример", limit=3)

    assert ответ.count("строка 2") + ответ.count("строка 3") == 3
    assert "ещё 5 в 3 модулях" in ответ
    assert ответ.index("ОбщийМодуль.Зовущий0") < ответ.index(
        "ОбщийМодуль.Зовущий1"
    )


@pytest.mark.parametrize("limit", [True, False, 0, 51, 1.5, "2"])
def test_get_callers_limit_строгий(limit, реестр_с_кодом):
    with pytest.raises(RegistryError, match="limit"):
        tools.get_callers(
            реестр_с_кодом,
            "ОбщийМодуль.ОбщийПример::Сложить",
            config="Пример",
            limit=limit,
        )


def test_get_callers_привязки_подписок_и_заданий_берёт_из_графа(
    корень_кода, реестр_из_кода, monkeypatch
):
    _цель(корень_кода)
    реестр = реестр_из_кода(
        корень_кода, configuration=_конфигурация_с_привязками()
    )
    monkeypatch.setattr(
        tools,
        "прочитать_модуль",
        lambda _path: pytest.fail("get_callers не должен читать текст модулей"),
    )

    ответ = tools.get_callers(реестр, TARGET, config="Пример")

    assert "ПодпискаНаСобытие.ПослеЗаписи" in ответ
    assert "подписка на событие" in ответ.lower()
    assert "РегламентноеЗадание.НочнойОбмен" in ответ
    assert "регламентное задание" in ответ.lower()
    assert TARGET in ответ


def test_get_callers_привязка_основной_конфигурации_не_доказывает_вызов_расширения(
    корень_кода, реестр_из_кода, архив_кода
):
    _цель(корень_кода)
    реестр = реестр_из_кода(
        корень_кода, configuration=_конфигурация_с_привязками()
    )
    реестр.add_modules(
        архив_кода(корень_кода, extension="Доп"), configuration="Пример"
    )

    ответ = tools.get_callers(
        реестр, TARGET, config="Пример", extension="Доп"
    )

    строка_привязки = ответ.index("ПодпискаНаСобытие.ПослеЗаписи")
    assert ответ.index("не доказывает", ответ.index("## Привязки")) < строка_привязки
    assert "тело выбранного расширения выполняется" in ответ


def test_get_callers_форма_называет_форму_элемент_и_событие(
    корень_кода, реестр_из_кода
):
    ext = (
        корень_кода
        / "Catalogs"
        / "Обработчики"
        / "Forms"
        / "Основная"
        / "Ext"
    )
    (ext / "Form").mkdir(parents=True)
    (ext / "Form" / "Module.bsl").write_text(
        "Процедура Обработать()\nКонецПроцедуры\n", encoding="utf-8"
    )
    (ext / "Form.xml").write_text(
        "<Form>"
        "<Events><Event name=\"OnOpen\">Обработать</Event></Events>"
        "<ChildItems><Button name=\"Кнопка\"><Events>"
        "<Event name=\"OnClick\">Обработать</Event>"
        "</Events></Button><InputField name=\"Поле\"><Events>"
        "<Event name=\"OnChange\">Обработать</Event>"
        "</Events></InputField></ChildItems>"
        "</Form>",
        encoding="utf-8",
    )
    реестр = реестр_из_кода(корень_кода)

    ответ = tools.get_callers(
        реестр,
        "Справочник.Обработчики.Форма.Основная::Обработать",
        config="Пример",
    )

    assert "форма" in ответ.lower() and "OnOpen" in ответ
    assert "Кнопка" in ответ and "OnClick" in ответ
    assert "Поле" in ответ and "OnChange" in ответ


def test_get_callers_пустой_ответ_объясняет_динамические_вызовы(
    корень_кода, реестр_из_кода
):
    _цель(корень_кода)
    реестр = реестр_из_кода(корень_кода)

    ответ = tools.get_callers(реестр, TARGET, config="Пример")

    assert "мест вызова в коде нет" in ответ.lower()
    assert "привязок в метаданных нет" in ответ.lower()
    assert "не является модулем формы" in ответ.lower()
    for имя in (
        "Выполнить",
        "ОписаниеОповещения",
        "ПодключитьОбработчикОжидания",
    ):
        assert имя in ответ
    assert "не значит" in ответ


def test_get_callers_отличает_форму_без_структуры_от_обычного_модуля(
    корень_кода, реестр_из_кода
):
    форма = (
        корень_кода
        / "Catalogs"
        / "БезСтруктуры"
        / "Forms"
        / "Основная"
        / "Ext"
        / "Form"
    )
    форма.mkdir(parents=True)
    (форма / "Module.bsl").write_text(
        "Процедура Обработать()\nКонецПроцедуры\n", encoding="utf-8"
    )
    реестр = реестр_из_кода(корень_кода)

    ответ = tools.get_callers(
        реестр,
        "Справочник.БезСтруктуры.Форма.Основная::Обработать",
        config="Пример",
    )

    assert "структур" in ответ.lower()
    assert "форм" in ответ.lower()
    assert "это не значит" not in ответ.lower()


@pytest.mark.parametrize("state", ["ready", "empty", "missing", "broken"])
def test_get_callers_общая_форма_различает_состояние_структуры(
    корень_кода, реестр_из_кода, state
):
    ext = корень_кода / "CommonForms" / "Общая" / "Ext"
    (ext / "Form").mkdir(parents=True)
    (ext / "Form" / "Module.bsl").write_text(
        "Процедура Обработать()\nКонецПроцедуры\n", encoding="utf-8"
    )
    if state == "ready":
        (ext / "Form.xml").write_text(
            "<Form><Events><Event name=\"OnOpen\">Обработать</Event>"
            "</Events></Form>",
            encoding="utf-8",
        )
    elif state == "empty":
        (ext / "Form.xml").write_text("<Form/>", encoding="utf-8")
    elif state == "broken":
        (ext / "Form.xml").write_text("<Form><Events>", encoding="utf-8")
    реестр = реестр_из_кода(корень_кода)

    ответ = tools.get_callers(
        реестр, "ОбщаяФорма.Общая::Обработать", config="Пример"
    )

    if state == "ready":
        assert "OnOpen" in ответ and "форма" in ответ.lower()
    elif state == "empty":
        assert "не назначена обработчиком" in ответ.lower()
        assert "это не значит" in ответ.lower()
    elif state == "missing":
        assert "нет структуры" in ответ.lower()
        assert "это не значит" not in ответ.lower()
    else:
        assert "повреждена" in ответ.lower()
        assert "это не значит" not in ответ.lower()


@pytest.mark.parametrize("evidence", ["descriptor", "container"])
def test_get_callers_частичная_структура_не_доказывает_отсутствие_handler(
    корень_кода, реестр_из_кода, evidence
):
    ext = корень_кода / "CommonForms" / "Общая" / "Ext" / "Form"
    ext.mkdir(parents=True)
    body = "Процедура Обработать()\nКонецПроцедуры\n"
    (ext / "Module.bsl").write_text(body, encoding="utf-8")
    if evidence == "descriptor":
        (корень_кода / "CommonForms" / "Общая.xml").write_text(
            "<MetaDataObject><CommonForm><Properties>"
            "<Name>Общая</Name></Properties></CommonForm></MetaDataObject>",
            encoding="utf-8",
        )
    else:
        (корень_кода / "CommonForm.Общая.Form").write_bytes(
            v8_container_bytes(
                [("module", body.encode()), ("form", b"{19}")]
            )
        )
    реестр = реестр_из_кода(корень_кода)

    ответ = tools.get_callers(
        реестр, "ОбщаяФорма.Общая::Обработать", config="Пример"
    )

    assert "семантика привязок событий не доказана" in ответ.lower()
    assert "не назначена обработчиком" not in ответ.lower()


def test_get_callers_битый_контейнер_не_называется_битым_form_xml(
    корень_кода, реестр_из_кода
):
    (корень_кода / "CommonForm.Общая.Form").write_bytes(b"broken")
    (корень_кода / "CommonForm.Общая.Form.Module.txt").write_text(
        "Процедура Обработать()\nКонецПроцедуры\n", encoding="utf-8"
    )
    реестр = реестр_из_кода(корень_кода)

    ответ = tools.get_callers(
        реестр, "ОбщаяФорма.Общая::Обработать", config="Пример"
    )

    assert "доказательство структуры формы повреждено" in ответ.lower()
    assert "form.xml повреждена" not in ответ.lower()


def test_get_callers_валидный_form_xml_не_теряется_из_за_битого_контейнера(
    корень_кода, реестр_из_кода
):
    ext = корень_кода / "CommonForms" / "Общая" / "Ext"
    (ext / "Form").mkdir(parents=True)
    (ext / "Form" / "Module.bsl").write_text(
        "Процедура Обработать()\nКонецПроцедуры\n", encoding="utf-8"
    )
    (ext / "Form.xml").write_text(
        '<Form><Events><Event name="OnOpen">Обработать</Event></Events></Form>',
        encoding="utf-8",
    )
    (ext / "Form.bin").write_bytes(b"broken")
    реестр = реестр_из_кода(корень_кода)

    ответ = tools.get_callers(
        реестр, "ОбщаяФорма.Общая::Обработать", config="Пример"
    )

    assert "OnOpen" in ответ
    assert "повреждена" not in ответ.lower()


def test_get_callers_частичная_граница_не_выдаётся_за_точного_владельца(
    корень_кода, реестр_из_кода
):
    _цель(корень_кода)
    _модуль(
        корень_кода,
        "Оборванный",
        "Процедура Незакрытая()\n    Цель.Обработать();\n",
    )
    реестр = реестр_из_кода(корень_кода)

    ответ = tools.get_callers(реестр, TARGET, config="Пример")

    assert "частич" in ответ.lower()
    assert "граница" in ответ.lower()
    assert "ОбщийМодуль.Оборванный::Незакрытая" not in ответ


def test_get_callers_общая_физическая_строка_не_угадывает_владельца(
    корень_кода, реестр_из_кода
):
    _цель(корень_кода)
    _модуль(
        корень_кода,
        "ОднаСтрока",
        "Процедура Первая() Цель.Обработать(); КонецПроцедуры "
        "Процедура Вторая() Цель.Обработать(); КонецПроцедуры\n",
    )
    реестр = реестр_из_кода(корень_кода)

    ответ = tools.get_callers(реестр, TARGET, config="Пример")

    assert "владелец не разрешён по границам строки" in ответ.lower()
    assert "ОднаСтрока::Первая" not in ответ
    assert "ОднаСтрока::Вторая" not in ответ


def test_get_callers_вызовы_на_начальной_и_конечной_строке_имеют_владельца(
    корень_кода, реестр_из_кода
):
    _цель(корень_кода)
    _модуль(
        корень_кода,
        "Границы",
        "Процедура Владелец() Цель.Обработать();\n"
        "    Цель.Обработать(); КонецПроцедуры\n"
        "Процедура Соседняя()\nКонецПроцедуры\n",
    )
    реестр = реестр_из_кода(корень_кода)

    ответ = tools.get_callers(реестр, TARGET, config="Пример")

    assert ответ.count("ОбщийМодуль.Границы::Владелец") == 2
    assert "строка 1" in ответ and "строка 2" in ответ
    assert "Границы::Соседняя" not in ответ


def test_get_callers_неразрешённые_места_тоже_ограничены(
    корень_кода, реестр_из_кода
):
    _цель(корень_кода)
    _модуль(
        корень_кода,
        "Подтверждённый",
        "Процедура Зовущая()\n"
        "    Цель.Обработать();\n"
        "КонецПроцедуры\n",
    )
    for номер in range(5):
        _модуль(
            корень_кода,
            f"Неясный{номер}",
            "Процедура Зовущая()\n"
            "    Объект.Обработать();\n"
            "КонецПроцедуры\n",
        )
    реестр = реестр_из_кода(корень_кода)

    ответ = tools.get_callers(реестр, TARGET, config="Пример", limit=2)

    assert "ОбщийМодуль.Подтверждённый::Зовущая" in ответ
    assert sum(f"Неясный{номер}`: строка" in ответ for номер in range(5)) == 1
    assert "ещё 4 одноимённых" in ответ


def test_get_callers_границы_модуля_готовятся_один_раз_для_всех_мест(
    корень_кода, реестр_из_кода, monkeypatch
):
    _цель(корень_кода)
    _модуль(
        корень_кода,
        "МногоМест",
        "Процедура Владелец()\n"
        + "    Цель.Обработать();\n" * 12
        + "КонецПроцедуры\n",
    )
    реестр = реестр_из_кода(корень_кода)
    настоящее = modules_index.Оглавление.модуля
    обращений = 0

    def считать(self, адрес):
        nonlocal обращений
        if адрес == "ОбщийМодуль.МногоМест":
            обращений += 1
        return настоящее(self, адрес)

    monkeypatch.setattr(modules_index.Оглавление, "модуля", считать)

    ответ = tools.get_callers(реестр, TARGET, config="Пример", limit=20)

    assert ответ.count("ОбщийМодуль.МногоМест::Владелец") == 12
    assert обращений == 1


def test_поиск_владельца_не_сканирует_десять_тысяч_перекрытий():
    проверок = 0

    class Запись:
        строка = 1
        частичный = False

        def __init__(self, позиция):
            self.позиция = позиция
            self.имя = f"П{позиция}"

        @property
        def конец(self):
            nonlocal проверок
            проверок += 1
            return 20_000

    границы = tools._build_owner_boundaries(
        [Запись(номер) for номер in range(10_000)]
    )
    проверок = 0

    site = tools._caller_site(
        {"ОбщийМодуль.Много": границы},
        modules_index.Место("ОбщийМодуль.Много", 10_000, "ОбщийМодуль.Цель"),
    )

    assert site.ambiguous_owner
    assert проверок <= 2


def test_get_callers_промах_предлагает_точный_похожий_адрес(реестр_с_кодом):
    ответ = tools.get_callers(
        реестр_с_кодом,
        "ОбщийМодуль.ОбщийПример::Слжить",
        config="Пример",
    )

    assert "возможно, имелось в виду" in ответ.lower()
    assert "ОбщийМодуль.ОбщийПример::Сложить" in ответ


@pytest.mark.parametrize(
    "address",
    ["", "ОбщийМодуль.ОбщийПример", "::Сложить", "Модуль::", "А::Б::В"],
)
def test_get_callers_требует_адрес_процедуры(address, реестр_с_кодом):
    with pytest.raises(RegistryError, match="address"):
        tools.get_callers(реестр_с_кодом, address, config="Пример")


@pytest.mark.parametrize("state", ["missing", "building", "error"])
def test_get_callers_честно_показывает_состояние_кода(
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

    ответ = tools.get_callers(
        реестр,
        "ОбщийМодуль.ОбщийПример::Сложить",
        config="Пример",
    )

    if state == "missing":
        assert "выгрузка в файлы не загружена" in ответ
    elif state == "building":
        assert "этап 2/4" in ответ and "1 из 3" in ответ
    else:
        assert "отказ сборки" in ответ


def test_get_callers_предупреждает_о_частичном_разборе_и_версии(
    корень_кода, реестр_из_кода
):
    _модуль(
        корень_кода,
        "Цель",
        "Процедура Обработать() Экспорт\n    Маркер = 1;\n",
    )
    реестр = реестр_из_кода(
        корень_кода,
        configuration=build_configuration(name="Пример", version="2.0"),
        code_version="1.0",
    )

    ответ = tools.get_callers(реестр, TARGET, config="Пример")

    первый_раздел = ответ.index("## Места вызова в коде")
    assert ответ.index("разобран не до конца") < первый_раздел
    assert ответ.index("версии 1.0") < первый_раздел
    assert "версии 2.0" in ответ


def test_get_callers_выбранное_расширение_не_смешивает_код_конфигурации(
    корень_кода, реестр_из_кода, архив_кода
):
    _цель(корень_кода)
    _модуль(
        корень_кода,
        "БазовыйЗовущий",
        "Процедура Вызвать()\n    Цель.Обработать();\nКонецПроцедуры\n",
    )
    реестр = реестр_из_кода(корень_кода)
    базовый = tools.get_callers(реестр, TARGET, config="Пример")

    for файл in корень_кода.rglob("Module.bsl"):
        файл.unlink()
    _цель(корень_кода)
    _модуль(
        корень_кода,
        "ЗовущийРасширения",
        "Процедура Вызвать()\n    Цель.Обработать();\nКонецПроцедуры\n",
    )
    реестр.add_modules(
        архив_кода(корень_кода, extension="Доп"), configuration="Пример"
    )

    расширение = tools.get_callers(
        реестр, TARGET, config="Пример", extension="Доп"
    )

    assert "БазовыйЗовущий" in базовый and "ЗовущийРасширения" not in базовый
    assert "ЗовущийРасширения" in расширение and "БазовыйЗовущий" not in расширение


@pytest.mark.parametrize("extension", [None, "Доп"], ids=["modules", "extension"])
@pytest.mark.parametrize("action", ["reparse", "remove"])
def test_get_callers_смена_поколения_не_смешивает_индексы(
    корень_кода,
    реестр_из_кода,
    архив_кода,
    monkeypatch,
    extension,
    action,
):
    _цель(корень_кода)
    _модуль(
        корень_кода,
        "СтарыйЗовущий",
        "Процедура Вызвать()\n    Цель.Обработать();\nКонецПроцедуры\n",
    )
    реестр = реестр_из_кода(корень_кода, extension=extension)
    for файл in корень_кода.rglob("Module.bsl"):
        файл.unlink()
    _цель(корень_кода)
    _модуль(
        корень_кода,
        "НовыйЗовущий",
        "Процедура Вызвать()\n    Цель.Обработать();\nКонецПроцедуры\n",
    )
    новый = архив_кода(корень_кода, extension=extension)

    начато = threading.Event()
    отпустить = threading.Event()
    настоящее = modules_index.Вызовы.выбрать
    первый = True

    def задержать(self, имя, цель, *, limit):
        nonlocal первый
        результат = настоящее(self, имя, цель, limit=limit)
        if первый:
            первый = False
            начато.set()
            отпустить.wait(timeout=3)
        return результат

    monkeypatch.setattr(modules_index.Вызовы, "выбрать", задержать)
    ответы: list[str] = []
    ошибки: list[BaseException] = []

    def читать():
        try:
            ответы.append(
                tools.get_callers(
                    реестр, TARGET, config="Пример", extension=extension
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
        assert "НовыйЗовущий" in ответы[0]
        assert "СтарыйЗовущий" not in ответы[0]
    else:
        assert not ответы
        assert len(ошибки) == 1
        assert isinstance(ошибки[0], RegistryError)
        assert "не загружено" in str(ошибки[0]).lower()
