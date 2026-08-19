#!/usr/bin/env python3
"""Сборка модулей обработки выгрузки структуры конфигурации 1С.

Ядро (обход метаданных + сериализаторы) живёт в единственном экземпляре —
src/core.bsl. Варианты обработки содержат только слой взаимодействия с формой
и маркер `// {{CORE}}`, вместо которого подставляется ядро.

ВАЖНО про JSON. Типы ЗаписьJSON / ПараметрыЗаписиJSON / ПереносСтрокJSON и
процедура ЗаписатьJSON появились в платформе 8.3.6. На 8.3.5 обращение к ним
рвёт КОМПИЛЯЦИЮ всего модуля, а не выполнение. Поэтому в XML-варианты
JSON-код не попадает вообще: он размечен в исходниках блоками

    // {{#json}}   ... код, требующий 8.3.6 ...   // {{/json}}
    // {{#xmlonly}} ... замена для XML-варианта ... // {{/xmlonly}}

и вырезается при сборке. Проверка результата — в конце сборки: в
XML-вариантах не должно остаться ни одного упоминания JSON.

Директива &НаСервереБезКонтекста проставляется перед процедурами ядра только
для вариантов с управляемой формой.

Запуск:
    python3 exporter-1c/build_modules.py
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

SRC = Path(__file__).parent / "src"
DIST = Path(__file__).parent / "dist"

CORE_MARKER = "// {{CORE}}"
PROC_START = re.compile(r"^(Функция|Процедура)\s", re.MULTILINE)

JSON_BLOCK = re.compile(
    r"^[ \t]*//[ \t]*\{\{#json\}\}[ \t]*\n(.*?)^[ \t]*//[ \t]*\{\{/json\}\}[ \t]*\n",
    re.DOTALL | re.MULTILINE,
)
XMLONLY_BLOCK = re.compile(
    r"^[ \t]*//[ \t]*\{\{#xmlonly\}\}[ \t]*\n(.*?)^[ \t]*//[ \t]*\{\{/xmlonly\}\}[ \t]*\n",
    re.DOTALL | re.MULTILINE,
)

# Всё, что не должно попасть в XML-вариант.
JSON_TOKENS = ("ЗаписьJSON", "ЗаписатьJSON", "ПараметрыЗаписиJSON", "ПереносСтрокJSON", "JSON")

# Встроенные функции, появившиеся в 8.3.6. XML-варианты собираются под 8.3.5,
# поэтому обращение к ним рвёт компиляцию модуля.
#
# Список сверен со справкой 2026-08-17. `СтрСравнить` отсюда убрана: она
# появилась в 8.3.5 и на целевой платформе работает — запрет был написан по
# памяти, когда справку ещё не умели разбирать. Каждой оставшейся функции
# соответствует рецепт замены в `src/mcp1c/replacements.py`, это проверяется
# тестом.
PLATFORM_836_TOKENS = (
    "СтрРазделить",
    "СтрСоединить",
    "СтрНайти",
    "СтрШаблон",
    "СтрНачинаетсяС",
    "СтрЗаканчиваетсяНа",
)

# исходник -> (выходной файл, управляемая форма, поддержка JSON)
VARIANTS = {
    "variant_ordinary_xml.bsl": ("ОбычнаяФорма_XML.bsl", False, False),
    "variant_ordinary_json.bsl": ("ОбычнаяФорма_JSON.bsl", False, True),
    "variant_managed_xml.bsl": ("УправляемаяФорма_XML.bsl", True, False),
    "variant_managed_both.bsl": ("УправляемаяФорма_XML_JSON.bsl", True, True),
}


def apply_capabilities(text: str, with_json: bool) -> str:
    """Оставить или вырезать блоки, зависящие от поддержки JSON платформой."""
    if with_json:
        text = JSON_BLOCK.sub(lambda m: m.group(1), text)
        text = XMLONLY_BLOCK.sub("", text)
    else:
        text = JSON_BLOCK.sub("", text)
        text = XMLONLY_BLOCK.sub(lambda m: m.group(1), text)
    return text


def annotate(text: str) -> str:
    """Проставить &НаСервереБезКонтекста перед каждой процедурой/функцией ядра."""
    return PROC_START.sub(lambda m: "&НаСервереБезКонтекста\n" + m.group(0), text)


def check_legacy_safe(text: str, name: str) -> list[str]:
    """XML-вариант обязан компилироваться на 8.3.5: ни JSON, ни функций 8.3.6."""
    hits = []
    for number, line in enumerate(text.splitlines(), start=1):
        lowered = line.lower()
        if any(token.lower() in lowered for token in JSON_TOKENS):
            hits.append(f"    {name}:{number}: [JSON] {line.strip()}")
        elif any(token in line for token in PLATFORM_836_TOKENS):
            hits.append(f"    {name}:{number}: [8.3.6] {line.strip()}")
    return hits


def build() -> int:
    core_path = SRC / "core.bsl"
    if not core_path.exists():
        print(f"НЕ НАЙДЕНО: {core_path}", file=sys.stderr)
        return 1

    core_text = core_path.read_text(encoding="utf-8")
    DIST.mkdir(parents=True, exist_ok=True)

    problems: list[str] = []

    for variant_name, (out_name, managed, with_json) in VARIANTS.items():
        variant_path = SRC / variant_name
        if not variant_path.exists():
            print(f"НЕ НАЙДЕНО: {variant_path}", file=sys.stderr)
            return 1

        variant_text = variant_path.read_text(encoding="utf-8")
        if CORE_MARKER not in variant_text:
            print(f"В {variant_name} нет маркера {CORE_MARKER}", file=sys.stderr)
            return 1

        core_block = apply_capabilities(core_text, with_json)
        if managed:
            core_block = annotate(core_block)

        result = apply_capabilities(variant_text, with_json)
        result = result.replace(CORE_MARKER, core_block)

        if not with_json:
            problems.extend(check_legacy_safe(result, out_name))

        (DIST / out_name).write_text(result, encoding="utf-8")

        форма = "управляемая" if managed else "обычная"
        форматы = "XML + JSON" if with_json else "только XML"
        print(f"  {out_name:32} {форма:12} {форматы:12} {len(result.splitlines()):>5} строк")

    if problems:
        print("\nОШИБКА: XML-вариант не соберётся на 8.3.5:", file=sys.stderr)
        print("\n".join(problems), file=sys.stderr)
        return 1

    print("\nПроверка пройдена: XML-варианты совместимы с 8.3.5 "
          "(нет JSON и функций 8.3.6).")
    print(f"Каталог: {DIST}")
    return 0


if __name__ == "__main__":
    sys.exit(build())
