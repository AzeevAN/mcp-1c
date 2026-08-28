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

import os
import re
import shutil
import sys
import tempfile
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

# Самостоятельные модули не разделяют ядро schema v1 и не входят в два EPF,
# которые собираются вручную. Сборщик лишь публикует проверенную копию из src/:
# так новый runtime-источник не делает существующие EPF устаревшими.
STANDALONE = {
    "extension_runtime_managed_json.bsl": "СнимокРасширений_УправляемаяФорма_JSON.bsl",
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


def publish(results: dict[str, str]) -> None:
    """Опубликовать проверенный набор; при сбое вернуть прежние файлы."""
    DIST.parent.mkdir(parents=True, exist_ok=True)
    dist_existed = DIST.exists()

    with tempfile.TemporaryDirectory(prefix=".dist-build-", dir=DIST.parent) as raw_temp:
        temp = Path(raw_temp)
        staged = temp / "staged"
        backup = temp / "backup"
        staged.mkdir()
        backup.mkdir()

        # Любая ошибка здесь оставляет dist нетронутым: публикация ещё не началась.
        for out_name, result in results.items():
            (staged / out_name).write_text(result, encoding="utf-8")

        if DIST.exists() and not DIST.is_dir():
            raise OSError(f"путь результата не является каталогом: {DIST}")
        DIST.mkdir(parents=True, exist_ok=True)

        existing: set[str] = set()
        for out_name in results:
            target = DIST / out_name
            if target.exists():
                shutil.copy2(target, backup / out_name)
                existing.add(out_name)

        replaced: list[str] = []
        try:
            # Переносимого вызова для замены четырёх файлов разом нет: каждый
            # target меняется атомарно, а сбой набора откатывает уже заменённые.
            for out_name in results:
                os.replace(staged / out_name, DIST / out_name)
                replaced.append(out_name)
        except OSError as publish_error:
            rollback_errors: list[str] = []
            for out_name in reversed(replaced):
                target = DIST / out_name
                try:
                    if out_name in existing:
                        os.replace(backup / out_name, target)
                    else:
                        target.unlink(missing_ok=True)
                except OSError as rollback_error:
                    rollback_errors.append(f"{out_name}: {rollback_error}")

            if not dist_existed:
                try:
                    DIST.rmdir()
                except OSError:
                    pass

            if rollback_errors:
                detail = "; ".join(rollback_errors)
                raise OSError(
                    f"{publish_error}; откат завершён не полностью: {detail}"
                ) from publish_error
            raise


def build() -> int:
    core_path = SRC / "core.bsl"
    if not core_path.exists():
        print(f"НЕ НАЙДЕНО: {core_path}", file=sys.stderr)
        return 1

    core_text = core_path.read_text(encoding="utf-8")
    problems: list[str] = []
    results: dict[str, str] = {}

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

        results[out_name] = result

        форма = "управляемая" if managed else "обычная"
        форматы = "XML + JSON" if with_json else "только XML"
        print(f"  {out_name:32} {форма:12} {форматы:12} {len(result.splitlines()):>5} строк")

    for source_name, out_name in STANDALONE.items():
        source_path = SRC / source_name
        if not source_path.exists():
            print(f"НЕ НАЙДЕНО: {source_path}", file=sys.stderr)
            return 1
        result = source_path.read_text(encoding="utf-8")
        if "ИсточникРасширенийКонфигурации.СеансАктивные" not in result:
            print(
                f"ОШИБКА: {source_name} не читает действующие расширения сеанса",
                file=sys.stderr,
            )
            return 1
        results[out_name] = result
        print(
            f"  {out_name:32} {'управляемая':12} {'JSON, 8.3.8+':12} "
            f"{len(result.splitlines()):>5} строк"
        )

    if problems:
        print("\nОШИБКА: XML-вариант не соберётся на 8.3.5:", file=sys.stderr)
        print("\n".join(problems), file=sys.stderr)
        return 1

    try:
        publish(results)
    except OSError as error:
        print(f"\nОШИБКА: набор dist не опубликован: {error}", file=sys.stderr)
        return 1

    print("\nПроверка пройдена: XML-варианты совместимы с 8.3.5 "
          "(нет JSON и функций 8.3.6).")
    print(f"Каталог: {DIST}")
    return 0


if __name__ == "__main__":
    sys.exit(build())
