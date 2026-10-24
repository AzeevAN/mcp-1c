"""Общие чистые helpers форматирования ответов инструментов."""

from __future__ import annotations


MAX_LIMIT = 50


def _render_notes(notes: list[str]) -> str:
    if not notes:
        return ""
    lines = ["", "---", "> **Оговорки по источнику данных**"]
    lines += [f"> - {note}" for note in notes]
    return "\n".join(lines) + "\n"


def _notes_block(context, *, critical_only: bool = True) -> str:
    return _render_notes(context.notes(critical_only=critical_only))


def _clamp(limit: int) -> int:
    return max(1, min(int(limit or 10), MAX_LIMIT))
