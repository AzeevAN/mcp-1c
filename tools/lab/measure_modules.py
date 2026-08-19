"""Разведочный замер: во что обойдётся индекс сигнатур модулей.

Индексируем сигнатуры и адреса, тела не держим — проектное решение из
«Направлений». Меряем: время разбора, число процедур, объём индекса в памяти
и на диске (marshal, как у index_cache.py).
"""
import marshal
import re
import resource
import sys
import time
from collections import Counter
from pathlib import Path

КОРЕНЬ = Path(sys.argv[1])

# Объявление процедуры/функции: имя, параметры, экспорт.
ОБЪЯВЛЕНИЕ = re.compile(
    r"^\s*(Процедура|Функция|Procedure|Function)\s+([А-Яа-яЁёA-Za-z_][\w]*)\s*\(([^)]*)\)\s*(Экспорт|Export)?",
    re.IGNORECASE | re.MULTILINE,
)
АННОТАЦИЯ = re.compile(
    r"&(Перед|После|Вместо|ИзменениеИКонтроль)\s*\(\s*\"([^\"]+)\"\s*\)", re.IGNORECASE
)
ДИРЕКТИВА = re.compile(r"^\s*#(Вставка|Удаление|КонецВставки|КонецУдаления)", re.IGNORECASE | re.MULTILINE)
ОБЛАСТЬ = re.compile(r"^\s*#Область\s+([^\s]+)", re.IGNORECASE | re.MULTILINE)


def вид_модуля(путь: Path) -> str:
    s = str(путь)
    if "/Forms/" in s:
        return "форма"
    if s.endswith("ObjectModule.bsl"):
        return "объект"
    if s.endswith("ManagerModule.bsl"):
        return "менеджер"
    if s.endswith("Module.bsl"):
        return "общий"
    if s.endswith("CommandModule.bsl"):
        return "команда"
    return "прочее"


def квалифицированное_имя(путь: Path) -> str:
    """Адрес модуля без корня: Documents/ЧекККМ/Ext/ObjectModule.bsl."""
    return str(путь.relative_to(КОРЕНЬ))


начало = time.monotonic()
rss0 = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss

индекс = []          # то, что реально держим
виды = Counter()
аннотаций = Counter()
директив = 0
областей = 0
файлов = 0
байт = 0
строк = 0

for путь in sorted(КОРЕНЬ.rglob("*.bsl")):
    текст = путь.read_text(encoding="utf-8-sig", errors="replace")
    файлов += 1
    байт += len(текст.encode("utf-8"))
    строк += текст.count("\n") + 1
    модуль = квалифицированное_имя(путь)
    вид = вид_модуля(путь)
    виды[вид] += 1
    директив += len(ДИРЕКТИВА.findall(текст))
    областей += len(ОБЛАСТЬ.findall(текст))

    # позиции аннотаций, чтобы связать с ближайшим следующим объявлением
    метки = [(m.start(), m.group(1), m.group(2)) for m in АННОТАЦИЯ.finditer(текст)]
    занятые = set()   # одна аннотация — одна процедура
    for m in ОБЪЯВЛЕНИЕ.finditer(текст):
        имя = m.group(2)
        параметры = m.group(3).strip()
        экспорт = bool(m.group(4))
        строка = текст.count("\n", 0, m.start()) + 1
        # Ближайшая аннотация ПЕРЕД объявлением и только она: одна аннотация
        # относится к одной процедуре, а не к каждой, что попала в окно.
        перекрытие = None
        ближайшая = [
            (поз, в, ц) for поз, в, ц in метки
            if 0 <= m.start() - поз < 400 and поз not in занятые
        ]
        if ближайшая:
            поз, вид_аннотации, цель = ближайшая[-1]
            занятые.add(поз)
            перекрытие = (вид_аннотации, цель)
            аннотаций[вид_аннотации] += 1
        индекс.append((модуль, имя, параметры, экспорт, строка, вид, перекрытие))

разбор = time.monotonic() - начало
rss1 = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss

сериализовано = marshal.dumps(индекс)

делитель = 1 if sys.platform == "darwin" else 1024  # macOS отдаёт байты, Linux — КБ
print(f"файлов .bsl        : {файлов:,}".replace(",", " "))
print(f"строк              : {строк:,}".replace(",", " "))
print(f"объём текста       : {байт / 1024 / 1024:,.1f} МБ".replace(",", " "))
print(f"процедур и функций : {len(индекс):,}".replace(",", " "))
print(f"из них экспортных  : {sum(1 for r in индекс if r[3]):,}".replace(",", " "))
print(f"перекрытий         : {sum(аннотаций.values()):,}  {dict(аннотаций)}".replace(",", " "))
print(f"директив правки    : {директив:,}".replace(",", " "))
print(f"областей #Область  : {областей:,}".replace(",", " "))
print(f"модулей по видам   : {dict(виды)}")
print(f"время разбора      : {разбор:.1f} с")
print(f"индекс в marshal   : {len(сериализовано) / 1024 / 1024:.1f} МБ")
print(f"пик RSS процесса   : {rss1 / делитель / 1024 / 1024:.0f} МБ (было {rss0 / делитель / 1024 / 1024:.0f})")
