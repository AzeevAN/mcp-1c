"""Хранение разобранных индексов на диске.

Разбор справки платформы занимает около двух секунд и 160 МБ — держать это
на каждом старте сервера незачем. Выгрузка конфигурации читается за 0,15 с,
но кладётся сюда же, чтобы у реестра был один способ хранить источники.

Формат — gzip + JSON. Не бинарный pickle: индекс должен переживать смену
версии Python и читаться глазами при разборе проблем.

Правило действует именно здесь, потому что это **канонический** артефакт:
единственный носитель данных, когда `.hbk` под рукой нет. Для производных
данных оно другое — построенные поисковые индексы кэшируются в `marshal`
(`index_cache.py`), их не читают глазами и восстанавливают за секунду.
"""

from __future__ import annotations

import gzip
import json
from dataclasses import asdict
from pathlib import Path
from typing import Any

from .syntax_model import (
    SyntaxFacts,
    SyntaxIndex,
    SyntaxItem,
    SyntaxParam,
    SyntaxVariant,
)

STORE_VERSION = 1


class StoreError(Exception):
    """Индекс не читается или несовместим."""


def _write(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with gzip.open(tmp, "wt", encoding="utf-8", compresslevel=6) as stream:
        json.dump(payload, stream, ensure_ascii=False, separators=(",", ":"))
    tmp.replace(path)


def _read(path: Path) -> dict[str, Any]:
    if not path.exists():
        raise StoreError(f"Индекс не найден: {path}")
    with gzip.open(path, "rt", encoding="utf-8") as stream:
        payload = json.load(stream)
    version = payload.get("store_version")
    if version != STORE_VERSION:
        raise StoreError(
            f"Индекс {path} версии {version}, ожидается {STORE_VERSION}. "
            "Пересоберите его из исходника."
        )
    return payload


# --------------------------------------------------------------- справка


def save_syntax(index: SyntaxIndex, path: str | Path) -> Path:
    payload = {
        "store_version": STORE_VERSION,
        "kind": "syntax",
        "platforms": index.platforms,
        "language": index.language,
        "source": index.source,
        # Разбор говорит о битой разметке один раз, при чтении исходника.
        # Не сохранив это, после перезапуска сервер поднимет тот же индекс
        # из файла, и справка будет выглядеть целой.
        "warnings": index.warnings,
        "items": [asdict(item) for item in index.items.values()],
    }
    target = Path(path)
    _write(target, payload)
    return target


def load_syntax(path: str | Path) -> SyntaxIndex:
    payload = _read(Path(path))
    if payload.get("kind") != "syntax":
        raise StoreError(f"{path} — не индекс справки.")

    index = SyntaxIndex(
        platforms=list(payload.get("platforms") or []),
        language=payload.get("language", "ru"),
        source=payload.get("source", ""),
        warnings=list(payload.get("warnings") or []),
    )
    for raw in payload.get("items") or []:
        index.add(_item_from_dict(raw))
    return index


def _item_from_dict(raw: dict[str, Any]) -> SyntaxItem:
    variants = [
        SyntaxVariant(
            title=v.get("title", ""),
            signature=v.get("signature", ""),
            params=[SyntaxParam(**p) for p in v.get("params") or []],
            returns=list(v.get("returns") or []),
            returns_description=v.get("returns_description", ""),
            description=v.get("description", ""),
        )
        for v in raw.get("variants") or []
    ]
    # Границы версий и расхождения со старыми справками появляются при
    # слиянии. Терять их при чтении нельзя: слитый вид хранится этим же
    # форматом, и после перезапуска сервер снова отдавал бы сигнатуру свежей
    # справки всем конфигурациям.
    older = [
        SyntaxFacts(
            platform=f.get("platform", ""),
            signature=f.get("signature", ""),
            availability=list(f.get("availability") or []),
            name_ru=f.get("name_ru", ""),
        )
        for f in raw.get("older") or []
    ]
    return SyntaxItem(
        id=raw["id"],
        kind=raw["kind"],
        name_ru=raw.get("name_ru", ""),
        name_en=raw.get("name_en", ""),
        parent_ru=raw.get("parent_ru", ""),
        parent_en=raw.get("parent_en", ""),
        description=raw.get("description", ""),
        availability=list(raw.get("availability") or []),
        since=raw.get("since", ""),
        until=raw.get("until", ""),
        older=older,
        variants=variants,
        examples=list(raw.get("examples") or []),
        see_also=list(raw.get("see_also") or []),
        members={k: list(v) for k, v in (raw.get("members") or {}).items()},
        values=list(raw.get("values") or []),
        readonly=raw.get("readonly"),
        note=raw.get("note", ""),
    )
