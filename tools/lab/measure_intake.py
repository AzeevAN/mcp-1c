"""Сколько весит то, что мы оставим от выгрузки конфигурации в файлы.

Запуск: python3 tools/lab/measure_intake.py <путь к выгрузке .zip> [ещё .zip]

Ничего не распаковывает: размеры берутся из центрального каталога архива.
Разведочный скрипт, живёт до появления провайдера `modules`.
"""
import sys
import zipfile
from collections import Counter
from pathlib import Path

TREE_SUFFIXES = {".bsl"}
TREE_NAMES = {"Form.xml"}
FLAT_SUFFIXES = {".txt", ".Form"}


def формат(имена) -> str:
    return "плоская" if any(и.endswith(".Form") for и in имена) else "иерархическая"


def нужен(имя: str, ф: str) -> bool:
    p = Path(imя := имя)
    if ф == "плоская":
        return p.suffix in FLAT_SUFFIXES
    return p.suffix in TREE_SUFFIXES or p.name in TREE_NAMES


def разбор(архив: Path) -> None:
    with zipfile.ZipFile(архив) as zf:
        записи = [i for i in zf.infolist() if not i.is_dir()]
        имена = [i.filename for i in записи]
        ф = формат(имена)
        по_типам: Counter[str] = Counter()
        файлов: Counter[str] = Counter()
        for i in записи:
            с = Path(i.filename).suffix.lower() or "(без)"
            по_типам[с] += i.file_size
            файлов[с] += 1
        всего = sum(i.file_size for i in записи)
        отобрано = [i for i in записи if нужен(i.filename, ф)]
        нужно = sum(i.file_size for i in отобрано)

    мб = lambda b: b / 2**20
    print(f"\n=== {архив.name} ===")
    print(f"  архив на диске : {мб(архив.stat().st_size):>8.0f} МБ")
    print(f"  развёрнутый    : {мб(всего):>8.0f} МБ, файлов {len(записи)}")
    print(f"  формат         : {ф}")
    print(f"  ОТБИРАЕМ       : {мб(нужно):>8.1f} МБ, файлов {len(отобрано)}"
          f"  ({нужно / всего * 100:.1f}% развёрнутого)")
    print("  по типам (топ-8):")
    for с, размер in по_типам.most_common(8):
        print(f"    {с:<10} {мб(размер):>8.1f} МБ   файлов {файлов[с]:>6}")
    # Признаки расширения — по разведке, раздел 5.
    with zipfile.ZipFile(архив) as zf:
        манифест = next((и for и in имена if и.endswith("Configuration.xml")), None)
        if манифест:
            голова = zf.read(манифест)[:4000].decode("utf-8", "replace")
            for тег in ("ObjectBelonging", "ConfigurationExtensionPurpose",
                        "NamePrefix", "CompatibilityMode"):
                print(f"    тег {тег:<34} {'есть' if тег in голова else 'нет'}")


def main(argv: list[str]) -> int:
    if len(argv) < 2:
        print(__doc__)
        return 2
    for путь in argv[1:]:
        разбор(Path(путь))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
