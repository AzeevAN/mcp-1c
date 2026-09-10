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

from .model import Configuration, Field, MetadataObject
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
# Справка перечисляет для `.Обороты` все три поля без оговорки про вид
# регистра. Живая проверка владельца 2026-08-26 закрыла этот пробел: у
# оборотного регистра `<Ресурс>Оборот` компилируется, а отдельные запросы к
# `<Ресурс>Приход` и `<Ресурс>Расход` дают «Поле не найдено». Отдать их агенту
# значило бы посоветовать несуществующие поля — ровно та ошибка, ради которой
# писался модуль.
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
_BASE_RESOURCE = "<Имя ресурса базового регистра>"
_BASE_CUT = "<Имя разреза базового регистра>"
_BASE_REGISTER = "<Имя базового регистра расчета>"


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


@dataclass(slots=True, frozen=True)
class TableAvailability:
    """Почему условная таблица конкретного регистра доступна или отсутствует."""

    name: str
    suffix: str
    available: bool
    reason: str


@dataclass(slots=True)
class VirtualTableReport:
    """Фактические таблицы и встроенное объяснение платформенных условий."""

    tables: list[VirtualTable] = field(default_factory=list)
    availability: list[TableAvailability] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)


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


def _expand_numbered(fields: list[str], count: int) -> list[str]:
    """Развернуть нумерованные субконто: `Субконто<Номер субконто>` → 1…N.

    Без плана счетов нумерованные поля убираются, но сама основная таблица не
    исчезает: у регистра без плана остаются период, регистратор и вид движения.
    """
    if not any(_EXT_DIMENSION_NUMBER in field for field in fields):
        return fields

    if count <= 0:
        return [field for field in fields if _EXT_DIMENSION_NUMBER not in field]

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


def _expand_base_resources(fields: list[str], resources: list[str]) -> list[str]:
    """Подставить ресурсы доказанного базового регистра расчёта.

    Поля разрезов зависят от параметра виртуальной таблицы `Разрезы` и заранее
    не перечислимы; сама таблица из-за этого не исчезает.
    """
    expanded: list[str] = []
    for field_name in fields:
        if field_name.startswith(_BASE_CUT):
            continue
        if field_name.startswith(_BASE_RESOURCE):
            suffix = field_name[len(_BASE_RESOURCE) :]
            expanded.extend(f"{resource}{suffix}" for resource in resources)
        else:
            expanded.append(field_name)
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
    if not isinstance(periodicity, str):
        return False
    normalized = periodicity.casefold().replace(" ", "").replace("_", "")
    return normalized not in {
        "",
        _NONPERIODIC.casefold(),
        "nonperiodical",
    }


def _balance_register(obj: MetadataObject) -> bool:
    value = obj.props.get("register_kind")
    if not isinstance(value, str):
        return False
    return value.casefold().replace(" ", "").replace("_", "") in {
        "остатки",
        "balance",
        "balancebalance",
    }


def _positive_int(value: object) -> int:
    return value if type(value) is int and value > 0 else 0


def _base_registers(
    obj: MetadataObject,
    configuration: Configuration | None,
) -> tuple[list[MetadataObject], str]:
    if obj.props.get("base_period") is not True:
        return [], "базовый период выключен"
    if configuration is None:
        return [], "нет resolved-конфигурации для связи с базовыми планами видов расчета"
    plan_name = obj.props.get("chart_of_calculation_types")
    plan = configuration.get(plan_name) if isinstance(plan_name, str) else None
    if plan is None or plan.kind != "ПланВидовРасчета":
        return [], "не задан или не разрешен план видов расчета"
    raw_base_plans = plan.props.get("base_calculation_types")
    if not isinstance(raw_base_plans, list) or not all(
        isinstance(item, str) for item in raw_base_plans
    ):
        return [], "план видов расчета не задает базовые планы"
    base_plans = set(raw_base_plans)
    result = sorted(
        (
            candidate
            for candidate in configuration.objects.values()
            if candidate.kind == "РегистрРасчета"
            and candidate.props.get("chart_of_calculation_types") in base_plans
        ),
        key=lambda candidate: candidate.full_name.casefold(),
    )
    if not result:
        return [], "для базовых планов не найден ни один регистр расчета"
    return result, ""


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


def _accounting_fields(
    fields: list[Field],
    template_suffix: str,
    table_suffix: str,
    correspondence: bool,
) -> list[Field]:
    """Отобрать поля бухгалтерского регистра для конкретного плейсхолдера."""
    if not correspondence and table_suffix in {"", "ДвиженияССубконто"}:
        return fields if template_suffix == "" else []
    if table_suffix in {"", "ДвиженияССубконто", "ОборотыДтКт"}:
        if template_suffix in {"", "Оборот"}:
            return [item for item in fields if item.balance is True]
        if template_suffix in {"Дт", "Кт", "ОборотДт", "ОборотКт"}:
            return [item for item in fields if item.balance is False]
    if table_suffix == "Обороты":
        if template_suffix == "":
            return fields
        if template_suffix == "Кор":
            return [item for item in fields if item.balance is False]
        if template_suffix.startswith("КорОборот"):
            return [item for item in fields if item.balance is False]
        if template_suffix.startswith("Оборот"):
            return fields
    return fields


def _accounting_service_field(
    template: str,
    obj: MetadataObject,
    table_suffix: str,
    ext_dimension_count: int,
) -> bool:
    if template == "УточнениеПериода":
        return table_suffix != "Остатки" and _positive_int(
            obj.props.get("period_adjustment_length")
        ) > 0
    correspondence = obj.props.get("correspondence") is True
    has_chart = bool(obj.props.get("chart_of_accounts"))
    if template in {"СчетДт", "СчетКт", "КорСчет"} or template.startswith(
        (
            "СубконтоДт",
            "СубконтоКт",
            "ВидСубконтоДт",
            "ВидСубконтоКт",
            "КорСубконто",
        )
    ):
        return correspondence and has_chart
    if template == "Счет":
        return has_chart and (
            not correspondence or table_suffix not in {"", "ДвиженияССубконто"}
        )
    if template.startswith(("Субконто", "ВидСубконто")):
        return has_chart and ext_dimension_count > 0
    return True


def _expand(
    template: str,
    obj: MetadataObject,
    *,
    table_suffix: str,
    ext_dimension_count: int,
) -> tuple[str, list[str]]:
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
        fields = obj.dimensions
        if obj.kind == "РегистрБухгалтерии":
            fields = _accounting_fields(
                fields,
                suffix,
                table_suffix,
                obj.props.get("correspondence") is True,
            )
        return "dimensions", [f"{f.name}{suffix}" for f in fields]

    if template.startswith(_ATTRIBUTE):
        suffix = template[len(_ATTRIBUTE) :]
        return "attributes", [
            f"{f.name}{suffix}" for f in obj.attributes if not f.standard
        ]

    if template in _SKIPPED:
        return "service", []

    # Свёртка детализации периода: `Период` остаётся, `ПериодГод` уходит.
    if template.startswith(_PERIOD_DETAIL) and template != _PERIOD_DETAIL:
        return "service", []

    if obj.kind == "РегистрБухгалтерии" and not _accounting_service_field(
        template, obj, table_suffix, ext_dimension_count
    ):
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


def _availability(
    obj: MetadataObject,
    *,
    ext_dimension_count: int,
    schedule_resources: list[str],
    base_registers: list[MetadataObject],
    base_error: str,
) -> list[TableAvailability]:
    result: list[TableAvailability] = []

    def add(suffix: str, available: bool, reason: str) -> None:
        result.append(
            TableAvailability(
                name=f"{obj.full_name}.{suffix}",
                suffix=suffix,
                available=available,
                reason=reason,
            )
        )

    if obj.kind == "РегистрСведений":
        available = _has_slices(obj)
        reason = (
            "регистр периодический"
            if available
            else "регистр непериодический или периодичность не доказана"
        )
        for suffix in _SLICE_SUFFIXES:
            add(suffix, available, reason)
    elif obj.kind == "РегистрНакопления":
        available = _balance_register(obj)
        reason = (
            "вид регистра — Остатки"
            if available
            else "таблицы итогов остатков существуют только у регистра вида Остатки"
        )
        for suffix in _BALANCE_ONLY:
            add(suffix, available, reason)
    elif obj.kind == "РегистрБухгалтерии":
        correspondence = obj.props.get("correspondence") is True
        add(
            "ОборотыДтКт",
            correspondence,
            (
                "включена корреспонденция"
                if correspondence
                else "таблица существует только при включенной корреспонденции"
            ),
        )
        chart = bool(obj.props.get("chart_of_accounts"))
        subconto = chart and ext_dimension_count > 0
        add(
            "Субконто",
            subconto,
            (
                f"задан план счетов, максимум субконто: {ext_dimension_count}"
                if subconto
                else "не задан план счетов или у него не доказано количество субконто"
            ),
        )
    elif obj.kind == "РегистрРасчета":
        action_period = obj.props.get("action_period") is True
        add(
            "ФактическийПериодДействия",
            action_period,
            (
                "включен период действия"
                if action_period
                else "период действия выключен; без него таблица не существует"
            ),
        )
        schedule = bool(obj.props.get("schedule"))
        schedule_available = action_period and schedule and bool(schedule_resources)
        if not action_period:
            schedule_reason = "период действия выключен, поэтому график неприменим"
        elif not schedule:
            schedule_reason = "период действия включен, но график не задан"
        elif not schedule_resources:
            schedule_reason = "график задан, но его ресурсы не удалось разрешить"
        else:
            schedule_reason = "включен период действия и разрешен регистр графика"
        add("ДанныеГрафика", schedule_available, schedule_reason)
        if base_registers:
            for base in base_registers:
                suffix = f"База{base.name}"
                plan = base.props.get("chart_of_calculation_types", "")
                add(suffix, True, f"включен базовый период; базовый план: {plan}")
        else:
            add(f"База{_BASE_REGISTER}", False, base_error)
    return result


def _table_from_template(
    obj: MetadataObject,
    template: TableTemplate,
    *,
    suffix: str,
    fields: list[str],
    ext_dimension_count: int,
) -> VirtualTable | None:
    if any(_unknown_placeholder(field_name) for field_name in fields):
        return None
    table = VirtualTable(
        name=f"{obj.full_name}{'.' + suffix if suffix else ''}",
        suffix=suffix,
        description=template.description,
    )
    resource_templates: list[tuple[str, set[int]]] = []
    for field_template in fields:
        if field_template.startswith(_RESOURCE):
            resource_suffix = field_template[len(_RESOURCE) :]
            if (
                obj.kind == "РегистрНакопления"
                and not _balance_register(obj)
                and resource_suffix in _MOVEMENT_SUFFIXES
            ):
                continue
            resources = obj.resources
            if obj.kind == "РегистрБухгалтерии":
                resources = _accounting_fields(
                    resources,
                    resource_suffix,
                    suffix,
                    obj.props.get("correspondence") is True,
                )
            resource_templates.append(
                (resource_suffix, {id(resource) for resource in resources})
            )
            continue
        bucket, names = _expand(
            field_template,
            obj,
            table_suffix=suffix,
            ext_dimension_count=ext_dimension_count,
        )
        getattr(table, bucket).extend(names)

    table.resources = [
        f"{resource.name}{resource_suffix}"
        for resource in obj.resources
        for resource_suffix, allowed in resource_templates
        if id(resource) in allowed
    ]
    return table


def analyze_virtual_tables(
    obj: MetadataObject,
    tables: dict[str, list[TableTemplate]] | None,
    *,
    configuration: Configuration | None = None,
    ext_dimension_count: int | None = None,
    schedule_resources: list[str] | None = None,
) -> VirtualTableReport:
    """Построить таблицы запроса и объяснить платформенные условия.

    `ext_dimension_count` — предел нумерации субконто из плана счетов, по
    которому ведётся регистр бухгалтерии. Ноль означает «плана счетов рядом
    нет»: таблицы с субконто тогда не показываются, потому что назвать их
    поля нечем.

    Условия существования таблиц встроены в код и доступны даже тогда, когда
    справка платформы не содержит полей выбранной версии. Имена полей без
    справки по-прежнему не выдумываются.
    """
    if not obj.kind.startswith("Регистр"):
        return VirtualTableReport()

    if ext_dimension_count is None:
        chart_name = obj.props.get("chart_of_accounts")
        chart = configuration.get(chart_name) if configuration and isinstance(chart_name, str) else None
        ext_dimension_count = (
            _positive_int(chart.props.get("max_ext_dimension_count")) if chart else 0
        )
    if schedule_resources is None:
        schedule_name = obj.props.get("schedule")
        schedule = configuration.get(schedule_name) if configuration and isinstance(schedule_name, str) else None
        schedule_resources = [item.name for item in schedule.resources] if schedule else []

    base_registers, base_error = _base_registers(obj, configuration)
    report = VirtualTableReport(
        availability=_availability(
            obj,
            ext_dimension_count=ext_dimension_count,
            schedule_resources=schedule_resources,
            base_registers=base_registers,
            base_error=base_error,
        )
    )
    if obj.kind == "РегистрБухгалтерии":
        adjustment = _positive_int(obj.props.get("period_adjustment_length"))
        report.notes.append(
            (
                f"`УточнениеПериода` доступно в основной и оборотных таблицах "
                f"(длина уточнения: {adjustment}), но отсутствует в `Остатки`."
                if adjustment
                else "`УточнениеПериода` отсутствует: длина уточнения периода равна нулю."
            )
        )
        unknown = [
            item.name
            for item in [*obj.dimensions, *obj.resources]
            if item.balance is None
        ]
        if unknown:
            report.notes.append(
                "Балансовость не доказана для полей: " + ", ".join(unknown)
                + "; дебетовые, кредитовые и кор-поля для них не перечисляются."
            )

    if not tables:
        return report

    # Один суффикс, два шаблона — так справка описывает регистр бухгалтерии:
    # с поддержкой корреспонденции (`ОстатокДт`/`ОстатокКт`) и без неё.
    # Выбирает признак из выгрузки; без него оба варианта равновероятны и
    # показывать нельзя ни один.
    correspondence = obj.props.get("correspondence")
    variants: dict[str, list[TableTemplate]] = {}
    for template in tables.get(obj.kind, []):
        variants.setdefault(template.suffix, []).append(template)

    availability = {item.suffix: item.available for item in report.availability}
    for suffix, choices in variants.items():
        template = _pick_variant(choices, correspondence)
        if template is None:
            continue

        if _BASE_REGISTER in template.suffix:
            for base in base_registers:
                resolved_suffix = template.suffix.replace(_BASE_REGISTER, base.name)
                fields = _expand_base_resources(
                    template.fields, [item.name for item in base.resources]
                )
                fields = _expand_numbered(fields, ext_dimension_count)
                table = _table_from_template(
                    obj,
                    template,
                    suffix=resolved_suffix,
                    fields=fields,
                    ext_dimension_count=ext_dimension_count,
                )
                if table is not None:
                    report.tables.append(table)
            continue

        # Перерасчеты адресуются собственными именами, которых эта карточка
        # регистра не содержит. Для них по-прежнему ничего не выдумываем.
        if "<" in template.suffix:
            continue

        if suffix in availability and not availability[suffix]:
            continue

        # Чужие плейсхолдеры в полях (`<Имя ресурса графика>ПериодДействия`)
        # означают, что состав таблицы берётся не из этого объекта. Показать
        # его ресурсы под этими именами — та же ошибка, ради которой писался
        # модуль, только наоборот.
        fields = _expand_numbered(template.fields, ext_dimension_count)
        fields = _expand_schedule(fields, schedule_resources)
        if fields is None:
            continue

        if (
            obj.kind == "РегистрНакопления"
            and template.suffix in _BALANCE_ONLY
            and not _balance_register(obj)
        ):
            continue

        if template.suffix in _SLICE_SUFFIXES and not _has_slices(obj):
            continue

        if template.suffix in _ACTION_PERIOD_ONLY and not obj.props.get("action_period"):
            continue

        table = _table_from_template(
            obj,
            template,
            suffix=template.suffix,
            fields=fields,
            ext_dimension_count=ext_dimension_count,
        )
        if table is not None:
            report.tables.append(table)

    return report


def virtual_tables(
    obj: MetadataObject,
    tables: dict[str, list[TableTemplate]] | None,
    *,
    ext_dimension_count: int = 0,
    schedule_resources: list[str] | None = None,
) -> list[VirtualTable]:
    """Совместимый короткий вызов: только фактически доступные таблицы."""
    return analyze_virtual_tables(
        obj,
        tables,
        ext_dimension_count=ext_dimension_count,
        schedule_resources=schedule_resources,
    ).tables
