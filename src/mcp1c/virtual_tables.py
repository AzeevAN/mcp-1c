"""Виртуальные таблицы регистров: имена полей, которых нет в метаданных.

Ресурс регистра называется в запросе не так, как в конфигураторе. `Количество`
в основной таблице превращается в `КоличествоОстаток` в `.Остатки` и в
`КоличествоПриход`/`КоличествоРасход`/`КоличествоОборот` в `.Обороты`. Из-за
этого агент пишет запрос, который выглядит правильным и падает на «поле не
найдено»: структуру объекта он получил верную, а таблицу спрашивает другую.

Знание разложено на две половины, и порознь ни одна не помогает:

- **справка платформы**, раздел `tables/` — какие таблицы бывают у вида
  регистра и как в них называются поля. Шаблонами: `<Имя ресурса>Остаток`;
- **метаданные конфигурации** — как ресурсы зовут на самом деле.

Здесь они соединяются: шаблон из справки, имена из выгрузки. Наизусть
суффиксы не сочиняются — нет справки, нет и блока (`test_без_справки_ничего
_не_выдумывается`).
"""

from dataclasses import dataclass, field

from .model import MetadataObject
from .syntax_model import SyntaxIndex

# Плейсхолдеры справки, которые подставляются из метаданных.
_DIMENSION = "<Имя измерения>"
_RESOURCE = "<Имя ресурса>"
_ATTRIBUTE = "<Имя реквизита>"

# Общие реквизиты в выгрузке не представлены: их состав — свойство
# конфигурации целиком, а не объекта. Подставить нечем, выдумывать нельзя.
_SKIPPED = ("<Имя общего реквизита>",)

# Детализация периода: у каждой оборотной таблицы десяток полей вида
# ПериодГод/ПериодДекада/ПериодСекунда. Разворачивать их на каждый вызов —
# шум в контексте агента; сам `Период` при этом нужен.
_PERIOD_DETAIL = "Период"

# `.Остатки` и `.ОстаткиИОбороты` существуют только у регистров остатков —
# так сказано в описании самой таблицы («Таблица существует только для
# регистров остатков»). Признак вида регистра приходит из выгрузки
# (`register_kind`), а не выводится из состава полей.
_BALANCE_ONLY = ("Остатки", "ОстаткиИОбороты")

# Приход и расход — следствие вида движения записи, а он есть только у
# регистра остатков. У оборотного регистра в `.Обороты` остаётся один
# `<Имя ресурса>Оборот`.
#
# ВНИМАНИЕ: единственное место модуля, где правило взято НЕ из справки.
# Справка перечисляет для `.Обороты` все три поля без оговорки про вид
# регистра. Отдать их для оборотного регистра значит выдать агенту поле,
# которого в таблице нет, — ровно та ошибка, ради которой писался модуль.
# Требует подтверждения на живой базе; до подтверждения ограничение
# сознательно строже справки.
_MOVEMENT_SUFFIXES = ("Приход", "Расход")

# Срезы существуют только у периодического регистра сведений: у
# непериодического нет самого поля `Период`, по которому срез берётся.
# Признак — `periodicity` из выгрузки; значение `Непериодический` названо
# так же, как в перечислении платформы `ПериодичностьРегистраСведений`.
#
# Нет признака в выгрузке — срезы не показываем вовсе. Так устроены все
# выгрузки до 2026-08-17: обработка читала несуществующее свойство
# `Периодичность` и молча теряла его вместе с режимом записи. Показать срез
# непериодическому регистру — ровно та ошибка, ради которой писался модуль,
# и она уже случилась на живом агенте.
_SLICE_SUFFIXES = ("СрезПервых", "СрезПоследних")
_NONPERIODIC = "Непериодический"

# Субконто нумеруются, а не называются: `Субконто1`, `ВидСубконтоДт2`. Предел
# нумерации — свойство плана счетов (`МаксКоличествоСубконто`), а не регистра,
# поэтому разворачивается только когда план счетов рядом.
_EXT_DIMENSION_NUMBER = "<Номер субконто>"

# `ФактическийПериодДействия` существует только у регистра расчёта с периодом
# действия: у регистра без него нет самого понятия «фактический период».
_ACTION_PERIOD_ONLY = ("ФактическийПериодДействия",)

# `ДанныеГрафика` описывает ресурсы ГРАФИКА, а не регистра расчёта: график —
# отдельный регистр сведений, указанный свойством `schedule`. Без него имена
# полей взять неоткуда, и таблица не показывается.
_SCHEDULE_RESOURCE = "<Имя ресурса графика>"


@dataclass(slots=True)
class TableTemplate:
    """Шаблон таблицы из справки: суффикс и поля с плейсхолдерами.

    Считается один раз на справку. Без этого каждый `get_object` по регистру
    перебирал все 25 тысяч элементов справки — 14,8 мс против 0,04 мс на
    объекте без таблиц. Тот же приём и по той же причине, что `by_name`
    в `LoadedSyntax`.
    """

    suffix: str
    description: str
    fields: list[str]


@dataclass(slots=True)
class VirtualTable:
    """Одна таблица запроса с уже подставленными именами полей."""

    name: str
    suffix: str
    description: str = ""
    dimensions: list[str] = field(default_factory=list)
    resources: list[str] = field(default_factory=list)
    attributes: list[str] = field(default_factory=list)
    service: list[str] = field(default_factory=list)

    def all_fields(self) -> list[str]:
        return self.dimensions + self.resources + self.attributes + self.service


def _placeholder(name: str) -> tuple[str, str] | None:
    """Разобрать имя таблицы справки на вид объекта и суффикс.

    `РегистрНакопления.<Имя регистра накопления>.Остатки` → `(«РегистрНакопления», «Остатки»)`.
    Возвращает `None`, если имя устроено иначе.
    """
    kind, _, rest = name.partition(".")
    if not rest.startswith("<"):
        return None

    closing = rest.find(">")
    if closing < 0:
        return None

    return kind, rest[closing + 1 :].lstrip(".")


def _field_order(key: str) -> tuple[int, str]:
    """Порядок полей внутри таблицы — тот же, что в справке.

    Обход словаря даёт порядок разбора, а он для слитого индекса зависит от
    того, какая справка легла первой: `КоличествоПриход, КоличествоОборот,
    КоличествоРасход` вместо `Приход, Расход, Оборот`. Номер в идентификаторе
    (`fields/field73`) — это и есть порядок страницы справки.
    """
    tail = key.rsplit("/", 1)[-1]
    digits = "".join(character for character in tail if character.isdigit())
    return (int(digits) if digits else 0, tail)


def _fields_by_table(syntax: SyntaxIndex) -> dict[str, list[str]]:
    """Поля справки, разложенные по таблицам, за один проход.

    Искать поля каждой таблицы отдельным перебором — 736 таблиц на 25 691
    элемент, 81 мс на старте. Один проход по словарю стоит на порядок меньше.
    """
    collected: dict[str, list[tuple[str, str]]] = {}
    for key, item in syntax.items.items():
        if item.kind != "query_field":
            continue
        table_id, separator, tail = key.partition("/fields/")
        if not separator:
            continue
        collected.setdefault(table_id, []).append((tail, item.name_ru))

    return {
        table_id: [name for _, name in sorted(fields, key=lambda p: _field_order(p[0]))]
        for table_id, fields in collected.items()
    }


def _expand_numbered(fields: list[str], count: int) -> list[str] | None:
    """Развернуть нумерованные субконто: `Субконто<Номер субконто>` → 1…N.

    `None` означает «в таблице есть субконто, а предела нумерации нет» —
    показывать её нельзя, имена полей неизвестны.
    """
    if not any(_EXT_DIMENSION_NUMBER in field for field in fields):
        return fields

    if count <= 0:
        return None

    expanded: list[str] = []
    for field in fields:
        if _EXT_DIMENSION_NUMBER not in field:
            expanded.append(field)
            continue
        expanded.extend(
            field.replace(_EXT_DIMENSION_NUMBER, str(number))
            for number in range(1, count + 1)
        )
    return expanded


def _expand_schedule(fields: list[str], resources: list[str]) -> list[str] | None:
    """Развернуть ресурсы графика: `<Имя ресурса графика>ПериодДействия`.

    `None` — в таблице есть поля графика, а самого графика у регистра нет:
    назвать их нечем.
    """
    if not any(_SCHEDULE_RESOURCE in field for field in fields):
        return fields

    if not resources:
        return None

    expanded: list[str] = []
    for field in fields:
        if _SCHEDULE_RESOURCE not in field:
            expanded.append(field)
            continue
        suffix = field.split(">", 1)[1]
        expanded.extend(f"{resource}{suffix}" for resource in resources)
    return expanded


def _pick_variant(
    choices: list[TableTemplate], correspondence: object
) -> TableTemplate | None:
    """Выбрать шаблон, когда справка описывает таблицу в двух вариантах.

    Так устроен регистр бухгалтерии: с поддержкой корреспонденции поля
    делятся на дебет и кредит (`ОстатокДт`, `ОборотКт`), без неё остаются
    одинарными. Признак приходит из выгрузки; если его нет — оба варианта
    равновероятны, и показывать нельзя ни один.
    """
    if len(choices) == 1:
        return choices[0]

    # Часть таблиц справка описывает дважды одинаково (`Остатки`,
    # `ОстаткиИОбороты` — тот же набор полей в другом порядке). Это дубль
    # разметки, а не два варианта: выбирать не из чего.
    наборы = {frozenset(template.fields) for template in choices}
    if len(наборы) == 1:
        return choices[0]

    if not isinstance(correspondence, bool):
        return None

    # Корреспонденция добавляет поля, а не заменяет их: к `Оборот` прибавляются
    # `ОборотДт`, `ОборотКт` и `КорОборот`. Конкретные суффиксы у каждой таблицы
    # свои, а вот отношение «надмножество» держится везде — по нему и выбираем.
    по_размеру = sorted(choices, key=lambda template: len(template.fields))
    return по_размеру[-1] if correspondence else по_размеру[0]


def _has_slices(obj: MetadataObject) -> bool:
    """Есть ли у регистра сведений срезы — то есть периодический ли он."""
    periodicity = obj.props.get("periodicity")
    return bool(periodicity) and periodicity != _NONPERIODIC


def _unknown_placeholder(template: str) -> bool:
    """Плейсхолдер, который нечем заполнить из этого объекта.

    Известные — измерение, ресурс, реквизит и общий реквизит (последний
    осознанно пропускается). Всё остальное (`<Имя ресурса графика>`,
    `<Имя ресурса базового регистра>`) описывает другой объект.
    """
    if "<" not in template:
        return False

    known = (_DIMENSION, _ATTRIBUTE, _RESOURCE, *_SKIPPED)
    return not template.startswith(known)


def _expand(template: str, obj: MetadataObject) -> tuple[str, list[str]]:
    """Подставить в шаблон поля объекта. Возвращает вид поля и имена.

    Ресурсы здесь не разворачиваются: их суффиксы собираются отдельно, чтобы
    поля одного ресурса шли подряд (`КоличествоПриход`, `КоличествоРасход`,
    `КоличествоОборот`), а не столбцами по суффиксу. Разработчик спрашивает
    «что есть по количеству», а не «где у всех приход».
    """
    # Суффикс бывает не только у ресурса: при корреспонденции измерения тоже
    # раздваиваются — `ОрганизацияДт`, `ОрганизацияКт`.
    if template.startswith(_DIMENSION):
        suffix = template[len(_DIMENSION) :]
        return "dimensions", [f"{f.name}{suffix}" for f in obj.dimensions]

    if template.startswith(_ATTRIBUTE):
        suffix = template[len(_ATTRIBUTE) :]
        return "attributes", [f"{f.name}{suffix}" for f in obj.attributes]

    if template in _SKIPPED:
        return "service", []

    # Свёртка детализации периода: `Период` остаётся, `ПериодГод` уходит.
    if template.startswith(_PERIOD_DETAIL) and template != _PERIOD_DETAIL:
        return "service", []

    return "service", [template]


def build_table_index(syntax: SyntaxIndex | None) -> dict[str, list[TableTemplate]]:
    """Шаблоны таблиц запроса по виду объекта: `РегистрНакопления` → таблицы.

    Пустой словарь — нормальный ответ: справки нет, или в ней нет разметки
    полей (так размечены таблицы в справке 8.3.5 — имя есть, полей нет).
    """
    if syntax is None:
        return {}

    fields_by_table = _fields_by_table(syntax)

    index: dict[str, list[TableTemplate]] = {}
    for table_id, item in syntax.items.items():
        if item.kind != "query_table":
            continue

        parsed = _placeholder(item.name_ru)
        if parsed is None:
            continue

        kind, suffix = parsed

        # Таблица изменений живёт планами обмена, а не запросами к остаткам.
        if suffix == "Изменения":
            continue

        fields = fields_by_table.get(table_id, [])
        if not fields:
            continue

        index.setdefault(kind, []).append(
            TableTemplate(suffix=suffix, description=item.description, fields=fields)
        )

    # Основная таблица первой, дальше по имени: порядок обхода справки
    # зависит от разметки, а выдача агенту должна быть устойчивой.
    for templates in index.values():
        templates.sort(key=lambda t: (t.suffix != "", t.suffix))

    return index


def virtual_tables(
    obj: MetadataObject,
    tables: dict[str, list[TableTemplate]] | None,
    *,
    ext_dimension_count: int = 0,
    schedule_resources: list[str] | None = None,
) -> list[VirtualTable]:
    """Таблицы запроса объекта с подставленными именами полей.

    `ext_dimension_count` — предел нумерации субконто из плана счетов, по
    которому ведётся регистр бухгалтерии. Ноль означает «плана счетов рядом
    нет»: таблицы с субконто тогда не показываются, потому что назвать их
    поля нечем.

    Пустой список — нормальный ответ: объект не регистр или справки нет.
    """
    if not tables or not obj.resources:
        return []

    # Вид регистра знает только регистр накопления: в выгрузке `register_kind`
    # проставлен ему одному. Регистру сведений он не нужен вовсе, а без него
    # `СрезПоследних` — самая частая таблица в отчётах — до агента не доходила.
    register_kind = obj.props.get("register_kind")

    # Один суффикс, два шаблона — так справка описывает регистр бухгалтерии:
    # с поддержкой корреспонденции (`ОстатокДт`/`ОстатокКт`) и без неё.
    # Выбирает признак из выгрузки; без него оба варианта равновероятны и
    # показывать нельзя ни один.
    correspondence = obj.props.get("correspondence")
    variants: dict[str, list[TableTemplate]] = {}
    for template in tables.get(obj.kind, []):
        variants.setdefault(template.suffix, []).append(template)

    result: list[VirtualTable] = []
    for suffix, choices in variants.items():
        template = _pick_variant(choices, correspondence)
        if template is None:
            continue

        # Плейсхолдер в самом имени таблицы (`База<Имя базового регистра
        # расчета>`, `<Имя перерасчета>`) — имя, которое агенту нечем
        # заполнить: подставляется не этот объект, а другой, которого в
        # выгрузке рядом нет.
        if "<" in template.suffix:
            continue

        # Чужие плейсхолдеры в полях (`<Имя ресурса графика>ПериодДействия`)
        # означают, что состав таблицы берётся не из этого объекта. Показать
        # его ресурсы под этими именами — та же ошибка, ради которой писался
        # модуль, только наоборот.
        fields = _expand_numbered(template.fields, ext_dimension_count)
        if fields is not None:
            fields = _expand_schedule(fields, schedule_resources or [])
        if fields is None or any(_unknown_placeholder(f) for f in fields):
            continue

        if template.suffix in _BALANCE_ONLY and register_kind == "Обороты":
            continue

        if template.suffix in _SLICE_SUFFIXES and not _has_slices(obj):
            continue

        if template.suffix in _ACTION_PERIOD_ONLY and not obj.props.get("action_period"):
            continue

        table = VirtualTable(
            name=f"{obj.full_name}{'.' + template.suffix if template.suffix else ''}",
            suffix=template.suffix,
            description=template.description,
        )
        resource_suffixes: list[str] = []
        for field_template in fields:
            if field_template.startswith(_RESOURCE):
                resource_suffix = field_template[len(_RESOURCE) :]
                if register_kind != "Остатки" and resource_suffix in _MOVEMENT_SUFFIXES:
                    continue
                resource_suffixes.append(resource_suffix)
                continue
            bucket, names = _expand(field_template, obj)
            getattr(table, bucket).extend(names)

        table.resources = [
            f"{resource.name}{suffix}"
            for resource in obj.resources
            for suffix in resource_suffixes
        ]

        result.append(table)

    return result
