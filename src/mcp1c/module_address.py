"""Перевод между путём файла в выгрузке и адресом на языке метаданных.

Путь на диске (`Documents/ЧекККМ/Ext/ObjectModule.bsl`) агенту не нужен и в
ответах не появляется: он видит `Документ.ЧекККМ.МодульОбъекта`. Этот модуль —
единственное место, где эти два мира встречаются (design doc, раздел 4,
подраздел «Адрес»).

Разделитель процедуры — `::`, а не точка. В адресе модуля точка уже
встречается трижды (`Справочник.Номенклатура.Форма.ФормаЭлемента`), и
`rpartition(".")` не отличит модуль от процедуры: агент, склеивший адрес из
частей нашей же выдачи, получил бы «не найдено» по существующему имени.

Неизвестные каталоги выгрузки — таблица `_ВИДЫ` изначально не покрывала
10,2% живого корпуса (804 файла из 7 878 на «Рознице для Казахстана»): не
только редкие каталоги, но и обычные `HTTPServices`, `WebServices`,
`Constants` — так что «неизвестный» не значит «редкий». На первых двух
живых выгрузках («Розница для Казахстана» и обезличенной отраслевой)
таблица закрыла 100% `.bsl`; третья («Автосалон6») дала ещё
`ChartsOfCalculationTypes` и `IntegrationServices`. Решение то же: честный
отказ (`ValueError` с именем каталога), а не подстановка похожего вида.
Собранный из чужого вида объекта метаданных адрес для агента неотличим от
верного и приведёт к «не найдено» без объяснения причины — то есть ровно к
тому, чего разделитель `::` уже избегает для процедур. Тот же приём и для
имени файла модуля, и для обратного разбора адреса: подставлять «наверное,
это X» здесь не входит в задачу — это работа `search_procedures` («не
найдено — возможно, имелось в виду», design doc, раздел 4). Расширять
таблицу без единого примера пути для проверки — то самое молчаливое
искажение: `Sequences` добавлен после отраслевой выгрузки,
`ChartsOfCalculationTypes` и `IntegrationServices` — после «Автосалон6»,
`ExternalDataSources` — после выгрузки с таблицами внешнего источника,
`AccountingRegisters` и `CalculationRegisters` — после образца тех каталогов.

Модули уровня конфигурации — особый случай: у них нет ни вида объекта, ни
имени, только каталог `Ext/` прямо в корне выгрузки, потому что
конфигурация в базе одна. Список файлов (`_МОДУЛИ_КОНФИГУРАЦИИ`) короткий и
закрытый платформой — в отличие от `_ВИДЫ`/`_МОДУЛИ`, которые расширяются
по мере находок, этот список меняться не должен.

Суффикс модуля в адресе (`.МодульОбъекта`, `.МодульМенеджера`) нужен только
там, где у вида в выгрузке встречается больше одного типа файла модуля и
без суффикса адрес неоднозначен — у документа их два, суффикс несёт смысл.
Там, где тип файла ровно один (`_ОДИН_МОДУЛЬ`), суффикс не различает ничего,
а только удлиняет адрес, который агент составляет руками, — поэтому его нет.
Это проверено по обеим живым выгрузкам, а не по памяти: `Constants`,
например, выглядит одномодульным видом, но на диске у него встречаются и
`ManagerModule.bsl`, и `ValueManagerModule.bsl` — суффикс там остаётся.
"""

from __future__ import annotations

from dataclasses import dataclass

# Каталог выгрузки -> вид объекта метаданных на русском.
_ВИДЫ = {
    "CommonModules": "ОбщийМодуль", "Documents": "Документ",
    "Catalogs": "Справочник", "AccumulationRegisters": "РегистрНакопления",
    "InformationRegisters": "РегистрСведений", "DataProcessors": "Обработка",
    "Reports": "Отчет", "Enums": "Перечисление", "ChartsOfAccounts": "ПланСчетов",
    "BusinessProcesses": "БизнесПроцесс", "Tasks": "Задача",
    "ExchangePlans": "ПланОбмена", "CommonForms": "ОбщаяФорма",
    # Добавлено по сверке на живой выгрузке: эти каталоги составляли 10,2%
    # корпуса и не могли оставаться честными отказами.
    "CommonCommands": "ОбщаяКоманда", "Constants": "Константа",
    "ChartsOfCharacteristicTypes": "ПланВидовХарактеристик",
    "SettingsStorages": "ХранилищеНастроек", "WebServices": "WebСервис",
    "HTTPServices": "HTTPСервис", "DocumentJournals": "ЖурналДокументов",
    # Пример пути найден на обезличенной отраслевой выгрузке;
    # набор записей устроен ровно как у регистра — суффикс за ним сохранён.
    "Sequences": "Последовательность",
    "FilterCriteria": "КритерийОтбора",
    # «Автосалон6»: план видов расчёта (объект, менеджер и формы) и сервис
    # интеграции (`Ext/Module.bsl`, как у HTTPServices/WebServices).
    "ChartsOfCalculationTypes": "ПланВидовРасчета",
    "IntegrationServices": "СервисИнтеграции",
    # Таблица внешнего источника: `ExternalDataSources/<Источник>/Tables/<Таблица>/…`,
    # не `<Каталог>/<Имя>/…`. Вид один, раскладка на уровень глубже.
    "ExternalDataSources": "ВнешнийИсточникДанных",
    # Как у регистра накопления: набор записей, менеджер, формы и команды.
    # Перерасчёты в образце `CalculationRegisters` не встречались — вложенный
    # каталог `Recalculations/` в адрес не входит.
    "AccountingRegisters": "РегистрБухгалтерии",
    "CalculationRegisters": "РегистрРасчета",
}
_ВИДЫ_ОБРАТНО = {вид: каталог for каталог, вид in _ВИДЫ.items()}

# Имя файла -> суффикс адреса.
_МОДУЛИ = {
    "Module.bsl": "", "ObjectModule.bsl": "МодульОбъекта",
    "ManagerModule.bsl": "МодульМенеджера", "RecordSetModule.bsl": "МодульНабораЗаписей",
    "CommandModule.bsl": "МодульКоманды", "ValueManagerModule.bsl": "МодульМенеджераЗначения",
}
_МОДУЛИ_ОБРАТНО = {суффикс: файл for файл, суффикс in _МОДУЛИ.items()}

# Виды с ровно одним типом файла модуля на обеих живых выгрузках (проверено
# по диску, не по памяти — ChartsOfCharacteristicTypes и Constants выглядели
# кандидатами, но на деле имеют по два типа и суффикс сохраняют). Суффикс
# для этих видов не несёт различающего смысла, поэтому в адресе его нет;
# WebServices и HTTPServices сюда не входят — их единственный файл это
# Module.bsl с уже пустым суффиксом в _МОДУЛИ, особый случай не нужен.
_ОДИН_МОДУЛЬ = {
    "CommonCommands": "CommandModule.bsl",
    "SettingsStorages": "ManagerModule.bsl",
}
_ОДИН_МОДУЛЬ_ПО_ВИДУ = {_ВИДЫ[каталог]: файл for каталог, файл in _ОДИН_МОДУЛЬ.items()}

# Файл в Ext/ корня выгрузки -> суффикс адреса модуля конфигурации. Закрытый
# список платформы (все четыре подтверждены на живой выгрузке), не таблица
# соответствия видов объектов — конфигурация не вид, а единственный синглтон.
_МОДУЛИ_КОНФИГУРАЦИИ = {
    "ManagedApplicationModule.bsl": "МодульУправляемогоПриложения",
    "OrdinaryApplicationModule.bsl": "МодульОбычногоПриложения",
    "SessionModule.bsl": "МодульСеанса",
    "ExternalConnectionModule.bsl": "МодульВнешнегоСоединения",
}
_МОДУЛИ_КОНФИГУРАЦИИ_ОБРАТНО = {
    суффикс: файл for файл, суффикс in _МОДУЛИ_КОНФИГУРАЦИИ.items()
}


# Имя вида в плоской выгрузке -> публичный вид метаданных. Таблица отдельна
# от иерархических каталогов: формы множественного числа там являются частью
# физической раскладки, а здесь единственное число — часть грамматики имени.
_ПЛОСКИЕ_ВИДЫ = {
    "AccumulationRegister": "РегистрНакопления",
    "Catalog": "Справочник",
    "ChartOfCharacteristicTypes": "ПланВидовХарактеристик",
    "CommonCommand": "ОбщаяКоманда",
    "CommonForm": "ОбщаяФорма",
    "CommonModule": "ОбщийМодуль",
    "Configuration": "Конфигурация",
    "Constant": "Константа",
    "DataProcessor": "Обработка",
    "Document": "Документ",
    "DocumentJournal": "ЖурналДокументов",
    "Enum": "Перечисление",
    "ExchangePlan": "ПланОбмена",
    "FilterCriterion": "КритерийОтбора",
    "HTTPService": "HTTPСервис",
    "InformationRegister": "РегистрСведений",
    "Report": "Отчет",
    "WebService": "WebСервис",
}

_ПЛОСКИЕ_СУФФИКСЫ = {
    "Module": "",
    "ObjectModule": "МодульОбъекта",
    "ManagerModule": "МодульМенеджера",
    "RecordSetModule": "МодульНабораЗаписей",
    "ValueManagerModule": "МодульМенеджераЗначения",
}

# 18 видов и 13 синтаксических pattern дают не декартов продукт, а ровно 49
# подтверждённых сочетаний. Например, `CommonForm.X.Module.txt` синтаксически
# похож на общий модуль, но в измеренном формате форма имеет только `.Form`
# и `.Form.Module.txt`; принять похожее имя значило бы создать ложный адрес.
_ПЛОСКИЕ_PATTERN_BY_KIND = {
    "AccumulationRegister": {"manager_module", "object_form_container", "recordset_module"},
    "Catalog": {"manager_module", "object_command", "object_form_container", "object_form_text", "object_module"},
    "ChartOfCharacteristicTypes": {"object_form_container", "object_form_text", "object_module"},
    "CommonCommand": {"common_command"},
    "CommonForm": {"common_form_container", "common_form_text"},
    "CommonModule": {"compiled", "module"},
    "Configuration": {"configuration"},
    "Constant": {"value_manager_module"},
    "DataProcessor": {"manager_module", "object_command", "object_form_container", "object_form_text", "object_module"},
    "Document": {"manager_module", "object_form_container", "object_form_text", "object_module"},
    "DocumentJournal": {"object_form_container", "object_form_text"},
    "Enum": {"manager_module", "object_form_container"},
    "ExchangePlan": {"manager_module", "object_command", "object_form_container", "object_form_text", "object_module"},
    "FilterCriterion": {"object_form_container"},
    "HTTPService": {"module"},
    "InformationRegister": {"manager_module", "object_command", "object_form_container", "object_form_text", "recordset_module"},
    "Report": {"manager_module", "object_command", "object_form_container", "object_form_text", "object_module"},
    "WebService": {"module"},
}


class FlatNameError(ValueError):
    """Имя плоской выгрузки не входит в доказанную грамматику."""

    def __init__(self, category: str, reason: str):
        self.category = category
        self.reason = reason
        super().__init__(reason)


@dataclass(frozen=True, slots=True)
class FlatAddress:
    """Разобранное имя с достаточными полями для обратной сборки."""

    flat_kind: str
    public_kind: str
    name: str
    pattern: str
    nested_name: str = ""

    @property
    def address(self) -> str:
        if self.pattern == "configuration":
            return "Конфигурация.МодульУправляемогоПриложения"
        if self.pattern in ("object_form_container", "object_form_text"):
            return f"{self.public_kind}.{self.name}.Форма.{self.nested_name}"
        if self.pattern in ("common_form_container", "common_form_text"):
            return f"ОбщаяФорма.{self.name}"
        if self.pattern == "object_command":
            return f"{self.public_kind}.{self.name}.Команда.{self.nested_name}"
        if self.pattern == "common_command":
            return f"ОбщаяКоманда.{self.name}"
        suffix_by_pattern = {
            "module": "",
            "compiled": "",
            "object_module": "МодульОбъекта",
            "manager_module": "МодульМенеджера",
            "recordset_module": "МодульНабораЗаписей",
            "value_manager_module": "МодульМенеджераЗначения",
        }
        suffix = suffix_by_pattern[self.pattern]
        base = f"{self.public_kind}.{self.name}"
        return f"{base}.{suffix}" if suffix else base

    @property
    def compiled(self) -> bool:
        return self.pattern == "compiled"

    @property
    def is_form(self) -> bool:
        return self.pattern in {
            "object_form_container",
            "object_form_text",
            "common_form_container",
            "common_form_text",
        }

    @property
    def representation(self) -> str:
        if self.pattern in ("object_form_container", "common_form_container"):
            return "container"
        if self.pattern == "compiled":
            return "compiled"
        return "text"

    def filename(self) -> str:
        if self.pattern == "configuration":
            return "Configuration.ManagedApplicationModule.txt"
        if self.pattern == "compiled":
            return f"CommonModule.{self.name}.Module"
        if self.pattern == "object_form_container":
            return f"{self.flat_kind}.{self.name}.Form.{self.nested_name}.Form"
        if self.pattern == "object_form_text":
            return f"{self.flat_kind}.{self.name}.Form.{self.nested_name}.Form.Module.txt"
        if self.pattern == "common_form_container":
            return f"CommonForm.{self.name}.Form"
        if self.pattern == "common_form_text":
            return f"CommonForm.{self.name}.Form.Module.txt"
        if self.pattern == "common_command":
            return f"CommonCommand.{self.name}.CommandModule.txt"
        if self.pattern == "object_command":
            return f"{self.flat_kind}.{self.name}.Command.{self.nested_name}.CommandModule.txt"
        suffix_by_pattern = {
            "module": "Module",
            "object_module": "ObjectModule",
            "manager_module": "ManagerModule",
            "recordset_module": "RecordSetModule",
            "value_manager_module": "ValueManagerModule",
        }
        return f"{self.flat_kind}.{self.name}.{suffix_by_pattern[self.pattern]}.txt"


def ключ_адреса(address: str) -> str:
    """Детерминированный ключ для обнаружения регистровых коллизий."""
    return address.casefold()


def _flat_error(category: str, reason: str) -> FlatNameError:
    # Физическое имя намеренно не включается: неадресуемый кандидат наружу
    # представляется категорией и порядковым номером внутри источника.
    return FlatNameError(category, reason)


def _flat_address(
    kind: str,
    public_kind: str,
    name: str,
    pattern: str,
    nested_name: str = "",
) -> FlatAddress:
    if pattern not in _ПЛОСКИЕ_PATTERN_BY_KIND[kind]:
        raise _flat_error(
            "unsupported_flat_name", "неподдержанное сочетание вида и формы имени"
        )
    return FlatAddress(kind, public_kind, name, pattern, nested_name)


def разобрать_плоское_имя(filename: str) -> FlatAddress:
    """Разобрать только одну из доказанных форм плоского имени.

    Похожие хвосты, лишние компоненты и неизвестный вид не угадываются.
    Регистр всех служебных компонентов является частью формата.
    """
    if not filename or "/" in filename or "\\" in filename or "\x00" in filename:
        raise _flat_error("unsupported_flat_name", "неподдержанная форма плоского имени")
    parts = filename.split(".")
    kind = parts[0]
    public_kind = _ПЛОСКИЕ_ВИДЫ.get(kind)
    if public_kind is None:
        raise _flat_error("unsupported_flat_kind", "неподдержанный вид плоской выгрузки")
    if any(not part for part in parts):
        raise _flat_error("unsupported_flat_name", "неподдержанная форма плоского имени")

    if parts == ["Configuration", "ManagedApplicationModule", "txt"]:
        return _flat_address(kind, public_kind, "", "configuration")
    if kind == "Configuration":
        raise _flat_error("unsupported_flat_name", "неподдержанная форма плоского имени")

    if len(parts) == 3 and parts[2] == "Module" and kind == "CommonModule":
        return _flat_address(kind, public_kind, parts[1], "compiled")

    if len(parts) == 5 and parts[2:] == ["Form", "Module", "txt"]:
        if kind != "CommonForm":
            raise _flat_error("unsupported_flat_name", "неподдержанная форма плоского имени")
        return _flat_address(kind, public_kind, parts[1], "common_form_text")
    if len(parts) == 3 and parts[2] == "Form":
        if kind != "CommonForm":
            raise _flat_error("unsupported_flat_name", "неподдержанная форма плоского имени")
        return _flat_address(kind, public_kind, parts[1], "common_form_container")

    if len(parts) == 7 and parts[2] == "Form" and parts[4:] == ["Form", "Module", "txt"]:
        if kind in {"CommonForm", "CommonModule", "CommonCommand"}:
            raise _flat_error("unsupported_flat_name", "неподдержанная форма плоского имени")
        return _flat_address(kind, public_kind, parts[1], "object_form_text", parts[3])
    if len(parts) == 5 and parts[2] == "Form" and parts[4] == "Form":
        if kind in {"CommonForm", "CommonModule", "CommonCommand"}:
            raise _flat_error("unsupported_flat_name", "неподдержанная форма плоского имени")
        return _flat_address(
            kind, public_kind, parts[1], "object_form_container", parts[3]
        )

    if len(parts) == 4 and parts[2:] == ["CommandModule", "txt"]:
        if kind != "CommonCommand":
            raise _flat_error("unsupported_flat_name", "неподдержанная форма плоского имени")
        return _flat_address(kind, public_kind, parts[1], "common_command")
    if (
        len(parts) == 6
        and parts[2] == "Command"
        and parts[4:] == ["CommandModule", "txt"]
    ):
        if kind in {"CommonForm", "CommonModule", "CommonCommand"}:
            raise _flat_error("unsupported_flat_name", "неподдержанная форма плоского имени")
        return _flat_address(kind, public_kind, parts[1], "object_command", parts[3])

    if len(parts) == 4 and parts[3] == "txt" and parts[2] in _ПЛОСКИЕ_СУФФИКСЫ:
        pattern_by_suffix = {
            "Module": "module",
            "ObjectModule": "object_module",
            "ManagerModule": "manager_module",
            "RecordSetModule": "recordset_module",
            "ValueManagerModule": "value_manager_module",
        }
        return _flat_address(
            kind, public_kind, parts[1], pattern_by_suffix[parts[2]]
        )

    raise _flat_error("unsupported_flat_name", "неподдержанная форма плоского имени")


def адрес_скомпилированного_модуля(относительный_путь: str) -> str:
    """Адрес двух канонических путей скомпилированного общего модуля.

    Такой файл плоской выгрузки содержит образ без исходного текста. Он не
    участвует в обратном преобразовании ``путь_модуля``: прочитать по этому
    адресу нечего, но сам факт существования общего модуля индекс обязан
    сохранить.
    """
    части = относительный_путь.split("/")
    имя = ""
    if (
        len(части) == 2
        and части[0] == "CommonModules"
        and части[1].endswith(".Module")
        and части[1] != ".Module"
        and части[1].count(".") == 1
    ):
        имя = части[1][: -len(".Module")]
    elif len(части) == 1:
        плоские = части[0].split(".")
        if (
            len(плоские) == 3
            and плоские[0] == "CommonModule"
            and плоские[1]
            and плоские[2] == "Module"
        ):
            имя = плоские[1]
    if not имя:
        raise ValueError(
            "не удалось разобрать путь скомпилированного общего модуля: "
            f"{относительный_путь!r}"
        )
    return f"ОбщийМодуль.{имя}"


def адрес_модуля(относительный_путь: str) -> str:
    """Путь файла в выгрузке -> адрес модуля на языке метаданных.

    Понимает шесть форм пути: `Ext/<Файл>.bsl` (модуль уровня конфигурации,
    без вида и имени), `<Каталог>/<Имя>/Ext/<Файл>.bsl` (общий модуль,
    модуль объекта и т. п.), `<Каталог>/<Имя>/Ext/Form/Module.bsl` (общая
    форма — сама себе объект, промежуточного `Forms/<ИмяФормы>` нет),
    `<Каталог>/<Имя>/Forms/<ИмяФормы>/Ext/Form/Module.bsl` (форма объекта),
    `<Каталог>/<Имя>/Commands/<ИмяКоманды>/Ext/CommandModule.bsl` (команда
    объекта, по образцу формы) и таблицу внешнего источника
    `ExternalDataSources/<Источник>/Tables/<Таблица>/…` (модуль или форма
    таблицы). Любая другая форма пути — как и незнакомый каталог или файл —
    честный отказ, см. докстроку модуля.
    """
    части = относительный_путь.split("/")

    if части[0] == "Ext":
        if len(части) == 2 and части[1] in _МОДУЛИ_КОНФИГУРАЦИИ:
            return f"Конфигурация.{_МОДУЛИ_КОНФИГУРАЦИИ[части[1]]}"
        raise ValueError(
            f"неизвестный модуль уровня конфигурации: {относительный_путь!r} "
            "не найден в таблице соответствия"
        )

    if части[0] not in _ВИДЫ:
        каталог = части[0]
        raise ValueError(
            f"неизвестный вид объекта метаданных: каталог выгрузки {каталог!r} "
            "не найден в таблице соответствия"
        )
    вид = _ВИДЫ[части[0]]

    if части[0] == "ExternalDataSources":
        if (
            len(части) == 9
            and части[2] == "Tables"
            and части[4] == "Forms"
            and части[6:] == ["Ext", "Form", "Module.bsl"]
        ):
            _, источник, _, таблица, _, имя_формы, _, _, _ = части
            return f"{вид}.{источник}.Таблица.{таблица}.Форма.{имя_формы}"
        if len(части) == 6 and части[2] == "Tables" and части[4] == "Ext":
            _, источник, _, таблица, _, файл = части
            if файл not in _МОДУЛИ:
                raise ValueError(
                    f"неизвестное имя файла модуля: {файл!r} не найдено "
                    "в таблице соответствия"
                )
            суффикс = _МОДУЛИ[файл]
            база = f"{вид}.{источник}.Таблица.{таблица}"
            return f"{база}.{суффикс}" if суффикс else база
        raise ValueError(
            f"не удалось разобрать путь модуля: {относительный_путь!r}"
        )

    if len(части) == 7 and части[2] == "Forms" and части[4:] == ["Ext", "Form", "Module.bsl"]:
        _, имя, _, имя_формы, _, _, _ = части
        return f"{вид}.{имя}.Форма.{имя_формы}"

    if len(части) == 6 and части[2] == "Commands" and части[4:] == ["Ext", "CommandModule.bsl"]:
        _, имя, _, имя_команды, _, _ = части
        return f"{вид}.{имя}.Команда.{имя_команды}"

    # ОбщаяФорма — сама себе объект: нет промежуточного "Forms/<ИмяФормы>",
    # но модуль лежит на уровень глубже обычного (Ext/Form/Module.bsl, а не
    # Ext/Module.bsl) — та же вложенность, что и у формы объекта.
    if len(части) == 5 and части[2:] == ["Ext", "Form", "Module.bsl"]:
        _, имя, _, _, _ = части
        return f"{вид}.{имя}"

    if len(части) == 4 and части[2] == "Ext":
        _, имя, _, файл = части

        if части[0] in _ОДИН_МОДУЛЬ:
            # Единственный тип файла модуля для этого вида — суффикс не
            # различает ничего. Другой файл здесь появиться не должен; если
            # появится — честный отказ, а не тихая потеря обратимости.
            ожидаемый_файл = _ОДИН_МОДУЛЬ[части[0]]
            if файл != ожидаемый_файл:
                raise ValueError(
                    f"неожиданный файл модуля {файл!r} для вида {вид!r}: "
                    f"на живых выгрузках у него только {ожидаемый_файл!r}"
                )
            return f"{вид}.{имя}"

        if файл not in _МОДУЛИ:
            raise ValueError(
                f"неизвестное имя файла модуля: {файл!r} не найдено в таблице соответствия"
            )
        суффикс = _МОДУЛИ[файл]
        return f"{вид}.{имя}.{суффикс}" if суффикс else f"{вид}.{имя}"

    raise ValueError(f"не удалось разобрать путь модуля: {относительный_путь!r}")


def путь_модуля(адрес: str) -> str:
    """Адрес модуля на языке метаданных -> путь файла в выгрузке.

    Обратная функция к `адрес_модуля`: `путь_модуля(адрес_модуля(путь)) == путь`
    для всех путей, которые `адрес_модуля` разбирает без ошибки.
    """
    части = адрес.split(".")
    вид = части[0]

    if вид == "Конфигурация":
        if len(части) == 2 and части[1] in _МОДУЛИ_КОНФИГУРАЦИИ_ОБРАТНО:
            return f"Ext/{_МОДУЛИ_КОНФИГУРАЦИИ_ОБРАТНО[части[1]]}"
        raise ValueError(
            f"неизвестный модуль уровня конфигурации в адресе: {адрес!r} "
            "не найден в таблице соответствия"
        )

    if вид not in _ВИДЫ_ОБРАТНО:
        raise ValueError(
            f"неизвестный вид объекта метаданных в адресе: {вид!r} "
            "не найден в таблице соответствия"
        )
    каталог = _ВИДЫ_ОБРАТНО[вид]

    if вид == "ВнешнийИсточникДанных":
        if len(части) == 6 and части[2] == "Таблица" and части[4] == "Форма":
            _, источник, _, таблица, _, имя_формы = части
            return (
                f"{каталог}/{источник}/Tables/{таблица}/Forms/{имя_формы}"
                "/Ext/Form/Module.bsl"
            )
        if len(части) == 5 and части[2] == "Таблица":
            _, источник, _, таблица, суффикс = части
            if суффикс not in _МОДУЛИ_ОБРАТНО:
                raise ValueError(
                    f"неизвестный суффикс модуля в адресе: {суффикс!r} "
                    "не найден в таблице соответствия"
                )
            return (
                f"{каталог}/{источник}/Tables/{таблица}/Ext/"
                f"{_МОДУЛИ_ОБРАТНО[суффикс]}"
            )
        raise ValueError(f"не удалось разобрать адрес модуля: {адрес!r}")

    if len(части) == 4 and части[2] == "Форма":
        _, имя, _, имя_формы = части
        return f"{каталог}/{имя}/Forms/{имя_формы}/Ext/Form/Module.bsl"

    if len(части) == 4 and части[2] == "Команда":
        _, имя, _, имя_команды = части
        return f"{каталог}/{имя}/Commands/{имя_команды}/Ext/CommandModule.bsl"

    if len(части) == 2 and вид in _ОДИН_МОДУЛЬ_ПО_ВИДУ:
        _, имя = части
        return f"{каталог}/{имя}/Ext/{_ОДИН_МОДУЛЬ_ПО_ВИДУ[вид]}"

    if len(части) in (2, 3):
        имя = части[1]
        суффикс = части[2] if len(части) == 3 else ""
        if суффикс not in _МОДУЛИ_ОБРАТНО:
            raise ValueError(
                f"неизвестный суффикс модуля в адресе: {суффикс!r} "
                "не найден в таблице соответствия"
            )
        файл = _МОДУЛИ_ОБРАТНО[суффикс]
        # ОбщаяФорма хранится как форма (Ext/Form/Module.bsl), а не как
        # обычный модуль (Ext/Module.bsl) — тот же файл, другая вложенность.
        if вид == "ОбщаяФорма":
            return f"{каталог}/{имя}/Ext/Form/{файл}"
        return f"{каталог}/{имя}/Ext/{файл}"

    raise ValueError(f"не удалось разобрать адрес модуля: {адрес!r}")


def разобрать_адрес(адрес: str) -> tuple[str, str | None]:
    """Отделяет имя процедуры от адреса модуля по разделителю `::`.

    Адрес без `::` означает модуль целиком — тогда возвращается `None`
    вместо имени процедуры.
    """
    модуль, разделитель, процедура = адрес.partition("::")
    return модуль, процедура if разделитель else None
