"""Рендер объекта конфигурации в markdown с уровнями детализации.

Три уровня — не украшение, а способ не жечь контекст агента:

    brief   ~50 токенов   назначение объекта, счётчики
    fields  основной      реквизиты, табличные части, измерения, значения
    full    всё           + свойства, движения, связи, входящие ссылки

Схлопывание составных типов происходит здесь, а не в выгрузке: у Регистратора
типов бывают сотни, но в модели они сохранены полностью — граф строится по ним.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass

from .graph import EDGE_TITLES, Graph
from .model import Configuration, Field, MetadataObject
from .structure_origin import StructureOriginView
from .syntax_model import KIND_TITLES, SyntaxItem

BRIEF, FIELDS, FULL = "brief", "fields", "full"
DETAIL_LEVELS = (BRIEF, FIELDS, FULL)


@dataclass(frozen=True, slots=True)
class ProcedureMatch:
    """Одна строка поиска по коду, уже дочитанная с диска для ответа."""

    address: str
    signature: str
    exported: bool
    function: bool
    line: int
    calls: int
    unresolved_calls: int
    annotated: bool


@dataclass(frozen=True, slots=True)
class ProcedureOutline:
    """Строка оглавления модуля, дочитанная с диска."""

    address: str
    signature: str
    exported: bool
    function: bool
    line: int
    calls: int
    directive: str = ""
    events: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class CallerSite:
    """Одно место вызова с владельцем, выведенным из оглавления."""

    module: str
    line: int
    owner: str | None
    partial_owner: bool = False
    ambiguous_owner: bool = False


@dataclass(frozen=True, slots=True)
class MetadataBinding:
    """Привязка процедуры подпиской или регламентным заданием."""

    kind: str
    source: str


@dataclass(frozen=True, slots=True)
class FormHandlerBinding:
    """Событие формы и элемент; ``element=None`` означает саму форму."""

    element: str | None
    event: str


def _code_warnings(warnings: list[str]) -> list[str]:
    """Предупреждения стоят над кодом, достоверность которого меняют."""
    if not warnings:
        return []
    return [*(f"> ⚠ {warning}" for warning in warnings), ""]


def _code_fence(body: list[str]) -> str:
    """Граница Markdown длиннее любой последовательности обратных кавычек."""
    longest = max(
        (
            len(match.group())
            for line in body
            for match in re.finditer(r"`+", line)
        ),
        default=0,
    )
    return "`" * max(3, longest + 1)


def render_module_toc(
    configuration: str,
    module: str,
    procedures: list[ProcedureOutline],
    *,
    warnings: list[str],
    extension: str | None = None,
) -> str:
    """Оглавление без тела: модуль может занимать десятки килобайт."""
    источник = (
        f"расширение {extension} конфигурации {configuration}"
        if extension
        else f"конфигурация {configuration}"
    )
    out = _code_warnings(warnings)
    out.extend([f"# Оглавление `{module}`", "", f"Источник: {источник}.", ""])
    if not procedures:
        out.append("В модуле нет разобранных процедур и функций.")
    for item in procedures:
        вид = "функция" if item.function else "процедура"
        доступ = "экспортная" if item.exported else "неэкспортная"
        дополнения = []
        if item.directive:
            дополнения.append(f"&{item.directive}")
        if item.events:
            дополнения.append("обработчик: " + ", ".join(item.events))
        хвост = f" · {' · '.join(дополнения)}" if дополнения else ""
        out.append(
            f"- `{item.address}` — {вид} · {доступ} · строка {item.line} "
            f"· подтверждённых мест вызова: {item.calls}{хвост}"
        )
        out.append(f"  Сигнатура: `{item.signature}`")
    return "\n".join(out).rstrip() + "\n"


def render_procedure_card(
    configuration: str,
    address: str,
    *,
    signature: str,
    compilation: list[str],
    body: list[str],
    start_line: int,
    lines: int,
    warnings: list[str],
    annotation: tuple[str, str] | tuple[()] = (),
    extension: str | None = None,
) -> str:
    """Карточка и окно тела с готовым следующим вызовом."""
    out = _code_warnings(warnings)
    out.extend([f"# `{address}`", "", f"Сигнатура: `{signature}`"])
    if annotation:
        вид, цель = annotation
        цель_текст = f'("{цель}")' if цель else ""
        out.append(f"Аннотация расширения: `&{вид}{цель_текст}`.")
    if compilation:
        out.append("Контекст компиляции: " + ", ".join(compilation) + ".")
    out.extend(["", "## Тело", ""])

    конец = min(len(body), start_line + lines)
    окно = body[start_line:конец]
    if окно:
        граница = _code_fence(окно)
        out.extend([f"{граница}bsl", *окно, граница])
    else:
        out.append(
            f"Запрошено start_line={start_line}, но в теле всего "
            f"{len(body)} строк."
        )

    if конец < len(body):
        аргументы = [
            f"address={json.dumps(address, ensure_ascii=False)}"
        ]
        if configuration:
            аргументы.append(
                f"config={json.dumps(configuration, ensure_ascii=False)}"
            )
        if extension:
            аргументы.append(
                f"extension={json.dumps(extension, ensure_ascii=False)}"
            )
        аргументы.extend([f"start_line={конец}", f"lines={lines}"])
        out.extend(
            [
                "",
                f"Показаны строки {start_line}–{конец - 1} из {len(body)}. "
                "Продолжение:",
                f"`get_procedure({', '.join(аргументы)})`",
            ]
        )
    return "\n".join(out).rstrip() + "\n"


def render_procedure_search(
    configuration: str,
    query: str,
    *,
    exact: list[ProcedureMatch],
    exact_total: int,
    exact_more_modules: int,
    words: list[ProcedureMatch],
    words_more: bool,
    limit: int,
    extension: str | None = None,
) -> str:
    """Два уровня поиска не смешиваются: точное имя и поиск по словам."""
    источник = (
        f"расширении {extension} конфигурации {configuration}"
        if extension
        else f"конфигурации {configuration}"
    )
    out = [f"# Процедуры в {источник}: «{query}»", ""]

    неразрешённые: dict[str, int] = {}
    for совпадение in [*exact, *words]:
        имя = совпадение.address.rpartition("::")[2]
        if совпадение.unresolved_calls:
            неразрешённые[имя] = совпадение.unresolved_calls
    for имя, количество in неразрешённые.items():
        out.append(
            f"> Для имени `{имя}` цель части вызовов не удалось разрешить: "
            f"{количество}. Счётчики ниже показывают только подтверждённые "
            "места вызова конкретного модуля."
        )
    if неразрешённые:
        out.append("")

    def раздел(заголовок: str, совпадения: list[ProcedureMatch]) -> None:
        out.extend([f"## {заголовок} ({len(совпадения)})", ""])
        for совпадение in совпадения:
            вид = "функция" if совпадение.function else "процедура"
            доступ = "экспортная" if совпадение.exported else "неэкспортная"
            свойства = [вид, доступ, f"строка {совпадение.line}"]
            свойства.append(
                f"подтверждённых мест вызова: {совпадение.calls}"
            )
            if совпадение.unresolved_calls:
                свойства.append(
                    f"одноимённых без разрешённой цели: "
                    f"{совпадение.unresolved_calls}"
                )
            if совпадение.annotated:
                свойства.append("есть аннотация расширения")
            out.append(f"- `{совпадение.address}` — {' · '.join(свойства)}")
            out.append(f"  Сигнатура: `{совпадение.signature}`")
        out.append("")

    if exact:
        раздел("Точное имя", exact)
        осталось = exact_total - len(exact)
        if осталось:
            out.extend(
                [
                    f"Показан предел; есть ещё {осталось} в "
                    f"{exact_more_modules} модулях.",
                    "",
                ]
            )
    if words:
        раздел("По словам (только экспортные)", words)
        if words_more:
            подсказка = (
                "Достигнут максимум `limit=50`; уточните `query` или `scope`."
                if limit >= 50
                else "Есть ещё результаты; увеличьте `limit`."
            )
            out.extend([подсказка, ""])

    return "\n".join(out).rstrip() + "\n"


def render_standard_procedure_search(
    configuration: str,
    query: str,
    procedure: str,
    *,
    found_count: int,
    scope: str | None,
    match: ProcedureMatch | None = None,
    extension: str | None = None,
) -> str:
    """Типовое намерение не превращается в случайный адрес процедуры."""
    источник = (
        f"расширении {extension} конфигурации {configuration}"
        if extension
        else f"конфигурации {configuration}"
    )
    out = [f"# Типовая процедура в {источник}: «{query}»", ""]
    out.extend([f"Распознано типовое событие `{procedure}`.", ""])

    if scope is None:
        if found_count:
            out.extend(
                [
                    f"Найдено реализаций: {found_count}. Адрес не выбран: "
                    "укажите `scope` объекта, модуля или формы.",
                    "",
                ]
            )
        else:
            out.extend(
                [
                    "В загруженном коде реализаций с таким точным именем нет.",
                    "",
                ]
            )
        return "\n".join(out).rstrip() + "\n"

    out.extend([f"Область: `{scope}`.", ""])
    if found_count == 0:
        out.extend(
            [
                "В этой области реализаций с таким точным именем нет. "
                "Проверьте `scope` или продолжите обычный поиск другой "
                "формулировкой.",
                "",
            ]
        )
    elif found_count > 1:
        out.extend(
            [
                f"В области найдено реализаций: {found_count}. Адрес не "
                "выбран: укажите точный адрес модуля или формы в `scope`.",
                "",
            ]
        )
    else:
        assert match is not None
        вид = "функция" if match.function else "процедура"
        доступ = "экспортная" if match.exported else "неэкспортная"
        свойства = [
            вид,
            доступ,
            f"строка {match.line}",
            f"подтверждённых мест вызова: {match.calls}",
        ]
        if match.unresolved_calls:
            свойства.append(
                f"одноимённых без разрешённой цели: {match.unresolved_calls}"
            )
        if match.annotated:
            свойства.append("есть аннотация расширения")
        out.extend(
            [
                "## Точное имя (1)",
                "",
                f"- `{match.address}` — {' · '.join(свойства)}",
                f"  Сигнатура: `{match.signature}`",
                "",
            ]
        )
    return "\n".join(out).rstrip() + "\n"


def render_callers(
    configuration: str,
    address: str,
    *,
    code_sites: list[CallerSite],
    confirmed_total: int,
    omitted_modules: int,
    unresolved_sites: list[CallerSite],
    unresolved_total: int,
    metadata: list[MetadataBinding],
    form_bindings: list[FormHandlerBinding],
    form_state: str,
    warnings: list[str],
    extension: str | None = None,
) -> str:
    """Три независимых источника обратных связей, без ложного объединения."""
    источник = (
        f"расширение {extension} конфигурации {configuration}"
        if extension
        else f"конфигурация {configuration}"
    )
    out = _code_warnings(warnings)
    out.extend([f"# Кто вызывает `{address}`", "", f"Источник кода: {источник}.", ""])

    def владелец(site: CallerSite) -> str:
        if site.ambiguous_owner:
            return "владелец не разрешён по границам строки"
        if site.owner is not None:
            return f"`{site.owner}`"
        if site.partial_owner:
            return (
                "точный владелец неизвестен: граница частично "
                "разобранной процедуры не найдена"
            )
        return "вне тел разобранных процедур"

    out.extend(["## Места вызова в коде", ""])
    if unresolved_sites:
        out.append(
            f"> Для одноимённой процедуры найдено мест, где цель не удалось "
            f"разрешить до модуля: {unresolved_total}. Они не входят в число "
            "подтверждённых мест и не приписаны запрошенному адресу."
        )
        out.append("")
    elif unresolved_total:
        out.extend(
            [
                f"> Для одноимённой процедуры найдено мест без разрешённой "
                f"цели: {unresolved_total}. Предел занят подтверждёнными "
                "местами, поэтому ниже показан только этот счётчик.",
                "",
            ]
        )

    if code_sites:
        out.extend([f"Подтверждённых мест: {confirmed_total}.", ""])
        текущий = None
        for site in code_sites:
            if site.module != текущий:
                текущий = site.module
                out.extend([f"### `{site.module}`", ""])
            out.append(f"- строка {site.line} — {владелец(site)}")
        out.append("")
    elif unresolved_total:
        out.extend(
            [
                "Подтверждённых мест именно для запрошенного модуля нет; "
                "есть только одноимённые места без разрешённой цели.",
                "",
            ]
        )
    else:
        out.extend(["Мест вызова в коде нет.", ""])

    if confirmed_total > len(code_sites):
        out.extend(
            [
                f"Показан предел; есть ещё {confirmed_total - len(code_sites)} "
                f"в {omitted_modules} модулях.",
                "",
            ]
        )

    if unresolved_total:
        out.extend(["Одноимённые места без подтверждённой цели:", ""])
        for site in unresolved_sites:
            out.append(
                f"- `{site.module}`: строка {site.line} — {владелец(site)}"
            )
        if unresolved_sites:
            out.append("")
        if unresolved_total > len(unresolved_sites):
            out.extend(
                [
                    f"Есть ещё {unresolved_total - len(unresolved_sites)} "
                    "одноимённых мест без подтверждённой цели.",
                    "",
                ]
            )

    out.extend(["## Привязки из метаданных", ""])
    if metadata:
        if extension:
            out.extend(
                [
                    "Привязки ниже взяты из метаданных основной конфигурации; "
                    "сама привязка не доказывает, что тело выбранного "
                    "расширения выполняется. Это зависит от состава "
                    "заимствования и аннотации; код других расширений в "
                    "ответ не добавлен.",
                    "",
                ]
            )
        подписи = {
            "handler": "подписка на событие",
            "method": "регламентное задание",
        }
        for binding in metadata:
            out.append(
                f"- {подписи[binding.kind]} `{binding.source}` → `{address}`"
            )
        out.append("")
    else:
        out.extend(["Привязок в метаданных нет.", ""])

    out.extend(["## Обработчик формы", ""])
    if form_bindings:
        for binding in form_bindings:
            владелец = "форма" if binding.element is None else f"элемент `{binding.element}`"
            out.append(f"- {владелец}: событие `{binding.event}`")
        out.append("")
    elif form_state == "not_form":
        out.extend(["Запрошенный модуль не является модулем формы.", ""])
    elif form_state == "missing":
        out.extend(
            [
                "Для модуля формы нет структуры Form.xml; доступен только его код.",
                "",
            ]
        )
    elif form_state == "broken":
        out.extend(["Структура Form.xml повреждена; привязки прочитать нельзя.", ""])
    elif form_state == "partial_broken":
        out.extend(
            [
                "Доказательство структуры формы повреждено; привязки событий "
                "не доказаны.",
                "",
            ]
        )
    elif form_state == "partial":
        out.extend(
            [
                "Форма прочитана частично; семантика привязок событий не "
                "доказана.",
                "",
            ]
        )
    else:
        out.extend(["Процедура не назначена обработчиком события формы.", ""])

    if (
        confirmed_total == 0
        and unresolved_total == 0
        and not metadata
        and not form_bindings
        and form_state in ("ready", "not_form")
    ):
        out.extend(
            [
                "Мест вызова и привязок нет. Это не значит, что процедуру не "
                "вызывают: динамические вызовы строкой (`Выполнить`, "
                "`ОписаниеОповещения`, `ПодключитьОбработчикОжидания`) не "
                "индексируются.",
                "",
            ]
        )
    return "\n".join(out).rstrip() + "\n"


# Понятные подписи свойств объектов.
_PROP_TITLES = {
    "hierarchical": "Иерархический",
    "hierarchy_type": "Вид иерархии",
    "code_length": "Длина кода",
    "code_allowed_length": "Допустимая длина кода",
    "description_length": "Длина наименования",
    "posting": "Проведение",
    "number_length": "Длина номера",
    "number_allowed_length": "Допустимая длина номера",
    "number_periodicity": "Периодичность номера",
    "periodicity": "Периодичность",
    "write_mode": "Режим записи",
    "register_kind": "Вид регистра",
    "global": "Глобальный",
    "server": "Сервер",
    "client_managed": "Клиент (управляемое приложение)",
    "server_call": "Вызов сервера",
    "event": "Событие",
    "handler": "Обработчик",
    "method": "Метод",
    "correspondence": "Корреспонденция",
    "chart_of_accounts": "План счетов",
    "ext_dimension_types": "Виды субконто",
    "max_ext_dimension_count": "Максимум субконто",
    "code_type": "Тип кода",
    "number_type": "Тип номера",
    "numerator": "Нумератор",
    "number_rules_resolved": "Правила нумерации подтверждены",
    "real_time_posting": "Оперативное проведение",
    "register_records_deletion": "Удаление движений",
    "register_records_on_post": "Запись движений при проведении",
    "action_period": "Период действия",
    "base_period": "Базовый период",
    "schedule": "График",
    "chart_of_calculation_types": "План видов расчёта",
    "addressing": "Регистр адресации",
    "main_addressing_attribute": "Основной реквизит адресации",
    "current_performer": "Текущий исполнитель",
    "task": "Задача",
    "privileged": "Привилегированный",
    "external_connection": "Внешнее соединение",
    "return_values_reuse": "Повторное использование возвращаемых значений",
    "distributed_infobase": "Распределённая информационная база",
    "is_predefined": "Предопределённое",
    "use": "Использование",
    "characteristic_ext_values": "Дополнительные значения характеристик",
    "registered_documents_count": "Зарегистрированных документов",
    "standard_attributes_count": "Стандартных реквизитов",
    "columns_count": "Граф журнала",
    "commands_count": "Команд",
    "content_count": "Элементов состава",
    "forms_count": "Форм",
    "templates_count": "Макетов",
    "form_type": "Вид формы",
    "explanation": "Пояснение",
    "extended_presentation": "Расширенное представление",
    "list_presentation": "Представление списка",
    "extended_list_presentation": "Расширенное представление списка",
    "default_form": "Основная форма",
    "auxiliary_form": "Дополнительная форма",
    "use_purposes": "Назначения использования",
    "include_help_in_contents": "Включать справку в содержание",
    "use_standard_commands": "Использовать стандартные команды",
    "predefined": "Предопределённый",
    "picture": "Картинка",
    "value_type_string_allowed_length": "Допустимая длина строки",
    "value_type_number_allowed_sign": "Допустимый знак числа",
}

# Свойства, от которых зависит, как писать код и запрос: есть ли у регистра
# срезы, делится ли он на дебет и кредит, появится ли у справочника `Родитель`.
# Печатаются на уровне `fields` — том самом, которым пользуются для написания
# кода, — а не только в `full`, куда за ними никто не пойдёт.
_CODE_RELEVANT_PROPS = (
    "register_kind",
    "periodicity",
    "write_mode",
    "correspondence",
    "chart_of_accounts",
    "ext_dimension_types",
    "max_ext_dimension_count",
    "hierarchical",
    "hierarchy_type",
    "posting",
    "action_period",
    "schedule",
    "chart_of_calculation_types",
    "addressing",
)


def _prop_value(value: object) -> str:
    """`True` в ответе на русском читается хуже, чем «Да»."""
    if isinstance(value, bool):
        return "Да" if value else "Нет"
    return str(value)


def _origin_annotation(sources: tuple[str, ...]) -> str:
    if not sources:
        return ""
    if len(sources) == 1:
        return (
            f" · _объявлен расширением «{sources[0]}» "
            "(статическая выгрузка)_"
        )
    names = ", ".join(f"«{name}»" for name in sources)
    return (
        f" · _источник неоднозначен: расширения {names} "
        "(статические выгрузки)_"
    )


def _field_line(
    item: Field,
    collapse_after: int,
    sources: tuple[str, ...] = (),
) -> str:
    line = f"- `{item.name}` — {item.type_spec(collapse_after)}"
    if item.synonym and item.synonym != item.name:
        line += f" // {item.synonym}"
    if item.comment:
        line += f" — {item.comment}"
    if item.indexing:
        line += f" [{item.indexing}]"
    # Ссылочные поля и табличные части ждут отдельного доказанного корпуса.
    # Даже если XML похож, до этого решения их происхождение не публикуем.
    if not item.object_types():
        line += _origin_annotation(sources)
    return line


def _section(title: str, lines: list[str]) -> list[str]:
    if not lines:
        return []
    return [f"## {title}", "", *lines, ""]


def _fields_section(
    title: str,
    items: list[Field],
    collapse_after: int,
    *,
    object_address: str = "",
    origins: StructureOriginView | None = None,
) -> list[str]:
    return _section(
        title,
        [
            _field_line(
                item,
                collapse_after,
                origins.field_sources(f"{object_address}.{item.name}")
                if origins is not None and object_address
                else (),
            )
            for item in items
        ],
    )


def _refs_section(title: str, refs: list[str]) -> list[str]:
    return _section(title, [f"- `{r}`" for r in refs])


def _virtual_tables_section(tables: list) -> list[str]:
    """Таблицы запроса с уже подставленными именами полей.

    Печатается там же, где реквизиты, а не в `full`: имена полей нужны
    ровно в тот момент, когда агент пишет запрос, и это уровень `fields`.
    """
    if not tables:
        return []

    lines: list[str] = []
    for table in tables:
        lines.append(f"- `{table.name}`")
        if table.dimensions:
            lines.append(f"  измерения: {', '.join(table.dimensions)}")
        if table.resources:
            lines.append(f"  ресурсы: {', '.join(table.resources)}")
        if table.attributes:
            lines.append(f"  реквизиты: {', '.join(table.attributes)}")
        if table.service:
            lines.append(f"  служебные: {', '.join(table.service)}")

    return _section("Таблицы запроса", lines) + [
        "Имя ресурса в виртуальной таблице отличается от имени в конфигураторе:",
        "берите его из списка выше, а не из раздела «Ресурсы».",
        "",
    ]


def _unlimited_strings_notice(obj: MetadataObject) -> list[str]:
    """Оговорка про строки неограниченной длины. Пусто, если таких полей нет.

    Живой промах 2026-08-18: агент сгруппировал запрос по реквизиту-строке без
    ограничения длины, и платформа такой запрос не выполнила. Отдали мы верное,
    но разница читалась только по отсутствию числа в скобках — вывод, который
    надо было сделать самому и знать, чем он грозит. Таких полей от 23% до 38%
    строковых в живых конфигурациях, то есть промах повторится.

    Печатается один раз, до списков полей. В первой редакции блок стоял в
    конце карточки — аккуратнее, но бесполезно: 2026-08-18 живой агент вызвал
    `get_object` с `detail=fields`, получил оговорку целиком и всё равно
    сгруппировал по такому полю. Оговорка была предпоследним абзацем, на 721
    токен позже строки поля, а решение принимается там, где имя копируют.
    Сам рецепт с тех пор стоит и в типе поля (`Field.type_spec`), чтобы строка
    была самодостаточной.

    Имя поля в примере берётся с этой же карточки: по нему видно, о чём речь.
    Псевдоним таблицы не подставляется — в запросе он свой, и наш пришлось бы
    мысленно вычёркивать.

    Каждый запрет в списке подтверждён: агрегатные — самой справкой, остальные
    — прогонами владельца на живой базе 2026-08-18. Тексты ошибок платформы и
    разбивка по происхождению — в `docs/data-sources.md`, раздел «Оговорки в
    карточке». Осторожных формулировок здесь больше нет намеренно: пока запрет
    не проверен, он либо называется предположением, либо не пишется вовсе.

    «Сравнивать» стоит первым и без уточнения, потому что платформа говорит
    именно так — «нельзя сравнивать поля неограниченной длины». Это шире
    условия соединения: под запрет попадают и равенство в ГДЕ, и В (…).
    """
    if not any(поле.is_unlimited_string for _, поле in obj.all_fields()):
        return []
    пример = next(
        поле.name for _, поле in obj.all_fields() if поле.is_unlimited_string
    )
    return [
        "> **Строки неограниченной длины** помечены `(неогр.)`. Платформа не "
        "даёт их сравнивать, группировать и упорядочивать и не пускает в "
        "РАЗЛИЧНЫЕ, ОБЪЕДИНИТЬ и агрегатные КОЛИЧЕСТВО, МИНИМУМ, МАКСИМУМ. "
        "Ограничивайте длину — одинаково в списке выборки и в группировке:",
        ">",
        f">     ПОДСТРОКА({пример}, 1, 100) КАК {пример}",
        ">",
        "> Длину подбирайте по смыслу поля: 100 — не универсальное число.",
        "",
    ]

def render_object(
    obj: MetadataObject,
    detail: str = FIELDS,
    *,
    graph: Graph | None = None,
    collapse_after: int = 5,
    max_incoming: int = 20,
    max_relations: int = 40,
    virtual_tables: list | None = None,
    origins: StructureOriginView | None = None,
) -> str:
    """Markdown-описание объекта на заданном уровне детализации."""
    if detail not in DETAIL_LEVELS:
        raise ValueError(f"Неизвестный уровень детализации: {detail}")

    out: list[str] = [f"# {obj.kind}: {obj.name}"]
    if obj.synonym:
        out.append(f"**{obj.synonym}**")
    if obj.comment and obj.comment != obj.synonym:
        out.append(obj.comment)
    out.append("")
    out.append(f"Полное имя: `{obj.full_name}` · в коде: `{obj.manager_path}`")
    object_sources = origins.object_sources(obj.full_name) if origins else ()
    if object_sources:
        if len(object_sources) == 1:
            out.append(
                f"Объявлен расширением: «{object_sources[0]}» "
                "(статическая файловая выгрузка)."
            )
        else:
            names = ", ".join(f"«{name}»" for name in object_sources)
            out.append(
                "Источник объекта неоднозначен: его объявляют расширения "
                f"{names} (статические файловые выгрузки)."
            )
    if origins and origins.unknown:
        out.append(
            "Происхождение структуры: **неизвестно** — "
            + "; ".join(origins.unknown)
            + ". Непомеченные элементы нельзя считать частью основной "
            "конфигурации."
        )
    out.append("")

    if detail == BRIEF:
        out.append(_brief_summary(obj, graph))
        return "\n".join(out).rstrip() + "\n"

    if detail == FULL:
        props = [
            f"- {_PROP_TITLES.get(k, k)}: `{_prop_value(v)}`"
            for k, v in sorted(obj.props.items())
            if v not in (None, "")
        ]
        out += _section("Свойства", props)
    else:
        # Булево здесь не отбрасывается по ложности: «Корреспонденция: Нет» —
        # такой же ответ на вопрос, как «Да», и молчание читалось бы как
        # «свойства нет».
        существенные = [
            f"- {_PROP_TITLES.get(k, k)}: `{_prop_value(obj.props[k])}`"
            for k in _CODE_RELEVANT_PROPS
            if obj.props.get(k) not in (None, "")
        ]
        out += _section("Свойства", существенные)

    out += _unlimited_strings_notice(obj)
    out += _fields_section(
        "Реквизиты",
        obj.attributes,
        collapse_after,
        object_address=obj.full_name,
        origins=origins,
    )
    out += _fields_section("Измерения", obj.dimensions, collapse_after)
    out += _fields_section("Ресурсы", obj.resources, collapse_after)
    out += _virtual_tables_section(virtual_tables or [])

    if obj.value_type is not None:
        out += _section(
            "Тип значения", [f"- {obj.value_type.type_spec(collapse_after)}"]
        )

    if obj.tabular_parts:
        out.append("## Табличные части")
        out.append("")
        for part in obj.tabular_parts:
            heading = f"### {part.name}"
            if part.synonym and part.synonym != part.name:
                heading += f" — {part.synonym}"
            out.append(heading)
            out.append("")
            for item in part.attributes:
                out.append(_field_line(item, collapse_after))
            out.append("")

    if obj.enum_values:
        out += _section(
            "Значения",
            [
                f"- `{name}`" + (f" — {synonym}" if synonym and synonym != name else "")
                for name, synonym in obj.enum_values
            ],
        )

    if obj.predefined:
        out += _section(
            "Предопределённые",
            [f"- `{obj.manager_path}.{name}`" for name in obj.predefined],
        )

    if obj.forms:
        out += _refs_section("Формы", obj.forms)

    if obj.relations:
        lines = []
        for relation in obj.relations[:max_relations]:
            state = "разрешена" if relation.state == "resolved" else "не разрешена"
            details = ", ".join(
                f"{key}={value}"
                for key, value in relation.properties
                if value
            )
            suffix = f" · {details}" if details else ""
            lines.append(
                f"- `{relation.target}` — "
                f"{EDGE_TITLES.get(relation.kind, relation.kind)} · {state}{suffix}"
            )
        if len(obj.relations) > max_relations:
            lines.append(f"- … ещё {len(obj.relations) - max_relations}")
        out += _section("Связи файловой выгрузки", lines)

    out += _refs_section("Движения", obj.movements)

    if detail == FULL:
        out += _refs_section("Вводится на основании", obj.based_on)
        out += _refs_section("Владельцы", obj.owners)
        if graph is not None:
            out += _incoming_section(obj, graph, max_incoming)

    return "\n".join(out).rstrip() + "\n"


def _brief_summary(obj: MetadataObject, graph: Graph | None) -> str:
    parts: list[str] = []
    if obj.attributes:
        parts.append(f"реквизитов: {len(obj.attributes)}")
    if obj.dimensions:
        parts.append(f"измерений: {len(obj.dimensions)}")
    if obj.resources:
        parts.append(f"ресурсов: {len(obj.resources)}")
    if obj.tabular_parts:
        names = ", ".join(p.name for p in obj.tabular_parts)
        parts.append(f"табличных частей: {len(obj.tabular_parts)} ({names})")
    if obj.enum_values:
        parts.append(f"значений: {len(obj.enum_values)}")
    if obj.predefined:
        parts.append(f"предопределённых: {len(obj.predefined)}")
    if obj.movements:
        parts.append(f"движения: {len(obj.movements)}")
    if obj.forms:
        parts.append(f"форм: {len(obj.forms)}")
    if obj.relations:
        resolved = sum(item.state == "resolved" for item in obj.relations)
        parts.append(f"связей Source B: {resolved} из {len(obj.relations)}")
    if graph is not None:
        incoming = len(graph.incoming(obj.full_name))
        if incoming:
            parts.append(f"на объект ссылаются: {incoming}")
    return ", ".join(parts) if parts else "нет состава"


def _incoming_section(obj: MetadataObject, graph: Graph, limit: int) -> list[str]:
    edges = graph.incoming(obj.full_name)
    if not edges:
        return []

    grouped: dict[str, list[str]] = {}
    for edge in edges:
        grouped.setdefault(edge.source, []).append(edge.title)

    lines = []
    for source in sorted(grouped)[:limit]:
        lines.append(f"- `{source}` — {', '.join(sorted(set(grouped[source])))}")
    if len(grouped) > limit:
        lines.append(f"- … ещё {len(grouped) - limit}")
    return _section(f"На объект ссылаются ({len(grouped)})", lines)


def render_configuration_summary(config: Configuration, graph: Graph | None = None) -> str:
    """Карта конфигурации — то, с чего агент начинает работу."""
    out = [
        f"# Конфигурация: {config.synonym or config.name}",
        "",
        f"- Имя: `{config.name}`",
        f"- Версия: {config.version}",
        f"- Поставщик: {config.vendor}",
        f"- Платформа: {config.platform or 'неизвестна'}",
        f"- Выгружено: {config.exported_at}",
        f"- Объектов: {len(config)}",
    ]
    if config.compatibility_mode:
        out.append(f"- Режим совместимости: {config.compatibility_mode}")
    if graph is not None:
        out.append(f"- Связей: {len(graph.edges)}")
    out.append("")

    if config.truncated:
        out.append("> **Выгрузка неполная** — сделана с ограничением числа объектов.")
        out.append("")
    if not config.predefined_available:
        out.append(
            "> **Сведения о предопределённых элементах не получены**; "
            "это не доказывает их отсутствие в базе."
        )
        out.append("")
    for warning in config.warnings:
        out.append(f"> Предупреждение: {warning}")
    if config.warnings:
        out.append("")

    out.append("## Состав")
    out.append("")
    for kind, count in config.kinds().items():
        out.append(f"- {kind}: {count}")
    out.append("")

    if graph is not None:
        hubs = graph.hubs(10)
        if hubs:
            out.append("## Наиболее используемые объекты")
            out.append("")
            for name, count in hubs:
                obj = config.get(name)
                title = f" — {obj.title}" if obj and obj.synonym else ""
                out.append(f"- `{name}`{title}: ссылок {count}")
            out.append("")

    return "\n".join(out).rstrip() + "\n"


# --------------------------------------------------------------- справка


def _version_notice(item: SyntaxItem, resolution) -> list[str]:
    """Оговорка о том, что для этой версии точных сведений нет.

    Молчать нельзя, но и общий баннер на каждом ответе бесполезен: его
    перестают читать. Поэтому оговорка появляется только у тех элементов,
    которые между известными справками менялись, и называет оба состояния —
    выбирать за агента сервер не должен.
    """
    if resolution is None or resolution.exact:
        return []

    if not resolution.alternatives:
        # Сравнивать не с чем: все загруженные справки новее этой версии.
        # Молчать всё равно нельзя — сведения взяты из чужой версии.
        return [
            f"> Точной справки для платформы **{resolution.asked}** нет: сведения "
            f"ниже взяты из справки **{resolution.platform}**, а она новее. "
            "Сигнатура и доступность могли отличаться — проверьте в "
            "конфигураторе или загрузите справку своей версии.",
            "",
        ]

    out = [
        f"> Точной справки для платформы **{resolution.asked}** нет, "
        "а между известными версиями элемент менялся:"
    ]
    for facts in resolution.alternatives:
        описание = facts.signature or ", ".join(facts.availability) or facts.name_ru
        out.append(f"> - **{facts.platform}**: `{описание}`")
    out.append(
        "> Какое состояние действует в вашей версии — проверьте в конфигураторе "
        "или загрузите справку этой платформы. Не додумывайте."
    )
    out.append("")
    return out


def render_syntax_item(
    item: SyntaxItem, detail: str = FIELDS, resolution=None
) -> str:
    """Карточка элемента платформы.

    Доступность выводится всегда и первой строкой после заголовка: это то,
    из-за чего сгенерированный код чаще всего не компилируется.

    `resolution` — что известно об элементе для версии конфигурации. Он же
    решает, какую сигнатуру и какую доступность показывать: между версиями
    менялись и та, и другая, а разница в один параметр — это ошибка
    компиляции, а не мелочь.
    """
    title = KIND_TITLES.get(item.kind, item.kind)
    out = [f"# {title}: {item.full_ru}"]
    if item.full_en:
        out.append(f"`{item.full_en}`")
    out.append("")

    if resolution is not None and resolution.name_ru and resolution.name_ru != item.name_ru:
        out.append(
            f"В платформе **{resolution.platform}** называется "
            f"`{resolution.name_ru}` — переименован в более поздних версиях."
        )
        out.append("")

    facts = []
    if item.since:
        facts.append(f"с версии платформы **{item.since}**")
    if item.until:
        facts.append(f"по версию **{item.until}** включительно")
    if item.readonly:
        facts.append("только чтение")
    if facts:
        out.append(" · ".join(facts))
    availability = (
        resolution.availability
        if resolution is not None and resolution.availability
        else item.availability
    )
    if availability:
        out.append(f"Доступность: {', '.join(availability)}")
    out.append("")

    out += _version_notice(item, resolution)

    if item.description:
        out.append(item.description)
        out.append("")

    base_signature = item.variants[0].signature if item.variants else ""
    своя_сигнатура = (
        resolution.signature
        if resolution is not None and resolution.signature != base_signature
        else ""
    )

    if detail == BRIEF:
        показать = своя_сигнатура or base_signature
        if показать:
            out.append(f"```bsl\n{показать}\n```")
        return "\n".join(out).rstrip() + "\n"

    if своя_сигнатура:
        # Сигнатура этой версии отличается от свежей. Показываем только её:
        # описание параметров хранится по свежей справке, и вывести его рядом
        # значит предложить агенту параметр, которого в его платформе нет.
        out.append(f"Сигнатура в платформе **{resolution.platform}**:")
        out.append("")
        out.append("```bsl")
        out.append(своя_сигнатура)
        out.append("```")
        out.append("")
        out.append(
            "> Разбор параметров сохранён только по самой свежей справке и здесь "
            "не приводится — он описывает другой набор. Ориентируйтесь на "
            "сигнатуру выше."
        )
        # Примечание относится к элементу, а не к разбору параметров: оно
        # предупреждает о поведении метода и нужно в любой версии.
        if item.note:
            out.append("")
            out.append(f"> {item.note}")
        return "\n".join(out).rstrip() + "\n"

    for variant in item.variants:
        if variant.title:
            out.append(f"## Вариант синтаксиса: {variant.title}")
            out.append("")
        if variant.signature:
            out.append("```bsl")
            out.append(variant.signature)
            out.append("```")
            out.append("")
        if variant.params:
            out.append("Параметры:")
            for param in variant.params:
                line = f"- `{param.name}` — {', '.join(param.types) or '?'}"
                if not param.required:
                    line += ", необязательный"
                if param.default:
                    line += f", по умолчанию {param.default}"
                out.append(line)
                if param.description:
                    out.append(f"  {param.description}")
            out.append("")
        if variant.returns:
            out.append(f"Возвращает: {', '.join(variant.returns)}")
            out.append("")
        if variant.description:
            out.append(variant.description)
            out.append("")

    if item.values:
        out += _section("Значения", [f"- `{v}`" for v in item.values])

    if item.examples and detail == FULL:
        out.append("## Пример")
        out.append("")
        out.append("```bsl")
        out.append(item.examples[0])
        out.append("```")
        out.append("")

    if item.note:
        out.append(f"> {item.note}")

    return "\n".join(out).rstrip() + "\n"
