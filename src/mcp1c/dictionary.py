"""Словарь: как говорят люди против того, как названо в конфигурации.

Два разных механизма, и путать их не надо.

**Синонимы слов** — общие для всех конфигураций. «Клиент», «покупатель» и
«контрагент» означают примерно одно везде, от Розницы до Бухгалтерии.
Работают на уровне токенов, помогают в любом запросе, но весят меньше прямого
совпадения: у слова «клиент» в 1С есть и второй смысл — клиентская часть
модуля.

**Псевдонимы объектов** — привязаны к конкретной конфигурации. «Справочник
физлиц» в одной базе означает `Справочник.ФизическиеЛица`, а в другой ещё и
`Справочник.Пользователи`. Это не синонимия слов, а прямое указание: когда я
говорю так — я имею в виду вот эти объекты. Поэтому вес максимальный, выше
любого текстового совпадения.

Файл лежит в каталоге данных, а не в коде: правится без пересборки образа и
перечитывается по `POST /admin/reload`. Встроенные группы служат основой,
пользовательский файл их дополняет и может переопределять.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable

from .synonyms import BUILTIN_ALIASES, SYNONYM_GROUPS

DICTIONARY_VERSION = 1
ANY_CONFIGURATION = "*"

# Происхождение правила. Общая терминология 1С живёт в коде и меняется
# релизом; знание про конкретное внедрение — в данных и меняется на месте.
# Разделение по природе знания, а не по удобству правки: то, что в базе
# заказчика под «физлицами» понимают ещё и пользователей, отправлять всем
# нельзя, а `Справочник.Файлы` одинаков везде.
SOURCE_BUILTIN = "встроенный"
SOURCE_LOCAL = "локальный —"
SOURCE_LOCAL_ANY = "локальный — все конфигурации"


def _normalize_phrase(text: str) -> str:
    return " ".join(text.lower().replace("ё", "е").split())


@dataclass(slots=True)
class Dictionary:
    """Пользовательский словарь поверх встроенного."""

    synonym_groups: list[list[str]] = field(default_factory=list)
    # конфигурация -> фраза -> полные имена объектов
    aliases: dict[str, dict[str, list[str]]] = field(default_factory=dict)
    path: Path | None = None

    # ------------------------------------------------------------- синонимы

    def synonyms(self, *, with_builtin: bool = True) -> dict[str, frozenset[str]]:
        """Слово -> чем его можно заменить."""
        table: dict[str, set[str]] = {}
        groups: Iterable[Iterable[str]] = (
            list(SYNONYM_GROUPS) + self.synonym_groups
            if with_builtin
            else self.synonym_groups
        )
        for group in groups:
            words = [w.lower().replace("ё", "е") for w in group if w.strip()]
            for word in words:
                table.setdefault(word, set()).update(w for w in words if w != word)
        return {word: frozenset(values) for word, values in table.items()}

    def add_synonyms(self, words: Iterable[str]) -> list[str]:
        group = sorted({w.strip().lower().replace("ё", "е") for w in words if w.strip()})
        if len(group) < 2:
            raise ValueError("В группе синонимов должно быть минимум два слова.")
        if group not in self.synonym_groups:
            self.synonym_groups.append(group)
        return group

    def remove_synonyms(self, words: Iterable[str]) -> bool:
        """Снять свою группу синонимов. Опознаётся по составу целиком.

        По составу, а не по номеру: номер сдвинется от любой правки соседей, а
        состав — то, что человек видит на экране. Частичное совпадение не
        считается: «возчик, перевозчик» — не та же группа, что «возчик,
        перевозчик, экспедитор», и снимать её по части слов значило бы
        угадывать.

        Встроенные группы отсюда не снимаются: они лежат в `synonyms.py`,
        меняются вместе с кодом и приезжают релизом. Снятие на месте разошлось бы
        с поставкой и вернулось при следующем обновлении.
        """
        нужное = sorted({w.strip().lower().replace("ё", "е") for w in words if w.strip()})
        for позиция, группа in enumerate(self.synonym_groups):
            if sorted(группа) == нужное:
                del self.synonym_groups[позиция]
                return True
        return False

    # ------------------------------------------------------------- псевдонимы

    def aliases_for(
        self, config: str | None, *, with_builtin: bool = True
    ) -> dict[str, list[str]]:
        """Псевдонимы для конфигурации.

        Порядок наложения — от общего к частному: встроенные, затем свои для
        всех конфигураций, затем свои для этой. Каждый следующий слой
        перекрывает предыдущий, поэтому руками всегда можно поправить
        встроенное значение.
        """
        merged: dict[str, list[str]] = {}
        if with_builtin:
            merged.update({k: list(v) for k, v in BUILTIN_ALIASES.items()})
        merged.update(self.aliases.get(ANY_CONFIGURATION, {}))
        if config:
            merged.update(self.aliases.get(config, {}))
        return merged

    def aliases_with_source(
        self, config: str | None = None
    ) -> dict[str, tuple[list[str], str]]:
        """Псевдонимы вместе с происхождением.

        Нужно при разборе «почему поиск так себя ведёт»: сразу видно, чьё
        правило сработало — общее из поставки или заведённое на этой машине.
        """
        result: dict[str, tuple[list[str], str]] = {
            phrase: (list(targets), SOURCE_BUILTIN)
            for phrase, targets in BUILTIN_ALIASES.items()
        }
        for phrase, targets in self.aliases.get(ANY_CONFIGURATION, {}).items():
            source = SOURCE_LOCAL_ANY
            if phrase in BUILTIN_ALIASES:
                source += " (перекрывает встроенный)"
            result[phrase] = (list(targets), source)
        if config:
            for phrase, targets in self.aliases.get(config, {}).items():
                source = f"{SOURCE_LOCAL} {config}"
                if phrase in BUILTIN_ALIASES:
                    source += " (перекрывает встроенный)"
                result[phrase] = (list(targets), source)
        return result

    def add_alias(
        self, phrase: str, targets: Iterable[str], config: str | None = None
    ) -> tuple[str, list[str]]:
        key = _normalize_phrase(phrase)
        if not key:
            raise ValueError("Пустая фраза псевдонима.")
        names = [t.strip() for t in targets if t.strip()]
        if not names:
            raise ValueError("Не указано ни одного объекта.")
        scope = config or ANY_CONFIGURATION
        self.aliases.setdefault(scope, {})[key] = names
        return key, names

    def remove_alias(self, phrase: str, config: str | None = None) -> bool:
        scope = config or ANY_CONFIGURATION
        return self.aliases.get(scope, {}).pop(_normalize_phrase(phrase), None) is not None

    # ------------------------------------------------------------- хранение

    @classmethod
    def load(cls, path: str | Path) -> "Dictionary":
        target = Path(path)
        if not target.exists():
            return cls(path=target)
        payload = json.loads(target.read_text(encoding="utf-8"))
        return cls(
            synonym_groups=[list(g) for g in payload.get("synonym_groups") or []],
            aliases={
                scope: {_normalize_phrase(k): list(v) for k, v in phrases.items()}
                for scope, phrases in (payload.get("aliases") or {}).items()
            },
            path=target,
        )

    def save(self, path: str | Path | None = None) -> Path:
        target = Path(path) if path else self.path
        if target is None:
            raise ValueError("Не задан путь для сохранения словаря.")
        target.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "dictionary_version": DICTIONARY_VERSION,
            "synonym_groups": self.synonym_groups,
            "aliases": self.aliases,
        }
        tmp = target.with_suffix(".tmp")
        tmp.write_text(
            json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8"
        )
        tmp.replace(target)
        self.path = target
        return target

    def stats(self) -> dict[str, int]:
        return {
            "своих групп синонимов": len(self.synonym_groups),
            "встроенных групп синонимов": len(SYNONYM_GROUPS),
            "встроенных псевдонимов": len(BUILTIN_ALIASES),
            "конфигураций с псевдонимами": len(self.aliases),
            "псевдонимов": sum(len(v) for v in self.aliases.values()),
        }
