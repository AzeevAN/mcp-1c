"""Закрытый каталог доказанных стандартных команд управляемой формы."""

from __future__ import annotations


FORM_STANDARD_COMMANDS = frozenset({"Help", "Close", "CustomizeForm"})
ITEM_STANDARD_COMMANDS = {
    "table": frozenset({"Add", "Delete", "MoveUp", "MoveDown"}),
}


def standard_command_supported(owner_kind: str, command: str) -> bool:
    """Проверить команду для формы либо конкретного вида элемента."""

    if owner_kind == "form":
        return command in FORM_STANDARD_COMMANDS
    return command in ITEM_STANDARD_COMMANDS.get(owner_kind, frozenset())


__all__ = [
    "FORM_STANDARD_COMMANDS",
    "ITEM_STANDARD_COMMANDS",
    "standard_command_supported",
]
