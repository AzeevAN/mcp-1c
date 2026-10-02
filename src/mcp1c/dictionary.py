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
import os
import stat
import uuid
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


class DictionaryError(Exception):
    """Файл словаря не читается или правку нельзя записать.

    Отдельный тип, а не ``ValueError``: HTTP-ручки отдают ``ValueError``
    кодом 422 как ошибку ввода, а здесь дело в файле на сервере.
    """


def _normalize_phrase(text: str) -> str:
    return " ".join(text.lower().replace("ё", "е").split())


def _parse_payload(
    payload: object,
) -> tuple[list[list[str]], dict[str, dict[str, list[str]]]]:
    """Проверить форму файла до построения объекта.

    Без проверки ``{"aliases": [1]}`` падал AttributeError глубоко внутри
    разбора, а строка вместо списка слов молча становилась группой букв.
    """
    if not isinstance(payload, dict):
        raise ValueError("корень должен быть объектом JSON")
    raw_groups = payload.get("synonym_groups") or []
    if not isinstance(raw_groups, list) or not all(
        isinstance(group, list) and all(isinstance(word, str) for word in group)
        for group in raw_groups
    ):
        raise ValueError("synonym_groups должен быть списком списков строк")
    raw_aliases = payload.get("aliases") or {}
    if not isinstance(raw_aliases, dict) or not all(
        isinstance(scope, str)
        and isinstance(phrases, dict)
        and all(
            isinstance(phrase, str)
            and isinstance(targets, list)
            and all(isinstance(target, str) for target in targets)
            for phrase, targets in phrases.items()
        )
        for scope, phrases in raw_aliases.items()
    ):
        raise ValueError(
            "aliases должен быть объектом «конфигурация -> фраза -> список имён»"
        )
    return (
        [list(group) for group in raw_groups],
        {
            scope: {_normalize_phrase(k): list(v) for k, v in phrases.items()}
            for scope, phrases in raw_aliases.items()
        },
    )


def _write_durably(target: Path, text: str) -> None:
    """tmp + fsync файла + replace + fsync каталога с прежними правами.

    Без fsync после сбоя питания rename может оказаться на диске раньше
    содержимого, и вместо словаря остаётся пустой файл. ``NamedTemporaryFile``
    создавал 0600, и replace переносил эти права на словарь: хост-пользователь
    смонтированного тома терял право его читать. Существующий файл сохраняет
    свои права, новый получает обычные 0666 с поправкой на umask.
    """
    try:
        mode = stat.S_IMODE(target.stat().st_mode)
    except FileNotFoundError:
        mode = None
    # Уникальное имя: общий `dictionary.tmp` сталкивался у параллельных
    # CLI/HTTP writers — первый replace уносил файл, второй получал
    # FileNotFoundError.
    temporary = target.with_name(f".{target.name}.{uuid.uuid4().hex}.tmp")
    try:
        descriptor = os.open(
            temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o666
        )
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            if mode is not None:
                os.fchmod(stream.fileno(), mode)
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        temporary.replace(target)
        directory = os.open(target.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        temporary.unlink(missing_ok=True)


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

    def copy(self) -> "Dictionary":
        """Независимая копия: правка применяется к ней и подменяет живой
        объект только после успешной записи на диск."""
        return Dictionary(
            synonym_groups=[list(group) for group in self.synonym_groups],
            aliases={
                scope: {phrase: list(targets) for phrase, targets in phrases.items()}
                for scope, phrases in self.aliases.items()
            },
            path=self.path,
        )

    # ------------------------------------------------------------- хранение

    @classmethod
    def load(cls, path: str | Path) -> "Dictionary":
        target = Path(path)
        if not target.exists():
            return cls(path=target)
        # Пустой или обрезанный файл после сбоя питания либо опечатка при
        # ручной правке раньше роняли и сервер, и CLI голым traceback.
        try:
            payload = json.loads(target.read_text(encoding="utf-8"))
            groups, aliases = _parse_payload(payload)
        except (OSError, UnicodeDecodeError, ValueError) as error:
            # Имя без каталога: текст уходит и в HTTP-ответ дашборда, а
            # пути сервера наружу не показываются.
            причина = (
                error.strerror or type(error).__name__
                if isinstance(error, OSError)
                else str(error)
            )
            raise DictionaryError(
                f"Локальный словарь {target.name} не читается: {причина}. Исправьте "
                "файл или уберите его и выполните перезагрузку."
            ) from error
        return cls(synonym_groups=groups, aliases=aliases, path=target)

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
        text = json.dumps(payload, ensure_ascii=False, indent=1)
        _write_durably(target, text)
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
