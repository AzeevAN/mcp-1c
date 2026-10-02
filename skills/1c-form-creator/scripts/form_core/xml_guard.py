"""Единственное место правила «DTD и ENTITY в XML запрещены» для form_core и form_creator.py."""

import re

# `\s*` после `<!` оставлен намеренно: строгая проверка не должна зависеть от того,
# как разборщик XML отнесётся к нестандартной записи.
FORBIDDEN_DOCTYPE = re.compile(r"<!\s*DOCTYPE\b", re.IGNORECASE)
FORBIDDEN_ENTITY = re.compile(r"<!\s*ENTITY\b", re.IGNORECASE)
FORBIDDEN_DECLARATION = re.compile(r"<!\s*(?:DOCTYPE|ENTITY)\b", re.IGNORECASE)
