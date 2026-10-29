"""Инструменты поиска и навигации по справке платформы."""

from __future__ import annotations

import re
from typing import TYPE_CHECKING

from . import replacements
from .registry_types import RegistryError
from .render import DETAIL_LEVELS, FIELDS, FULL, render_syntax_item
from .syntax_model import KIND_TITLES, SyntaxItem, parse_version, release
from .model import KIND_TO_MANAGER
from .virtual_tables import table_placeholder
from .tools_common import _clamp, _render_notes

if TYPE_CHECKING:
    from .registry import Registry


def _syntax_context(registry: Registry, config: str | None):
    context = registry.resolve(config, require_configuration=False)
    if context.syntax is None:
        raise RegistryError(
            "Справка платформы не подключена. Загрузите `shcntx_ru.hbk` "
            "из каталога установки 1С."
        )
    return context


def _syntax_notes_block(context) -> str:
    notes = context.notes(critical_only=True)
    if context.configuration is not None:
        mode = context.configuration.config.compatibility_mode
        if mode and mode not in {"DontUse", "НеИспользовать"}:
            scope = (
                "справка сопоставлена по версии платформы"
                if parse_version(context.platform)
                else "версия платформы неизвестна"
            )
            notes.append(
                f"Режим совместимости {mode}: {scope}, но ограничения этого "
                "режима для методов не "
                "проверены. Компиляция в данном режиме — unknown."
            )
    return _render_notes(notes)


def _варианты_таблицы(context, item: SyntaxItem) -> str:
    """Подпись таблицы, которую справка описывает несколькими страницами.

    Разные наборы полей — это вариант регистра бухгалтерии: корреспонденция
    добавляет поля (`СуммаОстатокДт`), так же выбирает `virtual_tables`.
    Одинаковые наборы — повтор разметки, подписывать нечего. Поля берутся из
    готового индекса шаблонов, без обхода справки.
    """
    parsed = table_placeholder(item.name_ru)
    if parsed is None:
        return ""
    kind, suffix = parsed
    наборы = {
        frozenset(template.fields)
        for template in context.syntax.tables.get(kind, ())
        if template.suffix == suffix
    }
    if len(наборы) < 2:
        return ""
    if len(наборы) == 2:
        меньший, больший = sorted(наборы, key=len)
        if меньший < больший:
            return "2 варианта: с корреспонденцией и без"
    return f"{len(наборы)} варианта с разным набором полей"


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
    hits = context.syntax.index.search(
        query, limit=limit, kinds=kinds,
        predicate=lambda doc: (
            _поисковый_элемент_справки(context, doc.payload) and keep(doc.payload)
        ),
    )
    # Окно нужно только для ограниченных подсказок о скрытом. Оно не может
    # ограничивать поиск доступных элементов: все верхние места бывают новее.
    raw = context.syntax.index.search(
        query, limit=limit * 4, kinds=kinds,
        predicate=lambda doc: _поисковый_элемент_справки(context, doc.payload),
    )
    filtered_out = [h for h in raw if not keep(h.doc.payload)]

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
                f"# Справка платформы: «{query}»",
                f"В версии платформы {context.platform} доступного ничего нет, "
                "но подходящие элементы есть в других версиях.",
            ]
            return "\n".join(head + скрытое) + "\n" + _syntax_notes_block(context)
        return (
            f"По запросу «{query}» в справке платформы "
            f"ничего не найдено{where}."
            + _syntax_notes_block(context)
        )

    out = [f"# Справка платформы: «{query}»"]
    if context.configuration is not None:
        out.append(f"Для конфигурации {context.name}, платформа {context.platform}")
    else:
        out.append(
            f"Справка {context.syntax.source.platform}, "
            "конфигурация не выбрана — фильтрации по версии нет"
        )
    out.append("")
    показанные: set[str] = set()
    for hit in hits:
        item: SyntaxItem = hit.doc.payload
        # Справка описывает часть таблиц дважды: регистр бухгалтерии с
        # корреспонденцией и без, а `Остатки` — просто повтором разметки.
        # Две одинаковые строки выглядели ошибкой; вариант — в `get_syntax`.
        if item.kind == "query_table":
            if item.address in показанные:
                continue
            показанные.add(item.address)
        out.append(f"- `{item.address}` / `{item.full_en}`")
        facts = [KIND_TITLES.get(item.kind, item.kind)]
        if item.kind == "query_table":
            варианты = _варианты_таблицы(context, item)
            if варианты:
                facts.append(варианты)
        if item.since:
            facts.append(f"с {item.since}")
        deprecation = _deprecation_for_context(context, item)
        if deprecation is not None:
            facts.append(f"не рекомендуется с {deprecation.since}")
        # Доступность берётся по версии конфигурации: мобильных контекстов в
        # старых платформах не существовало, а справка приписала их задним
        # числом тысячам элементов.
        resolution = (
            context.syntax.syntax.facts_for(item, context.platform)
            if context.platform
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
    return "\n".join(out + _hidden_block(context, filtered_out)) + "\n" + _syntax_notes_block(context)


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


def _deprecation_for_context(context, item: SyntaxItem):
    """Предупреждение только из справки точного релиза конфигурации."""
    if not item.deprecations or not context.platform:
        return None
    target = release(parse_version(context.platform))
    return next(
        (entry for entry in item.deprecations
         if release(parse_version(entry.platform)) == target),
        None,
    )


def _deprecation_block(context, item: SyntaxItem) -> str:
    entry = _deprecation_for_context(context, item)
    if entry is None:
        return ""
    lines = [f"\n**Не рекомендуется использовать с версии {entry.since}.**"]
    for link in entry.replacements:
        target = context.syntax.syntax.items.get(link.target_id) if link.target_id else None
        if target is None:
            lines.append(f"Рекомендуемая замена: {link.label} — цель не разрешена.")
        elif context.platform and not target.available_in(context.platform):
            lines.append(f"Рекомендуемая замена: {link.label} — недоступна в платформе {context.platform}.")
        elif [candidate.id for candidate in _точные_элементы(context, target.address)
              if not context.platform or candidate.available_in(context.platform)] != [target.id]:
            lines.append(f"Рекомендуемая замена: {link.label} — публичный адрес неоднозначен.")
        else:
            lines.append(f"Рекомендуемая замена: `{target.address}`.")
    return "\n".join(lines) + "\n"


def _отсечённые_однофамильцы(context, отсечённые: list[SyntaxItem], *,
                             подробно: bool) -> str:
    """Одноимённые, которых фильтр версии убрал, — названные вслух.

    Фильтр молчит, и это правильно, пока он убирает всё: тогда срабатывает
    `_unavailable_here` с причиной и заменой. Но если рядом остался доступный
    одноимённый член другого объекта, отсечённое исчезало бесследно.

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
        return "\n".join(out + рецепты) + "\n" + _syntax_notes_block(context)

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
            + _syntax_notes_block(context)
        )

    return (
        f"# `{item.address}` в этой конфигурации не существует\n\n"
        f"Элемент описан в справке по версию **{item.until}** включительно "
        f"и в более поздних справках отсутствует, а конфигурация "
        f"{context.name} работает на **{context.platform}**.\n\n"
        f"Использовать его нельзя — код не скомпилируется.\n"
        + _syntax_notes_block(context)
    )


_RE_РАЗДЕЛ_СПРАВКИ = re.compile(r"(?:^|/)catalog\d+(?:@[\d.]+)?$")


def _пустой_раздел_справки(item: SyntaxItem) -> bool:
    """Страница папки оглавления справки без собственного содержимого.

    Разбор берёт в индекс каждую страницу дерева, и папки вроде
    `objects/catalog2/catalog259` («Запрос») становятся объектами. В
    объединённой справке 2026-10-03 таких страниц 284; 16 из них делят
    публичный адрес с настоящим элементом, и `get_syntax` показывал их пустым
    «вариантом». Шесть страниц разделов несут пример кода — они
    содержательны и остаются.
    """
    if not _RE_РАЗДЕЛ_СПРАВКИ.search(item.id):
        return False
    return not (
        item.description
        or item.variants
        or item.examples
        or item.members
        or item.values
        or item.note
        or item.see_also
        or item.availability
        or item.deprecations
        or any(snapshot.links for snapshot in item.link_snapshots)
    )


def _поисковый_элемент_справки(context, item: SyntaxItem) -> bool:
    """Сохранить элемент, если он не пустая папка с содержательным дублем.

    Проверка предшествует фильтру версии: пустая папка не делает новый
    элемент доступным на старой платформе. Поиск точного адреса использует
    готовый индекс имён, без обхода всей справки на каждый результат.
    """
    if not _пустой_раздел_справки(item):
        return True
    address = item.address.casefold()
    return not any(
        peer.address.casefold() == address and not _пустой_раздел_справки(peer)
        for peer in context.syntax.find_exact(item.address)
    )


def _точные_элементы(context, name: str) -> list[SyntaxItem]:
    """Элементы справки с точным именем или адресом, без пустых разделов.

    Пустой раздел оглавления — не вариант элемента: показать в нём нечего,
    а отдельного адреса, чтобы до него дойти, у него нет. Через этот же
    поиск переходы карточки решают, однозначен ли адрес цели, поэтому раздел
    отсеивается здесь, а не только в `get_syntax`: иначе ссылка на `Запрос`
    числилась бы неоднозначной, хотя карточка открывается одна. Отсев идёт
    до фильтра версии: на 8.3.5 «Планировщик» (появился в 8.3.6) давал два
    пустых «варианта» вместо честного «недоступен в этой версии». Одинокий
    раздел остаётся, чтобы точное имя не превращалось в «элемента нет».
    """
    matching = context.syntax.find_exact(name)
    if len(matching) < 2:
        # Единственный кандидат отсеивать не из чего, а переходы карточки
        # зовут этот поиск на каждую из 50 ссылок.
        return matching
    содержательные =[item for item in matching if not _пустой_раздел_справки(item)]
    return содержательные or matching


# Множественное имя менеджера в коде → вид: `Справочники` → `Справочник`.
_MANAGER_TO_KIND = {manager: kind for kind, manager in KIND_TO_MANAGER.items()}


def _шаблоны_для_имени(context, name: str) -> list[SyntaxItem]:
    """Шаблоны справки для конкретного имени: `Справочники.Номенклатура.ПустаяСсылка`.

    Справка описывает члены объектов один раз на вид —
    `СправочникМенеджер.<Имя справочника>.ПустаяСсылка`, — и агент, спросивший
    так, как пишет в коде, получал «совпадения нет». Первый сегмент — вид в
    любой из форм кода (`Справочник`, `Справочники`) с окончаниями типов
    справки, сегмент `<…>` шаблона совпадает с любым одним сегментом.
    """
    сегменты = name.strip().split(".")
    if len(сегменты) < 3 or not all(сегменты):
        return []
    вид = _MANAGER_TO_KIND.get(сегменты[0], сегменты[0])
    первые = {
        value.casefold()
        for base in {сегменты[0], вид}
        for value in (base, base + "Менеджер", base + "Ссылка", base + "Объект")
    }
    найденные: list[SyntaxItem] = []
    for item in context.syntax.find_exact(сегменты[-1]):
        шаблон = item.address.split(".")
        if len(шаблон) != len(сегменты) or шаблон[0].casefold() not in первые:
            continue
        if not any(part.startswith("<") for part in шаблон[1:-1]):
            continue
        if all(
            part.startswith("<") or part.casefold() == wanted.casefold()
            for part, wanted in zip(шаблон[1:], сегменты[1:])
        ) and item not in найденные:
            найденные.append(item)
    return найденные


def get_syntax(
    registry: Registry,
    name: str,
    config: str | None = None,
    detail: str = FIELDS,
    links_offset: int = 0,
) -> str:
    """Полное описание элемента платформы: сигнатура, параметры, доступность."""
    if detail not in (*DETAIL_LEVELS, "links"):
        raise RegistryError("Неизвестный уровень карточки синтаксиса.")
    if links_offset < 0:
        raise RegistryError("links_offset не может быть отрицательным.")
    if links_offset and detail != "links":
        raise RegistryError("links_offset используется только с detail=links.")
    context = _syntax_context(registry, config)
    keep = context.syntax_filter()
    wanted = name.strip().lower()

    matching = _точные_элементы(context, wanted)
    exact = [item for item in matching if keep(item)]
    отсечённые = [item for item in matching if not keep(item)]

    # Элемент найден, но в платформе конфигурации его нет. Молчать об этом
    # нельзя: агент решит, что перепутал имя, и будет искать несуществующее.
    # Правильный вывод — что метод есть, но для этой версии нужен другой путь.
    if matching and not exact:
        return _unavailable_here(context, matching)

    if not matching:
        шаблоны = [item for item in _шаблоны_для_имени(context, name) if keep(item)]
        if len(шаблоны) == 1:
            карточка = get_syntax(
                registry, шаблоны[0].address, config, detail, links_offset
            )
            return (
                f"> Шаблон справки для `{name.strip()}`: в справке члены "
                "объектов описаны один раз на вид, с `<Имя …>` вместо имени.\n\n"
                + карточка
            )
        if шаблоны:
            строки = "\n".join(
                f"- `{item.address}` — {KIND_TITLES.get(item.kind, item.kind)}"
                for item in шаблоны
            )
            return (
                f"# Шаблоны справки для `{name.strip()}`\n\n{строки}\n\n"
                "Повторите вызов с адресом из списка.\n"
                + _syntax_notes_block(context)
            )

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
                return "\n".join(head + скрытое) + "\n" + _syntax_notes_block(context)
            # Называть надо ту справку, по которой ответ строился, а не
            # объединённый источник: тот всегда назовётся самой свежей из
            # загруженных, и отказ выходит подписан чужой версией.
            справка = context.syntax_platform
            если_совпало = справка == context.platform
            return (
                f"В справке платформы {справка} нет элемента `{name}`"
                + ("." if если_совпало else f", доступного в версии {context.platform}.")
                + _syntax_notes_block(context)
            )
        suggestion = "\n".join(f"- `{h.doc.payload.full_ru}`" for h in hits[:5])
        return (
            "\n".join([f"Точного совпадения нет. Возможно:\n{suggestion}"] + скрытое)
            + "\n"
            + _syntax_notes_block(context)
        )

    # По короткому имени находятся и чужие свойства с тем же именем. Если
    # запрос совпал с полным публичным адресом, сначала показываем именно его;
    # одноимённые варианты самого адреса по-прежнему выводим все.
    addressed = [item for item in exact if item.address.casefold() == wanted]
    if addressed:
        exact = addressed

    if len(exact) > 1:
        addresses = {item.address.casefold() for item in exact}
        if len(addresses) == 1:
            ordered = sorted(exact, key=lambda item: (item.kind, item.id))
            out = [
                f"# Одноимённые варианты: {len(ordered)}",
                "",
                f"Публичный адрес вариантов совпадает: `{ordered[0].address}`. ",
                "Выбрать один повторным вызовом невозможно, поэтому ниже ",
                "приведены все варианты из справки.",
                "",
            ]
            for number, item in enumerate(ordered, start=1):
                resolution = (
                    context.syntax.syntax.facts_for(item, context.platform)
                    if context.platform
                    else None
                )
                card = (
                    f"# {item.address}\n"
                    if detail == "links"
                    else render_syntax_item(item, detail, resolution)
                )
                if detail != "links":
                    card += _deprecation_block(context, item)
                if detail in (FULL, "links"):
                    card += _syntax_navigation(context, item, links_offset)
                rendered = card.splitlines()
                if rendered and rendered[0].startswith("# "):
                    rendered.pop(0)
                while rendered and not rendered[0]:
                    rendered.pop(0)
                out.extend(
                    [
                        f"## Вариант {number} из {len(ordered)} — "
                        f"{KIND_TITLES.get(item.kind, item.kind)}",
                        "",
                        *rendered,
                        "",
                    ]
                )
            return (
                "\n".join(out).rstrip()
                + "\n"
                + _отсечённые_однофамильцы(context, отсечённые, подробно=True)
                + _syntax_notes_block(context)
            )

        out = [f"# Одноимённых элементов: {len(exact)}", ""]
        for item in exact[:15]:
            out.append(
                f"- `{item.address}` — {KIND_TITLES.get(item.kind, item.kind)}"
                + (f", с {item.since}" if item.since else "")
            )
        out.append("")
        out.append("Повторите вызов с адресом из списка.")
        return (
            "\n".join(out)
            + "\n"
            + _отсечённые_однофамильцы(context, отсечённые, подробно=False)
            + _syntax_notes_block(context)
        )

    # Карточка собирается под версию конфигурации: сигнатура, доступность и
    # имя между версиями менялись, и разница в один параметр — ошибка
    # компиляции, а не мелочь.
    resolution = (
        context.syntax.syntax.facts_for(exact[0], context.platform)
        if context.platform
        else None
    )
    return (
        (f"# {exact[0].address}\n" if detail == "links" else render_syntax_item(exact[0], detail, resolution))
        + (_deprecation_block(context, exact[0]) if detail != "links" else "")
        + (_syntax_navigation(context, exact[0], links_offset) if detail in (FULL, "links") else "")
        + _отсечённые_однофамильцы(context, отсечённые, подробно=True)
        + _syntax_notes_block(context)
    )


_SYNTAX_LINK_SECTIONS = {
    "replacement": "Рекомендуемая замена",
    "properties": "Свойства",
    "methods": "Методы",
    "events": "События",
    "constructors": "Конструкторы",
    "collection": "Элементы коллекции",
    "see_also": "См. также",
}


_SYNTAX_LINK_PAGE_SIZE = 50


def _syntax_navigation_page(context, item: SyntaxItem, offset: int) -> dict:
    """Одна проверенная страница связей для MCP и дашборда."""
    snapshots = item.link_snapshots
    platforms = context.syntax.syntax.platforms
    empty_versions = [
        platform for position, platform in enumerate(platforms)
        if item.empty_link_mask & (1 << position)
    ]
    legacy_versions = [
        platform for position, platform in enumerate(platforms)
        if item.legacy_link_mask & (1 << position)
    ]
    if not snapshots and not empty_versions and not legacy_versions:
        return {"state": "legacy", "items": [], "total": 0, "offset": offset,
                "next_offset": None, "platform": ""}

    if context.platform:
        target_release = release(parse_version(context.platform))
        snapshot = next(
            (
                entry for entry in snapshots
                if release(parse_version(entry.platform)) == target_release
            ),
            None,
        )
        empty_platform = next(
            (platform for platform in empty_versions
             if release(parse_version(platform)) == target_release),
            "",
        )
        if snapshot is None and not empty_platform:
            state = "legacy" if any(
                release(parse_version(platform)) == target_release
                for platform in legacy_versions
            ) else "unknown"
            return {"state": state, "items": [], "total": 0, "offset": offset,
                    "next_offset": None, "platform": ""}
    else:
        latest = max(
            [entry.platform for entry in snapshots] + empty_versions + legacy_versions,
            key=parse_version,
        )
        snapshot = next((entry for entry in snapshots if entry.platform == latest), None)
        empty_platform = latest
        if snapshot is None and latest in legacy_versions and latest not in empty_versions:
            return {"state": "legacy", "items": [], "total": 0, "offset": offset,
                    "next_offset": None, "platform": ""}

    platform = snapshot.platform if snapshot else empty_platform
    seen: set[tuple[str, str, str]] = set()
    unique = []
    for link in snapshot.links if snapshot else ():
        # Одна цель бывает одновременно членом объекта и «См. также» с иной
        # подписью. Убирать можно лишь повтор той же подписанной связи.
        destination = f"id:{link.target_id}" if link.target_id else f"href:{link.href}"
        dedupe_key = (link.section, " ".join(link.label.split()).casefold(), destination)
        if dedupe_key in seen:
            continue
        seen.add(dedupe_key)
        unique.append(link)

    entries = []
    for link in unique[offset:offset + _SYNTAX_LINK_PAGE_SIZE]:
        target = context.syntax.syntax.items.get(link.target_id) if link.target_id else None
        label = " ".join(link.label.split()) or "Ссылка без подписи"
        if target is None:
            address, status = "", "unresolved"
        elif context.platform and not target.available_in(context.platform):
            address, status = "", "unavailable"
        else:
            matching = [
                candidate for candidate in _точные_элементы(context, target.address)
                if not context.platform or candidate.available_in(context.platform)
            ]
            if len(matching) != 1 or matching[0].id != target.id:
                address, status = "", "ambiguous"
            else:
                address, status = target.address, "ready"
        entries.append({"section": link.section, "label": label,
                        "address": address, "status": status,
                        "target_name": target.name_ru if target else ""})
    return {
        "state": "known", "platform": platform, "items": entries,
        "total": len(unique), "offset": offset,
        "next_offset": offset + _SYNTAX_LINK_PAGE_SIZE
        if offset + _SYNTAX_LINK_PAGE_SIZE < len(unique) else None,
    }


def _syntax_navigation(context, item: SyntaxItem, offset: int = 0) -> str:
    """Текст навигации с явной границей ответа для крупной карточки."""
    page = _syntax_navigation_page(context, item, offset)
    if page["state"] == "legacy":
        return (
            "\n## Навигация\n\nСсылки не собирались при прежнем разборе справки. "
            "Для навигации нужен повторный разбор исходной справки `.hbk` этой версии.\n"
        )
    if page["state"] == "unknown":
        return (
            "\n## Навигация\n\n"
            f"Для платформы **{context.platform}** нет подтверждённого "
            "состава ссылок этой карточки: справка её релиза не загружена "
            "или в ней нет этой страницы. Используйте поиск по синтаксису.\n"
        )

    lines = [f"\n## Навигация — справка {page['platform']}", ""]
    if not context.platform:
        lines.extend(["Версия конфигурации не выбрана; доступность целей не проверена.", ""])

    sections: dict[str, list[str]] = {}
    for entry in page["items"]:
        label = entry["label"]
        if entry["status"] == "unresolved":
            line = f"- {label} — цель не разрешена в исходной справке"
        elif entry["status"] == "unavailable":
            line = f"- {label} — недоступно в платформе {context.platform}"
        elif entry["status"] == "ambiguous":
            line = f"- {label} — публичный адрес неоднозначен"
        else:
            line = f"- `{entry['address']}`"
            if label.casefold() != entry["target_name"].casefold():
                line += f" — {label}"
        sections.setdefault(entry["section"], []).append(line)

    if not sections and page["total"] == 0:
        lines.append("Ссылок в этой карточке нет.")
    elif not sections:
        lines.append("На этой странице связей нет.")
    else:
        for section, entries in sections.items():
            lines.extend([f"### {_SYNTAX_LINK_SECTIONS.get(section, section)}", "", *entries, ""])
        lines.append(
            "Для перехода вызовите `get_syntax` с точным адресом из списка "
            "и той же конфигурацией."
        )
    if page["total"] > _SYNTAX_LINK_PAGE_SIZE:
        first = min(offset + 1, page["total"])
        last = min(offset + len(page["items"]), page["total"])
        lines.extend(["", f"Связи {first}–{last} из {page['total']}."])
        if page["next_offset"] is not None:
            lines.append(
                "Следующая страница: вызовите `get_syntax` с тем же `name` и "
                f"`config`, `detail=links`, `links_offset={page['next_offset']}`."
            )
    return "\n".join(lines).rstrip() + "\n"


def syntax_navigation_data(
    registry: Registry, name: str, config: str | None = None, links_offset: int = 0
) -> list[dict]:
    """Структурированные проверенные переходы для карточки дашборда."""
    context = _syntax_context(registry, config)
    keep = context.syntax_filter()
    exact = [item for item in _точные_элементы(context, name.strip().lower()) if keep(item)]
    addressed = [item for item in exact if item.address.casefold() == name.strip().casefold()]
    if addressed:
        exact = addressed
    if len({item.address.casefold() for item in exact}) > 1:
        return []
    ordered = sorted(exact, key=lambda entry: (entry.kind, entry.id))
    return [
        {"variant": (
            f"Вариант {position} из {len(ordered)} — "
            if len(ordered) > 1 else ""
         ) + KIND_TITLES.get(item.kind, item.kind),
         **_syntax_navigation_page(context, item, links_offset)}
        for position, item in enumerate(ordered, start=1)
    ]
