"""Цена поиска по 136 909 процедурам — настоящим `SearchIndex`, не игрушкой.

Меряем: сборку, резидентную память, латентность запроса. Хранение сигнатур
измерено отдельно (65 МБ) — здесь именно надстройка поиска.
"""
import gc
import marshal
import os
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, "src")
from mcp1c.search import Doc, SearchIndex, split_identifier


def rss_мб() -> float:
    вывод = subprocess.run(["ps", "-o", "rss=", "-p", str(os.getpid())],
                           capture_output=True, text=True).stdout.strip()
    return int(вывод) / 1024


ФАЙЛ = Path(sys.argv[1])
записи = marshal.loads(ФАЙЛ.read_bytes())
ТОЛЬКО_ЭКСПОРТНЫЕ = len(sys.argv) > 2 and sys.argv[2] == "экспортные"
if ТОЛЬКО_ЭКСПОРТНЫЕ:
    записи = [r for r in записи if r[3]]
    print("отбор: только экспортные")
gc.collect()
после_записей = rss_мб()
print(f"записей          : {len(записи):,}".replace(",", " "))
print(f"RSS с записями   : {после_записей:.0f} МБ")

начало = time.monotonic()
индекс = SearchIndex(field_weights={"name": 1.0, "module": 0.4})
for модуль, имя, параметры, экспорт, строка in записи:
    # Имя процедуры разбирается на слова: `ТаблицаМаксимальныхСуммОплаты`
    # без этого не найдётся ни по одному слову запроса.
    индекс.add(
        Doc(
            id=f"{модуль}#{имя}",
            fields={"name": " ".join(split_identifier(имя)), "module": модуль},
            kind="routine",
            exact_keys=[имя],
            payload=(модуль, строка, экспорт),
        )
    )
сборка = time.monotonic() - начало
gc.collect()
после_индекса = rss_мб()

# Прогрев: `_finalize` строит постинги массивами.
индекс.search("таблица сумм оплаты", limit=5)
gc.collect()
после_постингов = rss_мб()

запросы = [
    "заполнить список выбора",
    "провести документ чек ккм",
    "бонусные баллы начисление",
    "печать этикетки штрихкод",
    "проверка заполнения реквизитов",
]
замеры = []
for q in запросы:
    t = time.monotonic()
    hits = индекс.search(q, limit=10)
    замеры.append((time.monotonic() - t) * 1000)
    первый = hits[0].doc.id if hits else "—"
    print(f"  «{q}» -> {len(hits)} шт, первый {первый[:70]}")

print()
print(f"сборка индекса   : {сборка:.1f} с")
print(f"RSS после сборки : {после_индекса:.0f} МБ  (+{после_индекса - после_записей:.0f})")
print(f"RSS с постингами : {после_постингов:.0f} МБ  (+{после_постингов - после_индекса:.0f})")
print(f"цена поиска всего: {после_постингов - после_записей:.0f} МБ")
print(f"латентность      : медиана {sorted(замеры)[len(замеры)//2]:.1f} мс, "
      f"худшая {max(замеры):.1f} мс")
