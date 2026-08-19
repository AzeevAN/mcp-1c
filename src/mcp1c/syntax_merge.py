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

from .syntax_model import SyntaxFacts, SyntaxIndex, SyntaxItem, parse_version


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
    # Копии, а не сами элементы: разобранные справки версий реестр держит и
    # пересобирает слитый вид при каждой загрузке. Правка на месте удвоила бы
    # факты на втором слиянии и испортила бы исходную справку.
    for item in base.items.values():
        merged.add(_copy(item))

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
        # Внутри одной справки одинаковый ключ встречается у разных страниц —
        # 176 таких ключей в 8.3.5, это поля таблиц запросов. Схлопывать их
        # между собой нельзя: они описывают разные поля.
        claimed: set[tuple[str, str, str]] = set()
        for item in index.items.values():
            key_en = _key_en(item)
            current = _same_element(known_id.get(item.id), item)
            if current is None and _key(item) not in claimed:
                current = known.get(_key(item))
                if current is None and key_en is not None:
                    current = known_en.get(key_en)
            claimed.add(_key(item))
            if current is None:
                copy = _copy(item, id=_free_id(merged, item.id, platform), until=platform)
                merged.add(copy)
                known.setdefault(_key(copy), copy)
                known_id.setdefault(copy.id, copy)
                if key_en is not None:
                    known_en.setdefault(key_en, copy)
                continue
            facts = _difference(item, current, platform)
            if facts is not None:
                current.older.insert(0, facts)

    return merged


def _copy(item: SyntaxItem, **changes) -> SyntaxItem:
    """Копия элемента со своим списком версионных фактов."""
    return replace(item, older=list(item.older), **changes)


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
