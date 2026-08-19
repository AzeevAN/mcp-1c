"""Замер плоской выгрузки (8.3.5): модули в `.txt`, код форм в контейнерах.

Второй формат выгрузки конфигурации в файлы. Отличается от иерархического
не только раскладкой: модуль обычной формы лежит записью `module` внутри
двоичного контейнера `.Form`, а часть общих модулей поставляется
скомпилированными — исходника в них нет вовсе.

Запуск: python3 tools/lab/measure_flat.py <каталог плоской выгрузки>
"""

import marshal
import re
import sys
import time
from collections import Counter
from pathlib import Path

sys.path.insert(0, "src")
from mcp1c.v8container import V8Container  # noqa: E402

КОРЕНЬ = Path(sys.argv[1])
ВЫХОД = Path(sys.argv[2]) if len(sys.argv) > 2 else None

ОБЪЯВЛЕНИЕ = re.compile(
    r"^\s*(Процедура|Функция|Procedure|Function)\s+([А-Яа-яЁёA-Za-z_][\w]*)\s*\(([^)]*)\)\s*(Экспорт|Export)?",
    re.IGNORECASE | re.MULTILINE,
)

индекс: list[tuple] = []
виды = Counter()
строк = 0
байт = 0
защищённых = []
форм_всего = форм_с_кодом = форм_пустых = 0

начало = time.monotonic()


def собрать(модуль: str, текст: str) -> int:
    было = len(индекс)
    for m in ОБЪЯВЛЕНИЕ.finditer(текст):
        индекс.append(
            (модуль, m.group(2), m.group(3).strip(), bool(m.group(4)),
             текст.count("\n", 0, m.start()) + 1)
        )
    return len(индекс) - было


for файл in sorted(КОРЕНЬ.glob("*.txt")):
    # `.Template.txt` — макет, а не модуль.
    if файл.name.endswith(".Template.txt"):
        continue
    вид = файл.name.rsplit(".", 2)[-2] if файл.name.count(".") >= 2 else "?"
    текст = файл.read_text(encoding="utf-8-sig", errors="replace")
    строк += текст.count("\n") + 1
    байт += len(текст.encode("utf-8"))
    виды[вид] += 1
    собрать(файл.name, текст)

# Общие модули, поставленные скомпилированными: контейнер с записью `image`,
# внутри — образ, а не текст. Молчать о них нельзя: агент решит, что процедуры
# просто не нашлись.
for файл in sorted(КОРЕНЬ.glob("*.Module")):
    with V8Container(файл.read_bytes()) as контейнер:
        образ = контейнер.read("image").decode("utf-8-sig", errors="replace")
    if not ОБЪЯВЛЕНИЕ.search(образ):
        защищённых.append((файл.name, len(образ)))

for файл in sorted(КОРЕНЬ.glob("*.Form")):
    форм_всего += 1
    with V8Container(файл.read_bytes()) as контейнер:
        if "module" not in контейнер.namelist():
            continue
        сырое = контейнер.read("module")
    if len(сырое) < 10:
        форм_пустых += 1
        continue
    текст = сырое.decode("utf-8-sig", errors="replace")
    строк += текст.count("\n") + 1
    байт += len(текст.encode("utf-8"))
    виды["ФормаМодуль"] += 1
    форм_с_кодом += 1
    собрать(файл.name, текст)

разбор = time.monotonic() - начало
блоб = marshal.dumps(индекс)

print(f"модулей            : {sum(виды.values()):,}".replace(",", " "))
print(f"строк              : {строк:,}".replace(",", " "))
print(f"объём кода         : {байт / 1024 / 1024:.1f} МБ")
print(f"процедур и функций : {len(индекс):,}".replace(",", " "))
print(f"из них экспортных  : {sum(1 for r in индекс if r[3]):,}".replace(",", " "))
print(f"время разбора      : {разбор:.1f} с")
print(f"индекс в marshal   : {len(блоб) / 1024 / 1024:.1f} МБ")
print(f"форм               : {форм_всего}, с кодом {форм_с_кодом}, пустых {форм_пустых}")
print(f"по видам           : {dict(виды.most_common())}")
print(f"без исходников     : {len(защищённых)}")
for имя, размер in защищённых:
    print(f"    {имя[:60]:60} {размер:>8} символов образа")

if ВЫХОД:
    ВЫХОД.write_bytes(блоб)
    print(f"индекс записан     : {ВЫХОД}")
