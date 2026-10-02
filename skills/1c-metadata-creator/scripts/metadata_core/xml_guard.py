"""Единственное место правила «DTD и ENTITY в XML запрещены» для metadata_core и metadata_creator.py."""

import re

# `\s*` после `<!` оставлен намеренно: строгая проверка не должна зависеть от того,
# как разборщик XML отнесётся к нестандартной записи.
FORBIDDEN_DECLARATION = re.compile(r"<!\s*(?:DOCTYPE|ENTITY)\b", re.IGNORECASE)
