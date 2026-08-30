"""Поисковые формулировки для методов справки платформы.

Слой нужен там, где человек описывает задачу словами, не совпадающими с
именем элемента. Ключи написаны нами, не входят в карточку и проверяются по
идентификатору страницы при каждой загрузке справки.
"""

from __future__ import annotations

from dataclasses import dataclass, field


SEARCH_KEYS: dict[str, tuple[str, ...]] = {
    "objects/catalog213/catalog393/QueryResultSelection/methods/Next556": (
        "перебрать строки в выборке результата запроса",
    ),
}


@dataclass(slots=True)
class KeyCoverage:
    """Сколько поисковых ключей привязалось к текущей справке."""

    total: int = 0
    attached: int = 0
    lost: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.lost

    def as_warning(self) -> str:
        if self.ok:
            return ""
        примеры = ", ".join(sorted(self.lost)[:5])
        хвост = " и ещё…" if len(self.lost) > 5 else ""
        return (
            f"поисковых ключей {self.total}, привязано {self.attached}, "
            f"потеряно {len(self.lost)}: {примеры}{хвост}. Набор страниц "
            "справки не совпал с тем, под который ключи писались — поиск по "
            "методам платформы работает хуже заявленного."
        )


def coverage(item_ids: object) -> KeyCoverage:
    """Сверить таблицу ключей с идентификаторами одной версии справки."""
    известные = set(item_ids)  # type: ignore[arg-type]
    lost = [key for key in SEARCH_KEYS if key not in известные]
    return KeyCoverage(
        total=len(SEARCH_KEYS),
        attached=len(SEARCH_KEYS) - len(lost),
        lost=lost,
    )


def keys_text(item_id: str) -> str:
    """Ключи элемента одной строкой для поля индекса. Пусто — ключей нет."""
    return "\n".join(SEARCH_KEYS.get(item_id, ()))
