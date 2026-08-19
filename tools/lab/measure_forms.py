"""Цена разбора форм: 3 194 файла Form.xml, 91 МБ.

Форма нужна агенту тремя вещами: реквизиты формы, элементы, команды.
Меряем разбор и то, во что обходится держать это в памяти.
"""
import gc
import marshal
import os
import subprocess
import sys
import time
import xml.etree.ElementTree as ET
from collections import Counter
from pathlib import Path

КОРЕНЬ = Path(sys.argv[1])


def rss_мб() -> float:
    вывод = subprocess.run(["ps", "-o", "rss=", "-p", str(os.getpid())],
                           capture_output=True, text=True).stdout.strip()
    return int(вывод) / 1024


# В выгрузке формы теги идут с пространствами имён; берём локальное имя.
def локальное(тег: str) -> str:
    return тег.rsplit("}", 1)[-1]


индекс = []
счёт = Counter()
сломанных = 0
начало = time.monotonic()

for путь in sorted(КОРЕНЬ.rglob("Ext/Form.xml")):
    try:
        корень = ET.parse(путь).getroot()
    except ET.ParseError:
        сломанных += 1
        continue
    адрес = str(путь.relative_to(КОРЕНЬ))
    реквизиты, элементы, команды = [], [], []
    for узел in корень.iter():
        имя_тега = локальное(узел.tag)
        if имя_тега == "Attributes":
            for a in узел:
                имя = a.get("name") or ""
                if имя:
                    реквизиты.append(имя)
        elif имя_тега in ("InputField", "LabelField", "CheckBoxField", "Table",
                          "UsualGroup", "Button", "CommandBarHolder", "Page",
                          "Pages", "PictureField", "RadioButtonField"):
            имя = узел.get("name") or ""
            if имя:
                элементы.append((имя, имя_тега))
        elif имя_тега == "FormCommands":
            for c in узел:
                имя = c.get("name") or ""
                if имя:
                    команды.append(имя)
    счёт["реквизитов"] += len(реквизиты)
    счёт["элементов"] += len(элементы)
    счёт["команд"] += len(команды)
    индекс.append((адрес, реквизиты, элементы, команды))

разбор = time.monotonic() - начало
gc.collect()
резидент = rss_мб()
блоб = marshal.dumps(индекс)

print(f"форм разобрано  : {len(индекс):,}".replace(",", " "))
print(f"не разобралось  : {сломанных}")
for к, v in счёт.most_common():
    print(f"{к:16}: {v:,}".replace(",", " "))
print(f"время разбора   : {разбор:.1f} с")
print(f"RSS процесса    : {резидент:.0f} МБ")
print(f"в marshal       : {len(блоб)/1024/1024:.1f} МБ")
