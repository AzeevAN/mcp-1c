"""Инструменты сервера: чистые функции над реестром.

Здесь нет ни строки, зависящей от MCP. Причина не в чистоте ради чистоты:
эти же функции понадобятся дашборду и отладочному CLI, а протокольный слой
имеет свойство меняться. `server.py` — тонкая обёртка, которая только
регистрирует их и раздаёт описания.

Правило набора инструментов: их мало и список фиксирован. Каждый инструмент
висит в контексте агента постоянно, независимо от того, пользуется он им или
нет. Поэтому инструмент добавляется только под отдельную пользовательскую
задачу, а не под каждый новый внутренний индекс. Код конфигурации — отдельная
область поиска, для которой служит `search_procedures`.
"""

from __future__ import annotations

import difflib
from bisect import bisect_right
from dataclasses import dataclass

from . import replacements
from .bsl_lex import Процедура, прочитать_модуль, разобрать
from .module_address import путь_модуля
from .registry import (
    KIND_EXTENSION,
    STATUS_ERROR,
    LoadedModules,
    Registry,
    RegistryError,
)
from .render import (
    CallerSite,
    DETAIL_LEVELS,
    FIELDS,
    FormHandlerBinding,
    MetadataBinding,
    ProcedureMatch,
    ProcedureOutline,
    render_callers,
    render_module_toc,
    render_object,
    render_procedure_card,
    render_procedure_search,
    render_syntax_item,
)
from .search import FIELD_KIND_TITLES
from .syntax_model import (
    KIND_QUERY_ARTICLE,
    KIND_TITLES,
    QUERY_LANGUAGE_KINDS,
    SyntaxItem,
    parse_version,
    release,
)
from .virtual_tables import virtual_tables

def health(registry: Registry, *, detailed: bool) -> dict:
    """Тело ответа `/health`: живость и состав источников.

    `detailed` — прошёл ли запрос проверку на чтение. Имена конфигураций уже
    сведения о клиенте: кто у него внедрён и как называются доработки, — без
    токена отдаются только счётчики.

    Справка платформы и язык запросов разведены намеренно. Прежде оба жили в
    одном поле, и при загруженном только `shquery_ru.hbk` ответ гласил
    `"syntax_loaded": true, "syntax": ""` — «справка есть, но сломана» вместо
    «справки платформы нет».
    """
    площадки = list(registry.syntax.syntax.platforms) if registry.syntax else []
    тело = {
        "status": "ok",
        "configurations_total": len(registry.configurations),
        "syntax_loaded": bool(площадки),
        "query_language_loaded": registry.query_source is not None,
    }
    if detailed:
        тело["configurations"] = sorted(registry.configurations)
        тело["syntax"] = площадки
    return тело


MAX_LIMIT = 50
# Самая большая статья по языку запросов весит 24,8 КБ — это 2–7 тысяч
# токенов на одну. Десять таких в выдаче поиска из десяти строк съели бы
# контекст агента; по имени статья по-прежнему отдаётся целиком (`get_syntax`).
SNIPPET_CHARS = 400


def _notes_block(context, *, critical_only: bool = True) -> str:
    notes = context.notes(critical_only=critical_only)
    if not notes:
        return ""
    lines = ["", "---", "> **Оговорки по источнику данных**"]
    lines += [f"> - {note}" for note in notes]
    return "\n".join(lines) + "\n"


def _clamp(limit: int) -> int:
    return max(1, min(int(limit or 10), MAX_LIMIT))


# --------------------------------------------------------------- обзор


def list_configurations(registry: Registry) -> str:
    """Какие конфигурации загружены и что по ним доступно."""
    rows = registry.overview()
    if not rows:
        if registry.syntax is not None:
            return _syntax_only_overview(registry)
        return (
            "Не загружено ни одной конфигурации, нет ни справки платформы, ни "
            "справки по языку запросов.\n\n"
            "Выгрузите структуру обработкой из `exporter-1c/` и загрузите архив."
        )

    out = ["# Загруженные конфигурации", ""]
    for row in rows:
        out.append(f"## {row['name']}")
        if row["synonym"]:
            out.append(f"*{row['synonym']}*")
        out.append("")
        out.append(
            f"- Версия: {row['version']} · платформа **{row['platform']}**\n"
            f"- Объектов: {row['objects']}, связей: {row['edges']}"
        )
        providers = row["providers"]
        out.append(f"- Метаданные: да")
        # `providers['syntax']` истинно и когда подключён только язык
        # запросов (`LoadedSyntax` в registry.py собирает их в один объект):
        # у языка запросов платформы нет, `syntax_platform` тогда пуст, а
        # прежний текст на этом месте показывал «справка  — none».
        if providers["syntax"] and row["syntax_platform"]:
            relation = {
                "exact": "версия совпадает с конфигурацией",
                "newer": f"новее конфигурации, скрыто {row['syntax_hidden']} элементов",
                "older": "**старее конфигурации**",
            }.get(row["syntax_relation"], row["syntax_relation"])
            out.append(f"- Синтаксис платформы: справка {row['syntax_platform']} — {relation}")
        else:
            out.append("- Синтаксис платформы: не подключён")
        if registry.query_source is not None:
            out.append(
                f"- Язык запросов: подключён, {registry.query_source.items_total} страниц"
            )
        else:
            out.append("- Язык запросов: не подключён")
        out.append("- Индекс модулей: не подключён")
        for note in row["notes"]:
            out.append(f"- ⚠ {note}")
        out.append("")

    out += _coverage_section(registry)

    if len(rows) > 1:
        out.append(
            "> В запросах указывайте `config` явно — конфигурация по умолчанию "
            "не подставляется."
        )
    return "\n".join(out).rstrip() + "\n"


def _syntax_only_overview(registry: Registry) -> str:
    """Что доступно без единой загруженной конфигурации.

    Справка платформы и язык запросов — самостоятельные источники (см.
    `docs/query-language-design.md`, «Работа без справки платформы»): любой
    из них может быть загружен один, вместе или порознь. Прежний текст
    называл оба случая «справкой платформы» и на одном лишь языке запросов
    печатал пустое имя платформы между парой `**` и «0 элементов» — считал
    по слитому виду одной только платформы, который в этом сценарии пуст.
    """
    доступно = []
    if registry.syntax.syntax.platforms:
        total = f"{len(registry.syntax.syntax):,}".replace(",", "\u00a0")
        доступно.append(
            f"справка платформы **{registry.syntax.source.platform}** "
            f"({total} элементов)"
        )
    if registry.query_source is not None:
        доступно.append(
            f"язык запросов ({registry.query_source.items_total} страниц)"
        )

    return (
        "# Конфигурации не загружены\n\n"
        f"Доступно: {', '.join(доступно)}. Работают `search_syntax` и "
        "`get_syntax`, параметр `config` указывать не нужно.\n\n"
        "Фильтрации по версии платформы нет — выдача содержит всё, что "
        "описано в подключённых справках. Загрузите выгрузку структуры "
        "конфигурации, чтобы лишнее отсекалось автоматически.\n"
    )


def _coverage_section(registry: Registry) -> list[str]:
    """Каких справок не хватает и какие лишние.

    Знает об этом только сервер: он один видит и платформы конфигураций, и
    версии справок. Расхождение в один релиз стоит примерно 10–15 сигнатур и
    35–45 контекстов доступности — молчать об этом нельзя.
    """
    покрытие = registry.syntax_coverage()
    if not покрытие["loaded"] and not покрытие["missing"]:
        return []

    out = ["## Справки платформы", ""]
    if покрытие["loaded"]:
        out.append(f"Загружены: {', '.join(покрытие['loaded'])}.")
    else:
        out.append("Не загружено ни одной справки.")

    for пропуск in покрытие["missing"]:
        конфигурации = ", ".join(пропуск["configurations"])
        # «Собраны из соседних версий» верно, только когда соседние есть. При
        # нуле загруженных справок ответов по платформе нет вовсе, и обещать
        # приблизительные — врать (найдено живой проверкой 2026-08-19).
        чем_отвечаем = (
            "Ответы по ней собраны из соседних версий: наличие элементов "
            "отфильтровано, сигнатуры и доступность могут отличаться."
            if покрытие["loaded"]
            else "Методы и свойства платформы недоступны совсем — загрузите "
            "`shcntx_ru.hbk` этой версии."
        )
        out.append(
            f"- Не хватает справки **{пропуск['platform']}** — на ней работает "
            f"{конфигурации}. {чем_отвечаем}"
        )
    for лишняя in покрытие["unused"]:
        out.append(
            f"- Справка **{лишняя}** не используется: конфигураций на этой "
            "платформе нет."
        )
    out.append("")
    return out


# --------------------------------------------------------------- метаданные


def search_objects(
    registry: Registry,
    query: str,
    config: str | None = None,
    kind: str | None = None,
    limit: int = 10,
) -> str:
    """Найти объект конфигурации по описанию или части имени."""
    context = registry.resolve(config)
    kinds = [kind] if kind else None
    hits = context.configuration.index.search(query, limit=_clamp(limit), kinds=kinds)

    if not hits:
        return (
            f"По запросу «{query}» в конфигурации {context.name} ничего не найдено."
            + _notes_block(context)
        )

    out = [f"# Найдено в {context.name}: «{query}»", ""]
    for hit in hits:
        obj = hit.doc.payload
        title = f" — {obj.synonym}" if obj.synonym else ""
        out.append(f"- `{obj.full_name}`{title}")
        summary = []
        if obj.attributes:
            summary.append(f"реквизитов {len(obj.attributes)}")
        if obj.tabular_parts:
            summary.append(f"ТЧ {len(obj.tabular_parts)}")
        if obj.movements:
            summary.append(f"движений {len(obj.movements)}")
        if summary:
            out.append(f"  {', '.join(summary)}")

    out += _fields_section(context, query)
    return "\n".join(out) + "\n" + _notes_block(context)


def _fields_section(context, query: str, limit: int = 5) -> list[str]:
    """Совпадения в реквизитах объектов.

    Отдельным разделом, потому что отвечает на другой вопрос: не «какой объект
    мне нужен», а «где хранится это значение». Запрос «номер телефона
    контрагента» описывает поле, а не объект, и по объектам не находится вовсе.
    """
    hits = context.configuration.field_index.search(query, limit=limit)
    if not hits:
        return []

    out = ["", f"## Найдено в реквизитах ({len(hits)})", ""]
    for hit in hits:
        ref = hit.doc.payload
        item = ref.field
        title = item.synonym or item.name
        out.append(f"- `{ref.full_name}` — {title}")
        out.append(
            f"  {item.type_spec()} · "
            f"{FIELD_KIND_TITLES.get(ref.kind, ref.kind)} объекта {ref.object_title}"
        )
    return out


def get_object(
    registry: Registry,
    full_name: str,
    config: str | None = None,
    detail: str = FIELDS,
) -> str:
    """Структура объекта: реквизиты, табличные части, движения, связи."""
    if detail not in DETAIL_LEVELS:
        detail = FIELDS

    context = registry.resolve(config)
    obj = context.configuration.config.get(full_name)

    if obj is None:
        hits = context.configuration.index.search(full_name, limit=5)
        suggestion = "\n".join(f"- `{h.doc.id}`" for h in hits)
        return (
            f"В конфигурации {context.name} нет объекта `{full_name}`.\n\n"
            + (f"Возможно, имелось в виду:\n{suggestion}\n" if suggestion else "")
            + _notes_block(context)
        )

    # Виртуальные таблицы собираются здесь, а не в рендере: они соединяют
    # метаданные конфигурации со справкой платформы, а рендер про справку
    # ничего не знает и знать не должен.
    # Предел нумерации субконто — свойство плана счетов, а спрашивают про
    # регистр бухгалтерии. Без него поля вида `Субконто1` назвать нечем.
    chart = context.configuration.config.get(str(obj.props.get("chart_of_accounts", "")))
    ext_dimensions = chart.props.get("max_ext_dimension_count", 0) if chart else 0

    # `ДанныеГрафика` описывает ресурсы графика — отдельного регистра сведений,
    # а не самого регистра расчёта.
    schedule = context.configuration.config.get(str(obj.props.get("schedule", "")))

    tables = virtual_tables(
        obj,
        context.syntax.tables if context.syntax else None,
        ext_dimension_count=ext_dimensions if isinstance(ext_dimensions, int) else 0,
        schedule_resources=[f.name for f in schedule.resources] if schedule else None,
    )

    body = render_object(
        obj, detail, graph=context.configuration.graph, virtual_tables=tables
    )
    return body + _notes_block(context)


def get_related(
    registry: Registry,
    full_name: str,
    config: str | None = None,
    limit: int = 40,
) -> str:
    """Что задевает задача: движения, ссылки, зависимости объекта.

    Только прямые связи, и это решение, а не недоделка. Раздел «в радиусе N»
    здесь был и снят 2026-08-18: он обещал соседей соседей, а отдавал
    продолжение списка прямых соседей — обход упирался в свой лимит в 200
    объектов раньше, чем делал второй шаг (у `Справочник.Номенклатура` одних
    прямых соседей 264). Замер: 199 строк раздела, все до одной с расстоянием
    1, ценой 3 000 лишних токенов на вызов.

    Поднимать лимит смысла нет: на двух шагах достаётся тысяча объектов, на
    трёх — 1 700 из 5 637, то есть треть конфигурации. Список имён без
    объяснения, через что идёт связь, бесполезен и показанный целиком —
    информацию несёт ребро, а не узел. Отвечать «через что» должны пути, а не
    окрестность; разбор и условия возврата — в «Отложено».
    """
    context = registry.resolve(config)
    graph = context.configuration.graph

    if full_name not in context.configuration.config.objects:
        return f"В конфигурации {context.name} нет объекта `{full_name}`." + _notes_block(context)

    out = [f"# Связи `{full_name}` в {context.name}", ""]

    outgoing = graph.outgoing(full_name, include_weak=False)
    if outgoing:
        out.append(f"## Ссылается на ({len(outgoing)})")
        out.append("")
        for edge in outgoing[: _clamp(limit)]:
            out.append(f"- `{edge.target}` — {edge.title}")
        if len(outgoing) > limit:
            out.append(f"- … ещё {len(outgoing) - limit}")
        out.append("")

    incoming = graph.incoming(full_name)
    if incoming:
        out.append(f"## Ссылаются на него ({len(incoming)})")
        out.append("")
        for edge in incoming[: _clamp(limit)]:
            out.append(f"- `{edge.source}` — {edge.title}")
        if len(incoming) > limit:
            out.append(f"- … ещё {len(incoming) - limit}")
        out.append("")

    return "\n".join(out) + "\n" + _notes_block(context)


def compare_configurations(
    registry: Registry,
    full_name: str,
    configs: list[str] | None = None,
) -> str:
    """Один и тот же объект в разных конфигурациях — что различается."""
    names = list(configs) if configs else sorted(registry.configurations)
    if len(names) < 2:
        return "Для сравнения нужно минимум две загруженные конфигурации."

    out = [f"# Сравнение `{full_name}`", ""]
    found: dict[str, object] = {}

    for name in names:
        context = registry.resolve(name)
        obj = context.configuration.config.get(full_name)
        if obj is None:
            out.append(f"- **{name}** — объекта нет")
            continue
        found[name] = obj
        out.append(
            f"- **{name}** — реквизитов {len(obj.attributes)}, "
            f"ТЧ {len(obj.tabular_parts)}, движений {len(obj.movements)}"
        )
    out.append("")

    if len(found) < 2:
        return "\n".join(out) + "\n"

    names = list(found)
    left, right = found[names[0]], found[names[1]]
    left_attrs = {a.name for a in left.attributes}
    right_attrs = {a.name for a in right.attributes}

    only_left = sorted(left_attrs - right_attrs)
    only_right = sorted(right_attrs - left_attrs)

    if only_left:
        out.append(f"## Реквизиты только в {names[0]} ({len(only_left)})")
        out.append("")
        out += [f"- `{n}`" for n in only_left[:40]]
        out.append("")
    if only_right:
        out.append(f"## Реквизиты только в {names[1]} ({len(only_right)})")
        out.append("")
        out += [f"- `{n}`" for n in only_right[:40]]
        out.append("")
    if not only_left and not only_right:
        out.append("Состав реквизитов совпадает.")

    return "\n".join(out) + "\n"


# --------------------------------------------------------------- код модулей


def _procedure_limit(limit: int) -> int:
    """У поиска по коду предел — часть контракта, не молчаливый clamp."""
    if (
        isinstance(limit, bool)
        or not isinstance(limit, int)
        or not 1 <= limit <= MAX_LIMIT
    ):
        raise RegistryError(f"limit должен быть целым числом от 1 до {MAX_LIMIT}.")
    return limit


def _selected_modules(
    registry: Registry,
    config: str | None,
    extension: str | None,
) -> tuple[object, LoadedModules | None]:
    context = registry.resolve(config, extension=extension)
    if extension is None:
        return context, context.modules
    if context.extension is not None:
        return context, context.extension

    prefix = f"{context.name}:ext:"
    with registry._lock:
        доступные = sorted(
            source.id[len(prefix):]
            for source in registry.sources.values()
            if source.kind == KIND_EXTENSION and source.id.startswith(prefix)
        )
    хвост = (
        f" Доступны: {', '.join(доступные)}."
        if доступные
        else " Загруженных расширений нет."
    )
    raise RegistryError(
        f"Расширение {extension} с кодом для конфигурации {context.name} "
        f"не загружено.{хвост}"
    )


def _scope_modules(loaded: LoadedModules, scope: str | None) -> frozenset[str]:
    if scope is None:
        return frozenset()
    область = scope.strip()
    if not область or "::" in область:
        raise RegistryError(
            "Область поиска scope должна быть именем объекта или точным "
            "адресом модуля без имени процедуры."
        )

    модули = loaded.оглавление.модули
    низкое = область.casefold()
    точные = [модуль for модуль in модули if модуль.casefold() == низкое]
    if точные:
        return frozenset(точные)

    начало = низкое + "."
    объектные = [
        модуль for модуль in модули if модуль.casefold().startswith(начало)
    ]
    if объектные:
        return frozenset(объектные)
    raise RegistryError(
        f"Область поиска `{scope}` не найдена в загруженном коде. "
        "Укажите имя объекта метаданных или точный адрес модуля из выдачи."
    )


def _отрезок_процедуры(
    текст: str,
    *,
    начало_строка: int,
    начало_столбец: int,
    конец_строка: int,
    конец_столбец: int,
) -> list[str]:
    """Физические строки между точными позициями одноразового разбора."""
    строки = текст.split("\n")
    начало = начало_строка - 1
    конец = конец_строка - 1
    if not (0 <= начало <= конец < len(строки)):
        raise _SignatureError
    if начало == конец:
        return [строки[начало][начало_столбец:конец_столбец]]
    return [
        строки[начало][начало_столбец:],
        *строки[начало + 1:конец],
        строки[конец][:конец_столбец],
    ]


def _сигнатура_из_текста(текст: str, процедура: Процедура) -> str:
    """Декларация точного вхождения из уже дочитанного снимка модуля."""
    части = _отрезок_процедуры(
        текст,
        начало_строка=процедура.строка,
        начало_столбец=процедура.начало_столбец,
        конец_строка=процедура.конец_сигнатуры_строка,
        конец_столбец=процедура.конец_сигнатуры_столбец,
    )
    return " ".join(часть.strip() for часть in части if часть.strip())


def _сигнатура(
    loaded: LoadedModules,
    запись,
    снимки: dict[str, tuple[str, dict[int, Процедура]]] | None = None,
) -> str:
    """Дочитывает только декларацию по сохранённому номеру строки.

    В `LoadedModules` сигнатуры и тела не появляются: текст существует лишь
    на время одного ответа. Снимок позволяет не читать и не разбирать один
    файл по разу на каждую строку оглавления.
    """
    if снимки is None:
        снимки = {}
    снимок = снимки.get(запись.модуль)
    if снимок is None:
        путь = loaded.корень / путь_модуля(запись.модуль)
        текст = прочитать_модуль(путь)
        разбор = _parsed_procedures(
            loaded.оглавление.модуля(запись.модуль), текст
        )
        снимки[запись.модуль] = (текст, разбор)
    else:
        текст, разбор = снимок
    return _сигнатура_из_текста(
        текст, _parsed_procedure(разбор, запись)
    )


class _SignatureError(Exception):
    """Граница декларации не совпала с текущим файлом модуля."""


class _StaleModules(Exception):
    """LoadedModules сменился, пока инструмент читал canonical root."""


def _modules_are_current(registry: Registry, loaded: LoadedModules) -> bool:
    """Короткий CAS без дискового I/O под замком реестра."""
    with registry._lock:
        return registry.modules.get(loaded.source.id) is loaded


def _modules_state_snapshot(
    registry: Registry,
    loaded: LoadedModules,
) -> tuple[
    bool,
    str,
    str,
    tuple[int, int],
    str,
    tuple[int, int],
]:
    """Снимок готовности и прогресса одного поколения.

    Фоновый поток меняет эти поля последовательными
    присваиваниями под `_lock`. Читатель берёт их под тем же замком,
    иначе видимы невозможные сочетания вроде «этап 2,
    оглавление» или `status=error` без уже записанной причины. Диск под
    замком не читается.
    """
    with registry._lock:
        if registry.modules.get(loaded.source.id) is not loaded:
            raise _StaleModules
        return (
            loaded.готов,
            loaded.source.status,
            loaded.source.error,
            loaded.этап,
            loaded.название_этапа,
            loaded.прогресс,
        )


def _modules_availability_message(
    registry: Registry,
    context,
    loaded: LoadedModules | None,
) -> str | None:
    """Одинаковый контракт состояния для всех инструментов по коду."""
    if loaded is None:
        return (
            f"Для конфигурации {context.name} выгрузка в файлы не загружена. "
            "Инструменты про код ответить не могут."
        )
    готов, status, error, этап, название_этапа, прогресс = (
        _modules_state_snapshot(registry, loaded)
    )
    if готов:
        return None
    if status == STATUS_ERROR:
        причина = error or "причина не записана"
        return f"Индекс кода не построен: {причина}"
    номер_этапа, этапов = этап
    обработано, всего = прогресс
    return (
        f"Индекс кода строится: этап {номер_этапа}/{этапов} "
        f"«{название_этапа}», обработано {обработано} из {всего} "
        "элементов этапа. Ответы про код пока недоступны."
    )


def _procedure_matches(
    loaded: LoadedModules,
    записи: list,
    снимки: dict[str, tuple[str, dict[int, Процедура]]] | None = None,
) -> list[ProcedureMatch]:
    счётчики: dict[str, tuple[dict[str, int], int]] = {}
    результат: list[ProcedureMatch] = []
    for запись in записи:
        ключ_имени = запись.имя.casefold()
        сведения = счётчики.get(ключ_имени)
        if сведения is None:
            по_модулям = {}
            неразрешённых = 0
            for место in loaded.вызовы.места(запись.имя):
                if место.цель is None:
                    неразрешённых += 1
                else:
                    по_модулям[место.цель] = по_модулям.get(место.цель, 0) + 1
            сведения = по_модулям, неразрешённых
            счётчики[ключ_имени] = сведения
        по_модулям, неразрешённых = сведения
        результат.append(
            ProcedureMatch(
                address=f"{запись.модуль}::{запись.имя}",
                signature=_сигнатура(loaded, запись, снимки),
                exported=запись.экспорт,
                function=запись.функция,
                line=запись.строка,
                calls=по_модулям.get(запись.модуль, 0),
                unresolved_calls=неразрешённых,
                annotated=запись.перекрыта,
            )
        )
    return результат


def _search_procedures_once(
    registry: Registry,
    query: str,
    config: str | None = None,
    extension: str | None = None,
    scope: str | None = None,
    limit: int = 10,
) -> str:
    """Точное имя и поиск словами по коду конфигурации или расширения."""
    limit = _procedure_limit(limit)
    запрос = query.strip()
    if not запрос:
        raise RegistryError("Запрос query не может быть пустым.")

    context, loaded = _selected_modules(registry, config, extension)
    состояние = _modules_availability_message(registry, context, loaded)
    if состояние is not None:
        return состояние
    if any(
        индекс is None
        for индекс in (loaded.оглавление, loaded.вызовы, loaded.поиск)
    ):
        raise RegistryError("Готовый индекс кода неполон; перезагрузите источник.")

    приоритетные = _scope_modules(loaded, scope)
    точные_все = loaded.оглавление.по_имени(запрос)

    def категория(запись) -> int:
        if приоритетные and запись.модуль in приоритетные:
            return 0
        сдвиг = 1 if приоритетные else 0
        if запись.модуль.startswith("ОбщийМодуль."):
            return сдвиг
        return сдвиг + 1

    точные_все = sorted(точные_все, key=категория)
    точные = точные_все[:limit]
    точные_ключи = {
        (запись.модуль.casefold(), запись.имя.casefold())
        for запись in точные_все
    }

    def не_точная(doc) -> bool:
        запись = doc.payload
        return (запись.модуль.casefold(), запись.имя.casefold()) not in точные_ключи

    # Категории ищутся отдельно: scope не может потеряться за глобальным
    # top-N, а ради счётчика не материализуются десятки тысяч Hit. Берётся
    # ровно limit+1 — последний нужен только для честного «есть ещё».
    hits = []
    категорий = 3 if приоритетные else 2
    for номер_категории in range(категорий):
        осталось = limit + 1 - len(hits)
        if осталось <= 0:
            break
        hits.extend(
            loaded.поиск.search(
                запрос,
                limit=осталось,
                predicate=lambda doc, номер=номер_категории: (
                    не_точная(doc) and категория(doc.payload) == номер
                ),
            )
        )

    слова_все = [hit.doc.payload for hit in hits]
    слова = слова_все[:limit]
    if not точные and not слова:
        выбранное = (
            f"расширении {extension} конфигурации {context.name}"
            if extension
            else f"конфигурации {context.name}"
        )
        return (
            f"По запросу «{query}» в {выбранное} процедур не найдено. "
            "Поиск по словам выполняется только по экспортным процедурам; "
            "неэкспортная находится по точному имени. Если известен точный "
            "адрес, используйте `get_procedure(address=\"Модуль::Имя\")`."
        )

    try:
        снимки: dict[str, tuple[str, dict[int, Процедура]]] = {}
        точные_совпадения = _procedure_matches(loaded, точные, снимки)
        словесные_совпадения = _procedure_matches(loaded, слова, снимки)
    except (OSError, _SignatureError) as ошибка:
        # Canonical root мог смениться или исчезнуть между resolve и чтением.
        # Сначала identity CAS: ошибка старого поколения — повод повторить,
        # а не показывать путь или файловую причину пользователю.
        if not _modules_are_current(registry, loaded):
            raise _StaleModules from ошибка
        raise RegistryError(
            "Не удалось прочитать сигнатуру из текущей выгрузки кода: "
            "файл модуля недоступен."
        ) from ошибка

    # Последняя проверка непосредственно перед render: оглавление, вызовы,
    # номера строк и прочитанные сигнатуры обязаны принадлежать одному
    # объекту LoadedModules. Дискового I/O под `_lock` здесь нет.
    if not _modules_are_current(registry, loaded):
        raise _StaleModules

    return render_procedure_search(
        context.name,
        query,
        exact=точные_совпадения,
        exact_total=len(точные_все),
        exact_more_modules=len(
            {запись.модуль for запись in точные_все[len(точные):]}
        ),
        words=словесные_совпадения,
        words_more=len(слова_все) > limit,
        limit=limit,
        extension=extension,
    )


def search_procedures(
    registry: Registry,
    query: str,
    config: str | None = None,
    extension: str | None = None,
    scope: str | None = None,
    limit: int = 10,
) -> str:
    """Поиск с одним полным повтором при смене поколения кода."""
    for _ in range(2):
        try:
            return _search_procedures_once(
                registry, query, config, extension, scope, limit
            )
        except _StaleModules:
            continue
    raise RegistryError(
        "Код изменился во время поиска дважды; повторите запрос после "
        "завершения загрузки."
    )


def _procedure_window(start_line: int, lines: int) -> tuple[int, int]:
    """Строгая граница окна: ошибка вызова не маскируется обрезкой."""
    if (
        isinstance(start_line, bool)
        or not isinstance(start_line, int)
        or start_line < 0
    ):
        raise RegistryError("start_line должен быть целым числом от 0.")
    if (
        isinstance(lines, bool)
        or not isinstance(lines, int)
        or not 1 <= lines <= 200
    ):
        raise RegistryError("lines должен быть целым числом от 1 до 200.")
    return start_line, lines


def _modules_package_is_current(
    registry: Registry, loaded_modules: list[LoadedModules]
) -> bool:
    """Один CAS для всех корпусов, чьи части попадут в один ответ."""
    уникальные = {item.source.id: item for item in loaded_modules}
    with registry._lock:
        return all(
            registry.modules.get(source_id) is item
            for source_id, item in уникальные.items()
        )


def _read_module_snapshot(
    registry: Registry,
    loaded: LoadedModules,
    module: str,
) -> str:
    """Текст одного поколения, без раскрытия локального пути при отказе."""
    путь = loaded.корень / путь_модуля(module)
    try:
        текст = прочитать_модуль(путь)
    except OSError as error:
        if not _modules_are_current(registry, loaded):
            raise _StaleModules from error
        raise RegistryError(
            "Не удалось прочитать текущий файл модуля: файл недоступен."
        ) from error
    if not _modules_are_current(registry, loaded):
        raise _StaleModules
    return текст


def _parsed_procedures(
    записи: list, текст: str
) -> dict[int, Процедура]:
    """Сопоставляет оглавление с последовательностью одноразового разбора."""
    процедуры = разобрать(текст)
    if len(записи) != len(процедуры):
        raise _SignatureError
    результат: dict[int, Процедура] = {}
    for запись, процедура in zip(записи, процедуры, strict=True):
        if (
            запись.строка != процедура.строка
            or запись.имя.casefold() != процедура.имя.casefold()
        ):
            raise _SignatureError
        результат[запись.позиция] = процедура
    return результат


def _parsed_procedure(разбор: dict[int, Процедура], запись) -> Процедура:
    try:
        return разбор[запись.позиция]
    except KeyError as error:
        raise _SignatureError from error


def _procedure_body(текст: str, процедура: Процедура) -> list[str]:
    """Физические строки точного вхождения без соседей на граничных строках."""
    строки = текст.split("\n")
    конец_строка = процедура.конец or len(строки)
    return _отрезок_процедуры(
        текст,
        начало_строка=процедура.строка,
        начало_столбец=процедура.начало_столбец,
        конец_строка=конец_строка,
        конец_столбец=процедура.конец_столбец,
    )


def _extension_delta(текст: str, процедура: Процедура) -> list[str]:
    """Блоки правки дословно, включая сами граничные директивы."""
    результат: list[str] = []
    внутри: str | None = None
    концы = {
        "#удаление": "#конецудаления",
        "#вставка": "#конецвставки",
    }
    for строка in _procedure_body(текст, процедура):
        голая = строка.strip().casefold()
        if внутри is None and голая in концы:
            внутри = концы[голая]
        if внутри is not None:
            результат.append(строка)
            if голая == внутри:
                внутри = None
    return результат


_MODULE_CONTEXT_TITLES = {
    "global": "Глобальный",
    "server": "Сервер",
    "client_managed": "Клиент (управляемое приложение)",
    "server_call": "Вызов сервера",
    "privileged": "Привилегированный",
}


def _compilation_context(context, module: str, parsed) -> list[str]:
    результат: list[str] = []
    if parsed.директива:
        результат.append(f"&{parsed.директива}")
    if module.startswith("ОбщийМодуль.") and context.configuration is not None:
        объект = context.configuration.config.get(module)
        if объект is not None:
            for ключ, подпись in _MODULE_CONTEXT_TITLES.items():
                if ключ in объект.props:
                    значение = "да" if объект.props[ключ] is True else "нет"
                    результат.append(f"{подпись}: {значение}")
    return результат


def _module_warnings(context, loaded: LoadedModules, записи: list) -> list[str]:
    warnings: list[str] = []
    частичные = [запись for запись in записи if запись.частичный]
    if частичные:
        первая = min(item.строка for item in частичные)
        warnings.append(
            f"Модуль разобран не до конца: с процедуры на строке {первая} "
            "граница конца не найдена; оглавление может быть неполным."
        )
    if (
        loaded.source.kind != KIND_EXTENSION
        and context.configuration is not None
        and loaded.версия_кода
        and context.configuration.config.version
        and loaded.версия_кода != context.configuration.config.version
    ):
        warnings.append(
            f"Код модулей выгружен для версии {loaded.версия_кода}, "
            f"загруженные метаданные — версии {context.configuration.config.version}. "
            "Строить правку на этом ответе нельзя без сверки."
        )
    return warnings


def _similar_address(loaded: LoadedModules, module: str, name: str | None) -> list[str]:
    if name is None:
        return difflib.get_close_matches(module, loaded.оглавление.модули, n=5, cutoff=0.45)
    кандидаты = [
        f"{module}::{item.имя}" for item in loaded.оглавление.модуля(module)
    ]
    return difflib.get_close_matches(f"{module}::{name}", кандидаты, n=5, cutoff=0.45)


def _foreign_extension_warnings(
    registry: Registry,
    configuration: str,
    module: str,
    target_name: str,
    selected: LoadedModules,
    observed: list[LoadedModules],
) -> list[str]:
    prefix = f"{configuration}:ext:"
    with registry._lock:
        кандидаты = sorted(
            (
                (source_id[len(prefix):], loaded)
                for source_id, loaded in registry.modules.items()
                if source_id.startswith(prefix) and loaded is not selected
            ),
            key=lambda item: item[0],
        )
    warnings: list[str] = []
    for extension_name, foreign in кандидаты:
        try:
            готов, *_ = _modules_state_snapshot(registry, foreign)
        except _StaleModules:
            continue
        if not готов:
            continue
        записи = foreign.оглавление.модуля(module)
        if not записи:
            continue
        текст = _read_module_snapshot(registry, foreign, module)
        observed.append(foreign)
        разбор = _parsed_procedures(записи, текст)
        for запись in записи:
            parsed = _parsed_procedure(разбор, запись)
            if not parsed.перекрытие:
                continue
            вид, цель = parsed.перекрытие
            if (цель or parsed.имя).casefold() != target_name.casefold():
                continue
            вид_низкое = вид.casefold()
            if вид_низкое in ("вместо", "around"):
                warnings.append(
                    f"Процедуру уже перекрывает расширение `{extension_name}` "
                    f"аннотацией `&{вид}`. Какое расширение выиграет, зависит от "
                    "порядка расширений; текст чужого расширения не показан."
                )
            elif вид_низкое in (
                "изменениеиконтроль",
                "changeandvalidate",
            ):
                warnings.append(
                    f"Расширение `{extension_name}` аннотацией `&{вид}` "
                    "меняет типовое тело блоками вставки/удаления; текст "
                    "чужого расширения не показан."
                )
            else:
                warnings.append(
                    f"Расширение `{extension_name}` добавляет `&{вид}` для этой "
                    "процедуры; его код тоже выполняется, но текст чужого расширения "
                    "не показан."
                )
    return warnings


def _get_procedure_once(
    registry: Registry,
    address: str,
    config: str | None,
    extension: str | None,
    start_line: int,
    lines: int,
) -> str:
    context, loaded = _selected_modules(registry, config, extension)
    состояние = _modules_availability_message(registry, context, loaded)
    if состояние is not None:
        return состояние
    if any(
        индекс is None
        for индекс in (loaded.оглавление, loaded.вызовы, loaded.формы)
    ):
        raise RegistryError("Готовый индекс кода неполон; перезагрузите источник.")

    модуль, разделитель, имя = address.partition("::")
    модуль = модуль.strip()
    имя = имя.strip()
    if not модуль or (разделитель and (not имя or "::" in имя)):
        raise RegistryError(
            "address должен быть адресом модуля или парой `Модуль::Имя`."
        )
    канонический_модуль = next(
        (
            item
            for item in loaded.оглавление.модули
            if item.casefold() == модуль.casefold()
        ),
        None,
    )
    if канонический_модуль is None:
        похожие = _similar_address(loaded, модуль, None)
        хвост = "" if not похожие else "\n\nВозможно, имелось в виду:\n" + "\n".join(f"- `{item}`" for item in похожие)
        return f"Модуль `{модуль}` в загруженном коде не найден.{хвост}\n"
    модуль = канонический_модуль
    записи = loaded.оглавление.модуля(модуль)

    текст = _read_module_snapshot(registry, loaded, модуль)
    разбор = _parsed_procedures(записи, текст)
    observed = [loaded]
    warnings = _module_warnings(context, loaded, записи)

    if not разделитель:
        outlines: list[ProcedureOutline] = []
        for запись in записи:
            parsed = _parsed_procedure(разбор, запись)
            calls = sum(
                1
                for место in loaded.вызовы.места(запись.имя)
                if место.цель == модуль
            )
            outlines.append(
                ProcedureOutline(
                    address=f"{модуль}::{запись.имя}",
                    signature=_сигнатура_из_текста(текст, parsed),
                    exported=запись.экспорт,
                    function=запись.функция,
                    line=запись.строка,
                    calls=calls,
                    directive=parsed.директива,
                    events=loaded.формы.обработчик(модуль, запись.имя) or (),
                )
            )
        if not _modules_package_is_current(registry, observed):
            raise _StaleModules
        return render_module_toc(
            context.name, модуль, outlines, warnings=warnings, extension=extension
        )

    совпадения = [запись for запись in записи if запись.имя.casefold() == имя.casefold()]
    if not совпадения:
        похожие = _similar_address(loaded, модуль, имя)
        хвост = "" if not похожие else "\n\nВозможно, имелось в виду:\n" + "\n".join(f"- `{item}`" for item in похожие)
        return f"В модуле `{модуль}` нет процедуры `{имя}`.{хвост}\n"
    запись = совпадения[0]
    parsed = _parsed_procedure(разбор, запись)
    body = _procedure_body(текст, parsed)
    target_name = (
        parsed.перекрытие[1] or parsed.имя
        if parsed.перекрытие
        else parsed.имя
    )

    if extension and parsed.перекрытие and parsed.перекрытие[0].casefold() in (
        "изменениеиконтроль",
        "changeandvalidate",
    ):
        base = context.modules
        base_state = _modules_availability_message(registry, context, base)
        if base_state is not None:
            return base_state
        base_records = [
            item
            for item in base.оглавление.модуля(модуль)
            if item.имя.casefold() == target_name.casefold()
        ]
        if not base_records:
            raise RegistryError(
                f"Аннотация `&{parsed.перекрытие[0]}` ссылается на "
                f"`{модуль}::{target_name}`, но в коде основной конфигурации её нет."
            )
        base_text = _read_module_snapshot(registry, base, модуль)
        observed.append(base)
        base_parsed = _parsed_procedures(
            base.оглавление.модуля(модуль), base_text
        )
        body = [
            "// Тело основной конфигурации",
            *_procedure_body(
                base_text, _parsed_procedure(base_parsed, base_records[0])
            ),
            "",
            f"// Дельта расширения {extension}",
            *_extension_delta(текст, parsed),
        ]
    elif extension and parsed.перекрытие and parsed.перекрытие[0].casefold() in ("вместо", "around"):
        warnings.append(
            "Показано тело `&Вместо`; типовое тело читайте отдельным запросом "
            "к основной конфигурации без `extension`."
        )

    warnings.extend(
        _foreign_extension_warnings(
            registry, context.name, модуль, target_name, loaded, observed
        )
    )
    events = loaded.формы.обработчик(модуль, запись.имя) or ()
    compilation = _compilation_context(context, модуль, parsed)
    if events:
        compilation.append("события формы: " + ", ".join(events))
    if not _modules_package_is_current(registry, observed):
        raise _StaleModules
    return render_procedure_card(
        context.name,
        f"{модуль}::{запись.имя}",
        signature=_сигнатура_из_текста(текст, parsed),
        compilation=compilation,
        body=body,
        start_line=start_line,
        lines=lines,
        warnings=warnings,
        annotation=parsed.перекрытие,
        extension=extension,
    )


def get_procedure(
    registry: Registry,
    address: str,
    config: str | None = None,
    extension: str | None = None,
    start_line: int = 0,
    lines: int = 200,
) -> str:
    """Оглавление модуля или карточка процедуры с дисковым телом."""
    start_line, lines = _procedure_window(start_line, lines)
    if not isinstance(address, str) or not address.strip():
        raise RegistryError("address не может быть пустым.")
    for _ in range(2):
        try:
            return _get_procedure_once(
                registry, address, config, extension, start_line, lines
            )
        except _StaleModules:
            continue
        except _SignatureError as error:
            raise RegistryError(
                "Не удалось сопоставить оглавление с текущим текстом модуля; "
                "перезагрузите источник кода."
            ) from error
    raise RegistryError(
        "Код изменился во время чтения дважды; повторите запрос после "
        "завершения загрузки."
    )


_AMBIGUOUS_OWNER = object()


@dataclass(slots=True)
class _OwnerBoundaries:
    начала: list[int]
    состояния: list[object | None]
    частичные: list[int]


def _build_owner_boundaries(records: list) -> _OwnerBoundaries:
    """Кусочно-постоянное состояние владельца для двоичного поиска.

    Перекрытия сворачиваются один раз при подготовке. Запрос по строке после
    этого не идёт назад по тысячам подходящих процедур: одно состояние явно
    говорит, что владелец единственный, отсутствует или неоднозначен.
    """
    записи = sorted(records, key=lambda item: (item.строка, item.позиция))
    частичные = sorted(item.строка for item in записи if item.частичный)
    события: dict[int, tuple[list[int], list[int]]] = {}
    for номер, запись in enumerate(записи):
        конец = запись.конец
        if not конец:
            continue
        начала, удаления = события.setdefault(запись.строка, ([], []))
        начала.append(номер)
        начала, удаления = события.setdefault(конец + 1, ([], []))
        удаления.append(номер)

    координаты: list[int] = []
    состояния: list[object | None] = []
    активные: set[int] = set()
    for строка in sorted(события):
        добавления, удаления = события[строка]
        активные.difference_update(удаления)
        активные.update(добавления)
        координаты.append(строка)
        if len(активные) == 1:
            состояния.append(записи[next(iter(активные))])
        elif активные:
            состояния.append(_AMBIGUOUS_OWNER)
        else:
            состояния.append(None)
    return _OwnerBoundaries(координаты, состояния, частичные)


def _caller_boundaries(
    loaded: LoadedModules, modules: set[str]
) -> dict[str, _OwnerBoundaries]:
    """Интервальные состояния модулей для O(log P) поиска владельца."""
    return {
        module: _build_owner_boundaries(loaded.оглавление.модуля(module))
        for module in modules
    }


def _caller_site(boundaries, место) -> CallerSite:
    """Владелец места по непересекающимся строковым границам оглавления.

    Вызов хранит только номер строки. Если на одной физической строке лежат
    две процедуры, выбирать первую или последнюю было бы догадкой: обе
    границы подходят. Частичная процедура тоже не даёт правой границы.
    """
    границы = boundaries[место.модуль]
    позиция = bisect_right(границы.начала, место.строка) - 1
    состояние = границы.состояния[позиция] if позиция >= 0 else None
    if состояние is not None and состояние is not _AMBIGUOUS_OWNER:
        запись = состояние
        return CallerSite(
            module=место.модуль,
            line=место.строка,
            owner=f"{место.модуль}::{запись.имя}",
        )
    if состояние is _AMBIGUOUS_OWNER:
        return CallerSite(
            module=место.модуль,
            line=место.строка,
            owner=None,
            ambiguous_owner=True,
        )

    return CallerSite(
        module=место.модуль,
        line=место.строка,
        owner=None,
        partial_owner=bisect_right(границы.частичные, место.строка) > 0,
    )


def _metadata_bindings(context, module: str, name: str) -> list[MetadataBinding]:
    if context.configuration is None:
        return []
    совпадения = [
        MetadataBinding(kind=edge.kind, source=edge.source)
        for edge in context.configuration.graph.edges
        if edge.kind in ("handler", "method")
        and edge.target.casefold() == module.casefold()
        and edge.via.casefold() == name.casefold()
    ]
    return sorted(
        совпадения,
        key=lambda item: (item.kind, item.source.casefold(), item.source),
    )


def _form_bindings(loaded: LoadedModules, module: str, name: str):
    lower_module = module.casefold()
    if ".форма." not in lower_module and not lower_module.startswith("общаяформа."):
        return [], "not_form"
    форма = loaded.формы.состав(module)
    if форма is None:
        return [], "missing"
    if форма.битая:
        return [], "broken"
    привязки = [
        FormHandlerBinding(element=item.элемент, event=item.событие)
        for item in loaded.формы.привязки(module, name)
    ]
    return привязки, "ready"


def _get_callers_once(
    registry: Registry,
    address: str,
    config: str | None,
    extension: str | None,
    limit: int,
) -> str:
    context, loaded = _selected_modules(registry, config, extension)
    состояние = _modules_availability_message(registry, context, loaded)
    if состояние is not None:
        return состояние
    if any(
        индекс is None
        for индекс in (loaded.оглавление, loaded.вызовы, loaded.формы)
    ):
        raise RegistryError("Готовый индекс кода неполон; перезагрузите источник.")

    module, separator, name = address.partition("::")
    module = module.strip()
    name = name.strip()
    if not separator or not module or not name or "::" in name:
        raise RegistryError(
            "address должен быть точным адресом процедуры `Модуль::Имя`; "
            "адрес одного модуля для get_callers недостаточен."
        )
    canonical_module = next(
        (
            item
            for item in loaded.оглавление.модули
            if item.casefold() == module.casefold()
        ),
        None,
    )
    if canonical_module is None:
        похожие = _similar_address(loaded, module, None)
        хвост = (
            ""
            if not похожие
            else "\n\nВозможно, имелось в виду:\n"
            + "\n".join(f"- `{item}`" for item in похожие)
        )
        if not _modules_are_current(registry, loaded):
            raise _StaleModules
        return f"Модуль `{module}` в загруженном коде не найден.{хвост}\n"
    module = canonical_module
    записи_модуля = loaded.оглавление.модуля(module)
    совпадения = [
        запись for запись in записи_модуля if запись.имя.casefold() == name.casefold()
    ]
    if not совпадения:
        похожие = _similar_address(loaded, module, name)
        хвост = (
            ""
            if not похожие
            else "\n\nВозможно, имелось в виду:\n"
            + "\n".join(f"- `{item}`" for item in похожие)
        )
        if not _modules_are_current(registry, loaded):
            raise _StaleModules
        return f"В модуле `{module}` нет процедуры `{name}`.{хвост}\n"
    canonical_name = совпадения[0].имя

    выбор = loaded.вызовы.выбрать(canonical_name, module, limit=limit)
    показанные_места = выбор.подтверждённые
    остаток_бюджета = limit - len(показанные_места)
    показанные_неразрешённые_места = выбор.неразрешённые[:остаток_бюджета]
    границы = _caller_boundaries(
        loaded,
        {
            item.модуль
            for item in [*показанные_места, *показанные_неразрешённые_места]
        },
    )
    показанные = [_caller_site(границы, item) for item in показанные_места]
    показанные_неразрешённые = [
        _caller_site(границы, item) for item in показанные_неразрешённые_места
    ]
    metadata = _metadata_bindings(context, module, canonical_name)
    form_bindings, form_state = _form_bindings(loaded, module, canonical_name)
    warnings = _module_warnings(context, loaded, записи_модуля)

    # Все структуры ответа принадлежат одному объекту LoadedModules. Между
    # чтением массивов и этой проверкой reparse/remove может заменить пакет;
    # тогда весь запрос повторяется, а не смешивает два поколения.
    if not _modules_are_current(registry, loaded):
        raise _StaleModules
    return render_callers(
        context.name,
        f"{module}::{canonical_name}",
        code_sites=показанные,
        confirmed_total=выбор.подтверждённых_всего,
        omitted_modules=выбор.пропущено_в_модулях,
        unresolved_sites=показанные_неразрешённые,
        unresolved_total=выбор.неразрешённых_всего,
        metadata=metadata,
        form_bindings=form_bindings,
        form_state=form_state,
        warnings=warnings,
        extension=extension,
    )


def get_callers(
    registry: Registry,
    address: str,
    config: str | None = None,
    extension: str | None = None,
    limit: int = 20,
) -> str:
    """Подтверждённые вызовы, привязки метаданных и обработчики формы."""
    limit = _procedure_limit(limit)
    if not isinstance(address, str) or not address.strip():
        raise RegistryError("address не может быть пустым.")
    for _ in range(2):
        try:
            return _get_callers_once(registry, address, config, extension, limit)
        except _StaleModules:
            continue
    raise RegistryError(
        "Код изменился во время обратного поиска дважды; повторите запрос "
        "после завершения загрузки."
    )


# --------------------------------------------------------------- справка


def _syntax_context(registry: Registry, config: str | None):
    context = registry.resolve(config, require_configuration=False)
    # Каждый из двух источников самостоятелен (`docs/query-language-design.md`,
    # «Работа без справки платформы»): справка платформы весит десятки МБ, а
    # язык запросов — отдельный лёгкий файл. Требовать оба сразу незачем,
    # отказ остаётся только когда не подключено ни одного.
    if context.syntax is None:
        raise RegistryError(
            "Не подключено ни справки платформы, ни справки по языку "
            "запросов. Загрузите `shcntx_ru.hbk` или `shquery_ru.hbk` из "
            "каталога установки 1С."
        )
    return context


def search_syntax(
    registry: Registry,
    query: str,
    config: str | None = None,
    kind: str | None = None,
    limit: int = 10,
) -> str:
    """Найти метод, свойство или объект платформы.

    Результаты отфильтрованы по версии платформы конфигурации: того, чего в
    ней ещё нет, в выдаче не будет.
    """
    context = _syntax_context(registry, config)
    keep = context.syntax_filter()
    kinds = [kind] if kind else None

    limit = _clamp(limit)
    raw = context.syntax.index.search(query, limit=limit * 4, kinds=kinds)
    allowed = [h for h in raw if keep(h.doc.payload)]
    filtered_out = [h for h in raw if not keep(h.doc.payload)]
    hits = allowed[:limit]

    if not hits:
        where = (
            f" (платформа конфигурации {context.platform})"
            if context.configuration is not None
            else ""
        )
        # Пусто по двум разным причинам, и ответ у них разный. Если выдачу
        # обнулил фильтр версии, «ничего не найдено» — прямая ложь: агент
        # пойдёт искать опечатку в имени, которое существует. Поймано на живой
        # справке 2026-08-17 запросом «ПолучитьБуферДвоичныхДанных» под
        # конфигурацией 8.3.5.
        скрытое = _hidden_block(context, filtered_out)
        if скрытое:
            head = [
                f"# {_заголовок_выдачи(context)}: «{query}»",
                f"В версии платформы {context.platform} доступного ничего нет, "
                "но подходящие элементы есть в других версиях.",
            ]
            return "\n".join(head + скрытое) + "\n" + _notes_block(context)
        return (
            f"По запросу «{query}» в {_где_искали(context)} "
            f"ничего не найдено{where}."
            + _notes_block(context)
        )

    out = [f"# {_заголовок_выдачи(context)}: «{query}»"]
    if context.configuration is not None:
        out.append(f"Для конфигурации {context.name}, платформа {context.platform}")
    elif context.syntax.source.platform:
        out.append(
            f"Справка {context.syntax.source.platform}, "
            "конфигурация не выбрана — фильтрации по версии нет"
        )
    else:
        # Справки платформы нет вовсе — источник только язык запросов, у
        # которого версии не бывает. Прежний текст подставлял сюда пустую
        # строку («Справка , конфигурация не выбрана») — тот же класс ошибки,
        # что и в `list_configurations` (`_syntax_only_overview`).
        out.append("Только язык запросов — версий у него не бывает")
    out.append("")
    for hit in hits:
        item: SyntaxItem = hit.doc.payload
        out.append(f"- `{item.address}` / `{item.full_en}`")
        facts = [KIND_TITLES.get(item.kind, item.kind)]
        if item.since:
            facts.append(f"с {item.since}")
        # Доступность берётся по версии конфигурации: мобильных контекстов в
        # старых платформах не существовало, а справка приписала их задним
        # числом тысячам элементов. У языка запросов версий нет по устройству
        # формата (`facts_for` их и не считает) — на пустом наборе платформ
        # (без справки платформы) она падает `AttributeError`, вызывать её
        # для этих элементов незачем и нельзя.
        resolution = (
            context.syntax.syntax.facts_for(item, context.platform)
            if context.platform and not _is_query_kind(item.kind)
            else None
        )
        availability = (
            resolution.availability
            if resolution is not None and resolution.availability
            else item.availability
        )
        if availability:
            facts.append(", ".join(availability))
        if resolution is not None and not resolution.exact:
            facts.append(f"сведения по справке {resolution.platform}")
        out.append(f"  {' · '.join(facts)}")
        if item.kind == KIND_QUERY_ARTICLE:
            out.append(f"  {_article_snippet(item)}")

    return "\n".join(out + _hidden_block(context, filtered_out)) + "\n" + _notes_block(context)


КВАЛИФИКАТОР_ЗАПРОСА = ("запрос.", "языкзапросов.", "язык запросов.")


def _снять_квалификатор(name: str) -> tuple[str, bool]:
    """`Запрос.СтрНайти` → («стрнайти», только язык запросов).

    Одно имя живёт в двух доменах: `СтрНайти` есть и в платформе (с 8.3.6), и
    в языке запросов. У платформенного элемента есть владелец и его можно
    назвать `Глобальный контекст.СтрНайти`; у элемента языка запросов владельца
    нет вовсе, и до этого квалификатора достать его по имени было нечем.
    """
    имя = name.strip()
    низкое = имя.lower()
    for приставка in КВАЛИФИКАТОР_ЗАПРОСА:
        if низкое.startswith(приставка):
            return имя[len(приставка):].strip().lower(), True
    return низкое, False


def _is_query_kind(kind: str) -> bool:
    """Элемент источника языка запросов — без версии по устройству формата.

    Не путать с `query_table`/`query_field`: те же таблицы языка запросов,
    но описаны в справке платформы и версионируются как любой её элемент.
    """
    return kind in QUERY_LANGUAGE_KINDS


def _article_snippet(item: SyntaxItem) -> str:
    """Первый абзац статьи, обрезанный до `SNIPPET_CHARS`.

    Полный текст — только через `get_syntax` по имени: карточка отвечает на
    прямой вопрос «что говорит статья про X», а поиск — на «что вообще
    находится», и туда рецепт целиком класть незачем (см. `SNIPPET_CHARS`).
    """
    абзац = item.description.split("\n\n", 1)[0].strip()
    if len(абзац) > SNIPPET_CHARS:
        абзац = абзац[:SNIPPET_CHARS].rstrip() + "…"
    return абзац


def _заголовок_выдачи(context) -> str:
    """Чем представляется выдача поиска: справкой платформы, языком запросов
    или обоими сразу.

    Прежде строка была одна — «Справка платформы» — и печаталась даже когда
    платформы не загружено вовсе, тут же противореча собственному подзаголовку
    «Только язык запросов».
    """
    платформа = bool(context.syntax and context.syntax.syntax.platforms)
    return "Справка платформы" if платформа else "Язык запросов"


def _где_искали(context) -> str:
    """То же самое в предложном падеже: «в справке платформы», «в языке
    запросов». Отдельной функцией, потому что склонять на месте — значит
    получить «в справка платформы»."""
    платформа = bool(context.syntax and context.syntax.syntax.platforms)
    return "справке платформы" if платформа else "языке запросов"


def _hidden_block(context, filtered_out: list) -> list[str]:
    """Что фильтр версии убрал из выдачи и почему.

    Причины две и они противоположные: одно ещё не появилось, другого уже нет.
    Блок собирается отдельно, потому что нужен и там, где выдача есть, и там,
    где фильтр не оставил ничего — во втором случае молчание превращается в
    «такого имени нет».
    """
    if not filtered_out:
        return []

    target = parse_version(context.platform)
    позже = [h for h in filtered_out if _appears_later(h.doc.payload, target)]
    удалены = [h for h in filtered_out if not _appears_later(h.doc.payload, target)]

    out: list[str] = []
    if позже:
        out.append("")
        out.append(
            f"> Ещё {len(позже)} подходящих элементов скрыто: они появились "
            f"в платформе позже {context.platform}."
        )
        с_заменой = []
        for hit in позже[:3]:
            item = hit.doc.payload
            # Сам рецепт в выдачу не идёт: десять фрагментов кода в списке из
            # десяти строк съели бы контекст агента, а без оговорки «чем замена
            # отличается» рецепт превращает невыполнимый код в неверный.
            замена = " · есть замена" if replacements.find(item.name_ru) else ""
            if замена:
                с_заменой.append(item.name_ru)
            out.append(f"> - `{item.address}` — с {item.since}{замена}")

        # Отметки мало: агент отвечает на вопрос буквально («есть ли метод?») и
        # за рецептом сам не идёт — проверено на живом агенте 2026-08-17.
        # Работает указание, а не намёк, как со строкой про `config` в
        # `list_configurations`.
        if с_заменой:
            имена = ", ".join(f"`{имя}`" for имя in с_заменой)
            out.append(
                f"> Для помеченных есть чем заменить: вызовите `get_syntax` "
                f"({имена}) и покажите рецепт вместе с ответом."
            )

    if удалены:
        out.append("")
        out.append(
            f"> Ещё {len(удалены)} подходящих элементов скрыто: в платформе "
            f"{context.platform} их уже нет."
        )
        for hit in удалены[:3]:
            item = hit.doc.payload
            out.append(f"> - `{item.address}` — по версию {item.until} включительно")

    return out


def _appears_later(item: SyntaxItem, target: tuple[int, ...]) -> bool:
    """Элемент недоступен потому, что ещё не появился, а не потому, что удалён.

    Проверять `until` на непустоту нельзя: элемент, живший только в средней
    справке, имеет обе границы сразу (`since=8.3.18`, `until=8.3.19`), и для
    конфигурации 8.3.5 верная причина — «ещё не появился».
    """
    return bool(item.since) and release(item.since_tuple) > release(target)


def _replacement_block(item: SyntaxItem, target: tuple[int, ...], platform: str) -> str:
    """Рецепт замены, если он написан. Пусто — значит сказать нечего.

    Только для элементов, которые ещё не появились. Удалённому рецепт «для
    старых платформ» не подходит: там нужен путь вперёд, а не назад, и
    подставить один вместо другого — соврать ровно в ту сторону, из-за которой
    затевалось слияние справок.
    """
    if not _appears_later(item, target):
        return ""
    # Таблица замен написана про платформу: вместо `СтрНайти` — `Найти`. У
    # функции языка запросов имя то же, но `Найти` — метод глобального
    # контекста, в тексте запроса его не написать. Подставить сюда этот рецепт
    # значит выдать невыполнимый совет за проверенный.
    if _is_query_kind(item.kind):
        return ""
    рецепт = replacements.find(item.name_ru)
    if рецепт is None:
        return ""

    out = ["", f"**Чем заменить в {platform}:** {рецепт.instead}"]
    if рецепт.code:
        out += ["", "```bsl", рецепт.code, "```"]
    out += ["", f"**Отличие:** {рецепт.note}"]
    return "\n".join(out)


def _unavailable_reason(item: SyntaxItem, target: tuple[int, ...]) -> str:
    if _appears_later(item, target):
        return f"появился в **{item.since}**"
    if item.until:
        return f"описан по версию **{item.until}** включительно, дальше его нет"
    return "недоступен в этой версии"


def _отсечённые_однофамильцы(context, отсечённые: list[SyntaxItem], *,
                             подробно: bool) -> str:
    """Одноимённые, которых фильтр версии убрал, — названные вслух.

    Фильтр молчит, и это правильно, пока он убирает всё: тогда срабатывает
    `_unavailable_here` с причиной и заменой. Но если рядом остался
    одноимённый — из языка запросов, у которого версий не бывает, или просто
    доступный член другого объекта, — отсечённое исчезало бесследно.

    Живой промах 2026-08-19: на конфигурации 8.3.5 запрос `СтрНайти` отдавал
    карточку функции языка запросов и ни слова о том, что платформенная
    появилась в 8.3.6. Замер по живому реестру: 419 таких имён на 8.3.5,
    124 на 8.3.23, 101 на 8.3.27.

    `подробно` — отдана карточка одного элемента, места хватает на причину и
    рецепт. Иначе печатается список одноимённых, и туда идёт короткая сводка:
    у имени вроде «количество» отсечённых бывает полсотни.
    """
    if not отсечённые or not context.platform:
        return ""

    target = parse_version(context.platform)
    показать = отсечённые[:3] if подробно else отсечённые[:5]
    сколько = len(отсечённые)
    # Согласование: «Ещё 1 одноимённых недоступны» читается как опечатка, а
    # текст этот агент видит чаще всего именно в единственном числе.
    заголовок = (
        f"> **Ещё один одноимённый недоступен в {context.platform}:**"
        if сколько == 1
        else f"> **Ещё {сколько} одноимённых недоступны в {context.platform}:**"
    )
    out = ["", заголовок]
    for item in показать:
        out.append(f"> - `{item.address}` — {_unavailable_reason(item, target)}")
    if len(отсечённые) > len(показать):
        out.append(f"> - …и ещё {len(отсечённые) - len(показать)}")

    if подробно:
        for item in показать:
            блок = _replacement_block(item, target, context.platform)
            if блок:
                out.append(f"\n## Замена для `{item.address}`{блок}")
    else:
        out.append("> ")
        out.append("> Спросите по полному адресу, чтобы увидеть причину и замену.")
    return "\n".join(out) + "\n"


def _unavailable_here(context, matching: list[SyntaxItem]) -> str:
    """Элемент есть в платформе, но не в версии этой конфигурации.

    Причины две и они противоположные: либо он появился позже, либо был
    удалён раньше. Сказать «появился в версии X» про удалённый — соврать в
    ту же сторону, из-за которой затевалось слияние справок. У однофамильцев
    причины могут различаться, поэтому перечисляются все: умолчать про второй
    элемент — значит скрыть, что после обновления платформы он появится.
    """
    target = parse_version(context.platform)

    if len(matching) > 1:
        out = [
            f"# Ни один из одноимённых элементов не доступен в {context.platform}",
            "",
        ]
        рецепты = []
        for item in sorted(matching, key=lambda i: i.full_ru):
            out.append(f"- `{item.address}` — {_unavailable_reason(item, target)}")
            блок = _replacement_block(item, target, context.platform)
            if блок:
                рецепты.append(f"\n## Замена для `{item.address}`{блок}")
        out.append("")
        out.append(
            f"Конфигурация {context.name} работает на **{context.platform}**; "
            "использовать их нельзя — код не скомпилируется."
        )
        return "\n".join(out + рецепты) + "\n" + _notes_block(context)

    item = matching[0]
    if _appears_later(item, target):
        рецепт = _replacement_block(item, target, context.platform)
        # Без рецепта заканчиваем прежней фразой: сказать «нужен другой способ»
        # честнее, чем промолчать, но хуже, чем назвать способ.
        хвост = (
            f"\n{рецепт}" if рецепт else f" Нужен способ, доступный в {context.platform}."
        )
        return (
            f"# `{item.address}` недоступен в этой конфигурации\n\n"
            f"Элемент существует в платформе, но появился в версии "
            f"**{item.since}**, а конфигурация {context.name} работает на "
            f"**{context.platform}**.\n\n"
            f"Использовать его нельзя — код не скомпилируется."
            f"{хвост}\n"
            + _notes_block(context)
        )

    return (
        f"# `{item.address}` в этой конфигурации не существует\n\n"
        f"Элемент описан в справке по версию **{item.until}** включительно "
        f"и в более поздних справках отсутствует, а конфигурация "
        f"{context.name} работает на **{context.platform}**.\n\n"
        f"Использовать его нельзя — код не скомпилируется.\n"
        + _notes_block(context)
    )


def get_syntax(
    registry: Registry,
    name: str,
    config: str | None = None,
    detail: str = FIELDS,
) -> str:
    """Полное описание элемента платформы: сигнатура, параметры, доступность."""
    context = _syntax_context(registry, config)
    keep = context.syntax_filter()
    wanted, только_запросы = _снять_квалификатор(name)

    matching = context.syntax.find_exact(wanted)
    if только_запросы:
        matching = [item for item in matching if _is_query_kind(item.kind)]
    exact = [item for item in matching if keep(item)]
    отсечённые = [item for item in matching if not keep(item)]

    # Элемент найден, но в платформе конфигурации его нет. Молчать об этом
    # нельзя: агент решит, что перепутал имя, и будет искать несуществующее.
    # Правильный вывод — что метод есть, но для этой версии нужен другой путь.
    if matching and not exact:
        return _unavailable_here(context, matching)

    if not exact:
        raw = context.syntax.index.search(name, limit=20)
        hits = [h for h in raw if keep(h.doc.payload)]
        # Похожее, отсеянное по версии, — тоже подсказка, и на старой платформе
        # самая нужная: новые API агент помнит лучше, чем 8.3.5, и ошибается в
        # именах чаще именно там. Выбросить их значит ответить «такого нет»
        # там, где верно «есть, но не в вашей версии».
        скрытое = _hidden_block(context, [h for h in raw if not keep(h.doc.payload)])
        if not hits:
            if скрытое:
                head = [
                    f"Точного совпадения нет, и всё похожее недоступно "
                    f"в {context.platform}."
                ]
                return "\n".join(head + скрытое) + "\n" + _notes_block(context)
            # Называть надо ту справку, по которой ответ строился, а не
            # объединённый источник: тот всегда назовётся самой свежей из
            # загруженных, и отказ выходит подписан чужой версией.
            справка = context.syntax_platform
            если_совпало = справка == context.platform
            return (
                f"В справке платформы {справка} нет элемента `{name}`"
                + ("." if если_совпало else f", доступного в версии {context.platform}.")
                + _notes_block(context)
            )
        suggestion = "\n".join(f"- `{h.doc.payload.full_ru}`" for h in hits[:5])
        return (
            "\n".join([f"Точного совпадения нет. Возможно:\n{suggestion}"] + скрытое)
            + "\n"
            + _notes_block(context)
        )

    if len(exact) > 1:
        out = [f"# Одноимённых элементов: {len(exact)}", ""]
        for item in exact[:15]:
            out.append(
                f"- `{item.address}` — {KIND_TITLES.get(item.kind, item.kind)}"
                + (f", с {item.since}" if item.since else "")
            )
        out.append("")
        # Прежний текст советовал «уточните в виде `Объект.Член`» — для языка
        # запросов это тупик: владельца у элемента нет, и назвать его было
        # нечем. Теперь в списке стоит готовый адрес каждого, включая
        # квалификатор `Запрос.`.
        out.append("Повторите вызов с адресом из списка.")
        return (
            "\n".join(out)
            + "\n"
            + _отсечённые_однофамильцы(context, отсечённые, подробно=False)
            + _notes_block(context)
        )

    # Карточка собирается под версию конфигурации: сигнатура, доступность и
    # имя между версиями менялись, и разница в один параметр — ошибка
    # компиляции, а не мелочь. У языка запросов версий нет (см. проверку в
    # `search_syntax` выше) — тот же `_is_query_kind` бережёт от `facts_for`
    # на пустом наборе платформ.
    resolution = (
        context.syntax.syntax.facts_for(exact[0], context.platform)
        if context.platform and not _is_query_kind(exact[0].kind)
        else None
    )
    return (
        render_syntax_item(exact[0], detail, resolution)
        + _отсечённые_однофамильцы(context, отсечённые, подробно=True)
        + _notes_block(context)
    )
