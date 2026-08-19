"""Сколько индекс сигнатур стоит в рабочем режиме: сборка, выгрузка, подъём.

Пик разбора и резидентный размер — разные величины. Решение про отдельный
контейнер принимается по второй.
"""
import gc
import marshal
import re
import resource
import subprocess
import sys
import time
from pathlib import Path

КОРЕНЬ = Path(sys.argv[1])
ФАЙЛ = Path(sys.argv[2])
ЭТАП = sys.argv[3] if len(sys.argv) > 3 else "собрать"

ОБЪЯВЛЕНИЕ = re.compile(
    r"^\s*(Процедура|Функция|Procedure|Function)\s+([А-Яа-яЁёA-Za-z_][\w]*)\s*\(([^)]*)\)\s*(Экспорт|Export)?",
    re.IGNORECASE | re.MULTILINE,
)


def rss_мб() -> float:
    """Текущий RSS процесса, а не пик: `ru_maxrss` пик не отпускает."""
    вывод = subprocess.run(["ps", "-o", "rss=", "-p", str(__import__("os").getpid())],
                           capture_output=True, text=True).stdout.strip()
    return int(вывод) / 1024


if ЭТАП == "собрать":
    индекс = []
    начало = time.monotonic()
    for путь in sorted(КОРЕНЬ.rglob("*.bsl")):
        текст = путь.read_text(encoding="utf-8-sig", errors="replace")
        модуль = str(путь.relative_to(КОРЕНЬ))
        for m in ОБЪЯВЛЕНИЕ.finditer(текст):
            индекс.append((модуль, m.group(2), m.group(3).strip(),
                           bool(m.group(4)), текст.count("\n", 0, m.start()) + 1))
        del текст
    сборка = time.monotonic() - начало
    gc.collect()
    print(f"записей          : {len(индекс):,}".replace(",", " "))
    print(f"сборка           : {сборка:.1f} с")
    print(f"RSS с индексом   : {rss_мб():.0f} МБ")
    ФАЙЛ.write_bytes(marshal.dumps(индекс))
    print(f"на диске         : {ФАЙЛ.stat().st_size / 1024 / 1024:.1f} МБ")
else:
    пустой = rss_мб()
    начало = time.monotonic()
    индекс = marshal.loads(ФАЙЛ.read_bytes())
    подъём = time.monotonic() - начало
    gc.collect()
    print(f"записей          : {len(индекс):,}".replace(",", " "))
    print(f"подъём с диска   : {подъём:.2f} с")
    print(f"RSS пустой       : {пустой:.0f} МБ")
    print(f"RSS с индексом   : {rss_мб():.0f} МБ")
    print(f"цена индекса     : {rss_мб() - пустой:.0f} МБ")
