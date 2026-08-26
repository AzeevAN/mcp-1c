"""Границы ресурса для поисковой фразы и общего кэша стемов."""

import pytest

from mcp1c import search
from mcp1c.search import Doc, SearchIndex


MAX_QUERY_CHARS = 4096
MAX_QUERY_TOKENS = 32
STEM_CACHE_LIMIT = 4096


def _words(count: int) -> list[str]:
    """Различные слова без цифр и CamelCase, то есть ровно по одному токену."""
    return ["слово" + chr(1072 + index // 32) + chr(1072 + index % 32)
            for index in range(count)]


def test_search_accepts_32_distinct_tokens() -> None:
    words = _words(MAX_QUERY_TOKENS)
    index = SearchIndex([Doc(id="all", fields={"name": " ".join(words)})])

    assert [hit.doc.id for hit in index.search(" ".join(words))] == ["all"]


def test_search_rejects_33_distinct_tokens_with_clear_error() -> None:
    words = _words(MAX_QUERY_TOKENS + 1)
    index = SearchIndex([Doc(id="all", fields={"name": " ".join(words)})])

    with pytest.raises(ValueError, match=r"не более 32 различных токенов"):
        index.search(" ".join(words))


def test_search_rejects_query_above_character_limit() -> None:
    index = SearchIndex([])

    assert index.search("я" * MAX_QUERY_CHARS) == []
    with pytest.raises(ValueError, match=r"не более 4096 символов"):
        index.search("я" * (MAX_QUERY_CHARS + 1))


def test_stem_cache_is_bounded_lru() -> None:
    first = "первыйкэш"
    second = "второйкэш"
    search._STEM_CACHE.clear()
    try:
        search.stem(first)
        search.stem(second)
        for index in range(STEM_CACHE_LIMIT - 2):
            search.stem(f"уникальныйкэш{index}")

        # Обновляем первый элемент: следующим должен уйти второй, а не первый.
        search.stem(first)
        search.stem("переполнениеочереди")

        assert len(search._STEM_CACHE) == STEM_CACHE_LIMIT
        assert first in search._STEM_CACHE
        assert second not in search._STEM_CACHE
    finally:
        search._STEM_CACHE.clear()
