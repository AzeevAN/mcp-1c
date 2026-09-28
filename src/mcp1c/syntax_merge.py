"""Слияние справок нескольких версий платформы в один индекс.

Справка описывает только свою версию. Со свежей справкой на старой
конфигурации сервер ошибается там, где платформа менялась, — и это ошибки
компиляции, а не неточности: элемент объявляется несуществующим, сигнатура
отдаётся с лишним параметром, метод предлагается в контексте, где его нет.

Слияние стоит дёшево: старые справки почти целиком вложены в свежую, поэтому
четыре версии дают 25 003 ключа против 24 777 у одной (замер в CHANGELOG).
Базой становится самая свежая справка, остальные добавляют границы версий и
факты, которые в ней утеряны.
"""

from __future__ import annotations

from dataclasses import replace

from .syntax_links import target_page_id
from .syntax_model import (
    SyntaxDeprecation, SyntaxFacts, SyntaxIndex, SyntaxItem, SyntaxLinkSnapshot,
    parse_version,
)


# Владельцы, у которых между версиями сменились оба имени сразу — русское и
# английское, — поэтому запасной ключ по английскому их не ловит. Таблица
# курируемая: автоматическое сопоставление по составу членов на семье
# «Расширение управляемой формы для…» путает соседей, у них состав совпадает
# полностью и мера сходства не различает.
RENAMED_OWNERS = {
    "УправляемаяФорма": "ФормаКлиентскогоПриложения",
    "Встроенные функции языка": "Глобальный контекст",
    "ТочкаМаршрутаБизнесПроцессаСсылка": "ТочкаМаршрутаБизнесПроцессаСсылка.<Имя бизнес-процесса>",
}

# Та же семья расширений, но правило: имя строится подстановкой, и перечислять
# полтора десятка пар руками — значит ошибиться в одной из них.
_RENAMED_PREFIX = (
    "Расширение управляемой формы для ",
    "Расширение формы клиентского приложения для ",
)


def _owner(item: SyntaxItem) -> str:
    parent = item.parent_ru
    renamed = RENAMED_OWNERS.get(parent)
    if renamed is not None:
        return renamed
    old, new = _RENAMED_PREFIX
    if parent.startswith(old):
        return new + parent[len(old):]
    return parent


def _key(item: SyntaxItem) -> tuple[str, str, str]:
    """Ключ сопоставления версий — русское имя.

    Английское меняется в 3–10 раз чаще: 1С правит собственные опечатки
    (`VerifySignatreAsync` → `VerifySignatureAsync`) и переименовывает
    (`NotifyDescription` → `CallbackDescription`).
    """
    return (item.kind, _owner(item), item.name_ru)


def _key_en(item: SyntaxItem) -> tuple[str, str, str] | None:
    """Запасной ключ: русское имя тоже правят (`Жирный` → `Полужирный`)."""
    if not item.name_en:
        return None
    return (item.kind, item.parent_en, item.name_en)


def merge_syntax(indexes: list[SyntaxIndex]) -> SyntaxIndex:
    """Собрать один индекс из справок разных версий."""
    if not indexes:
        raise ValueError("Слить нечего: не передано ни одной справки.")
    # Версия обязательна: она попадает в границу `until`, а пустая граница
    # означает «элемент есть в самой свежей справке» — противоположное тому,
    # что на самом деле. В справках до 8.3.19 версии нет в самих данных
    # (0 страниц из 18 936 у 8.3.5), поэтому её задаёт человек при загрузке.
    безверсии = [index for index in indexes if not index.max_platform]
    if безверсии:
        raise ValueError(
            "У справки не указана версия платформы: "
            + ", ".join(index.source or "без источника" for index in безверсии)
        )
    ordered = sorted(indexes, key=lambda index: parse_version(index.max_platform))
    base = ordered[-1]

    # Версии объединяются, а не перечисляются по входам: на вход может прийти
    # уже слитый индекс — так справки сливаются по одной, чтобы разобранные не
    # лежали в памяти все сразу. Потерять промежуточную версию нельзя, по
    # списку решается, есть ли справка нужного релиза.
    versions: list[str] = []
    for index in ordered:
        for platform in index.platforms or [index.max_platform]:
            if platform and platform not in versions:
                versions.append(platform)

    merged = SyntaxIndex(
        platforms=sorted(versions, key=parse_version),
        source=base.source,
        language=base.language,
    )
    merged_positions = {platform: position for position, platform in enumerate(merged.platforms)}
    base_bit_positions = [merged_positions.get(platform) for platform in base.platforms]
    # Копии, а не сами элементы: разобранные справки версий реестр держит и
    # пересобирает слитый вид при каждой загрузке. Правка на месте удвоила бы
    # факты на втором слиянии и испортила бы исходную справку.
    for item in base.items.values():
        merged.add(_copy(
            item,
            link_snapshots=[],
            empty_link_mask=_remap_empty_mask(item.empty_link_mask, base_bit_positions),
        ))
    if len(base.platforms) == 1:
        base_ids = {item_id: item_id for item_id in base.items}
        base_page_ids = set(base.items)
        for item in base.items.values():
            merged.items[item.id].link_snapshots = _translated_snapshots(
                item, base, base_ids, base_page_ids
            )
    else:
        # При последовательном слиянии слитая база уже содержит точные ID.
        # Снимки не меняются, поэтому не копируем все связи при каждом шаге.
        for item in base.items.values():
            merged.items[item.id].link_snapshots = list(item.link_snapshots)

    base_ids = {item_id: item_id for item_id in base.items}
    base_page_ids = set(base.items) if len(base.platforms) == 1 else set()
    for item in base.items.values():
        merged.items[item.id].deprecations = _translated_deprecations(
            item, base, base_ids, base_page_ids,
        )

    # От свежих справок к старым: элемент, выпавший из базовой, описывается по
    # самой свежей справке, где он ещё был, а расхождения более старых
    # накапливаются относительно неё.
    known: dict[tuple[str, str, str], SyntaxItem] = {}
    known_en: dict[tuple[str, str, str], SyntaxItem] = {}
    known_id: dict[str, SyntaxItem] = {}
    for item in merged.items.values():
        known.setdefault(_key(item), item)
        known_id.setdefault(item.id, item)
        key_en = _key_en(item)
        if key_en is not None:
            known_en.setdefault(key_en, item)

    for index in ordered[-2::-1]:
        platform = index.max_platform
        bit_positions = [merged_positions.get(version) for version in index.platforms]
        # Внутри одной справки одинаковый ключ встречается у разных страниц —
        # 176 таких ключей в 8.3.5, это поля таблиц запросов. Схлопывать их
        # между собой нельзя: они описывают разные поля.
        claimed: set[tuple[str, str, str]] = set()
        source_to_merged: dict[str, str] = {}
        matched: list[tuple[SyntaxItem, SyntaxItem]] = []
        page_ids = set(index.items) if len(index.platforms) == 1 else set()
        for item in index.items.values():
            key_en = _key_en(item)
            current = _same_element(known_id.get(item.id), item)
            if current is None and _key(item) not in claimed:
                current = known.get(_key(item))
                if current is None and key_en is not None:
                    current = known_en.get(key_en)
            claimed.add(_key(item))
            if current is None:
                copy = _copy(
                    item,
                    id=_free_id(merged, item.id, platform),
                    until=platform,
                    link_snapshots=[],
                    deprecations=[],
                    empty_link_mask=_remap_empty_mask(item.empty_link_mask, bit_positions),
                )
                merged.add(copy)
                known.setdefault(_key(copy), copy)
                known_id.setdefault(copy.id, copy)
                if key_en is not None:
                    known_en.setdefault(key_en, copy)
                current = copy
            else:
                facts = _difference(item, current, platform)
                if facts is not None:
                    current.older.insert(0, facts)
                current.empty_link_mask |= _remap_empty_mask(item.empty_link_mask, bit_positions)
            source_to_merged[item.id] = current.id
            matched.append((item, current))

        # Сначала сопоставить все страницы, затем их ссылки: цель может стоять
        # в архиве после владельца и может получить ID с суффиксом версии.
        for item, current in matched:
            imported = _translated_snapshots(item, index, source_to_merged, page_ids)
            existing = {snapshot.platform for snapshot in current.link_snapshots}
            current.link_snapshots.extend(
                snapshot for snapshot in imported if snapshot.platform not in existing
            )
            current.link_snapshots.sort(key=lambda snapshot: parse_version(snapshot.platform))
            imported_deprecations = _translated_deprecations(
                item, index, source_to_merged, page_ids
            )
            existing_deprecations = {entry.platform for entry in current.deprecations}
            current.deprecations.extend(
                entry for entry in imported_deprecations
                if entry.platform not in existing_deprecations
            )
            current.deprecations.sort(key=lambda entry: parse_version(entry.platform))

    return merged


def _copy(item: SyntaxItem, **changes) -> SyntaxItem:
    """Копия элемента со своим списком версионных фактов."""
    fields = {"older": list(item.older), "link_snapshots": list(item.link_snapshots),
              "deprecations": list(item.deprecations)}
    fields.update(changes)
    return replace(item, **fields)


def _remap_empty_mask(mask: int, positions: list[int | None]) -> int:
    """Перенести биты пустых страниц при изменении порядка версий."""
    if not mask:
        return 0
    result = 0
    for old_position, new_position in enumerate(positions):
        if mask & (1 << old_position) and new_position is not None:
            result |= 1 << new_position
    return result


def _translated_snapshots(
    item: SyntaxItem,
    source: SyntaxIndex,
    id_map: dict[str, str],
    page_ids: set[str],
) -> list[SyntaxLinkSnapshot]:
    """Перенести снимки связей в пространство ID слитого индекса."""
    single_source = len(source.platforms) == 1
    result: list[SyntaxLinkSnapshot] = []
    for snapshot in item.link_snapshots:
        links = []
        for link in snapshot.links:
            source_target = link.target_id
            if not source_target and single_source:
                source_target = target_page_id(
                    snapshot.source_id or item.id, link.href, page_ids
                )
            links.append(replace(link, target_id=id_map.get(source_target, "")))
        result.append(
            replace(
                snapshot,
                platform=snapshot.platform or source.max_platform,
                links=links,
            )
        )
    return result


def _translated_deprecations(
    item: SyntaxItem,
    source: SyntaxIndex,
    id_map: dict[str, str],
    page_ids: set[str],
) -> list[SyntaxDeprecation]:
    """Перенести рекомендации замены в ID объединённой справки."""
    single_source = len(source.platforms) == 1
    result = []
    for entry in item.deprecations:
        links = []
        for link in entry.replacements:
            source_target = link.target_id
            if not source_target and single_source:
                source_target = target_page_id(entry.source_id or item.id, link.href, page_ids)
            links.append(replace(link, target_id=id_map.get(source_target, "")))
        result.append(replace(
            entry, platform=entry.platform or source.max_platform,
            replacements=links,
        ))
    return result


def _same_element(candidate: SyntaxItem | None, item: SyntaxItem) -> SyntaxItem | None:
    """Тот ли это элемент, если совпал путь страницы.

    Путь — сильный признак: у полей таблиц запросов имя одно на десятки
    страниц (`<Имя измерения>`, `Регистратор`), и различить их больше нечем.
    Но путь переиспользуется: на нём может оказаться другой элемент, поэтому
    требуется совпадение хотя бы одного из имён.
    """
    if candidate is None:
        return None
    if candidate.name_ru == item.name_ru:
        return candidate
    if candidate.name_en and candidate.name_en == item.name_en:
        return candidate
    return None


def _free_id(merged: SyntaxIndex, wanted: str, platform: str) -> str:
    """Путь страницы совпадает у 70,9% элементов, а описывать может разное.

    Занятый путь — не повод потерять элемент: добавляем к нему версию справки,
    из которой он пришёл.
    """
    if wanted not in merged.items:
        return wanted
    return f"{wanted}@{platform}"


def _difference(old: SyntaxItem, base: SyntaxItem, platform: str) -> SyntaxFacts | None:
    """Чем справка `platform` расходится с базовой. Ничем — значит None."""
    # `signature()` для свойства отдаёт его имя — сравнивать нужно объявленные
    # варианты, иначе переименование выглядит сменой сигнатуры.
    old_signature = old.variants[0].signature if old.variants else ""
    base_signature = base.variants[0].signature if base.variants else ""
    signature = old_signature if old_signature != base_signature else ""
    availability = list(old.availability) if old.availability != base.availability else []
    name_ru = old.name_ru if old.name_ru != base.name_ru else ""
    if not signature and not availability and not name_ru:
        return None
    return SyntaxFacts(
        platform=platform,
        signature=signature,
        availability=availability,
        name_ru=name_ru,
    )
