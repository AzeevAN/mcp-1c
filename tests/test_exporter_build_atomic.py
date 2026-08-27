"""Транзакционная публикация четырёх собранных BSL-вариантов."""

from __future__ import annotations

import importlib.util
import os
from pathlib import Path
from types import ModuleType

import pytest


ROOT = Path(__file__).resolve().parents[1]
BUILD_MODULES = ROOT / "exporter-1c" / "build_modules.py"


@pytest.fixture
def сборщик() -> ModuleType:
    spec = importlib.util.spec_from_file_location("build_modules_atomic_test", BUILD_MODULES)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _подготовить_исходники(
    сборщик: ModuleType,
    корень: Path,
    *,
    ядро: str = "Процедура Проверка()\nКонецПроцедуры\n",
) -> None:
    src = корень / "src"
    src.mkdir(parents=True)
    (src / "core.bsl").write_text(ядро, encoding="utf-8")
    for variant_name in сборщик.VARIANTS:
        (src / variant_name).write_text(
            "// Синтетический вариант\n// {{CORE}}\n",
            encoding="utf-8",
        )
    сборщик.SRC = src
    сборщик.DIST = корень / "dist"


def _старый_dist(сборщик: ModuleType) -> dict[str, bytes]:
    сборщик.DIST.mkdir(parents=True)
    старые: dict[str, bytes] = {}
    for out_name, _, _ in сборщик.VARIANTS.values():
        содержимое = f"старый результат: {out_name}\n".encode()
        (сборщик.DIST / out_name).write_bytes(содержимое)
        старые[out_name] = содержимое
    return старые


def _проверить_старый_dist(сборщик: ModuleType, старые: dict[str, bytes]) -> None:
    assert {
        path.name: path.read_bytes()
        for path in сборщик.DIST.iterdir()
        if path.suffix == ".bsl"
    } == старые


def _проверить_временные_каталоги_убраны(сборщик: ModuleType) -> None:
    assert not list(сборщик.DIST.parent.glob(".dist-build-*"))


def test_ошибка_проверки_835_не_меняет_ни_один_старый_результат(
    сборщик: ModuleType,
    tmp_path: Path,
) -> None:
    _подготовить_исходники(
        сборщик,
        tmp_path / "exporter",
        ядро='Процедура Проверка()\nСтрРазделить("а", ",");\nКонецПроцедуры\n',
    )
    старые = _старый_dist(сборщик)

    assert сборщик.build() == 1

    _проверить_старый_dist(сборщик, старые)
    _проверить_временные_каталоги_убраны(сборщик)


def test_отсутствующий_поздний_вариант_не_оставляет_частичную_сборку(
    сборщик: ModuleType,
    tmp_path: Path,
) -> None:
    _подготовить_исходники(сборщик, tmp_path / "exporter")
    поздний = next(reversed(сборщик.VARIANTS))
    (сборщик.SRC / поздний).unlink()
    старые = _старый_dist(сборщик)

    assert сборщик.build() == 1

    _проверить_старый_dist(сборщик, старые)
    _проверить_временные_каталоги_убраны(сборщик)


def test_ошибка_записи_временного_набора_не_меняет_dist(
    сборщик: ModuleType,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _подготовить_исходники(сборщик, tmp_path / "exporter")
    старые = _старый_dist(сборщик)
    выходы = set(старые)
    исходная_запись = Path.write_text
    записано = 0

    def отказ_на_третьем(self: Path, *args: object, **kwargs: object) -> int:
        nonlocal записано
        if self.name in выходы:
            записано += 1
            if записано == 3:
                raise OSError("синтетический отказ записи")
        return исходная_запись(self, *args, **kwargs)

    monkeypatch.setattr(Path, "write_text", отказ_на_третьем)

    assert сборщик.build() == 1

    _проверить_старый_dist(сборщик, старые)
    _проверить_временные_каталоги_убраны(сборщик)


def test_ошибка_публикации_откатывает_уже_заменённые_файлы(
    сборщик: ModuleType,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _подготовить_исходники(сборщик, tmp_path / "exporter")
    старые = _старый_dist(сборщик)
    исходная_замена = os.replace
    заменено = 0

    def отказ_на_третьем(src: str | bytes | Path, dst: str | bytes | Path) -> None:
        nonlocal заменено
        путь = Path(dst)
        if путь.parent == сборщик.DIST and путь.name in старые:
            заменено += 1
            if заменено == 3:
                raise OSError("синтетический отказ публикации")
        исходная_замена(src, dst)

    monkeypatch.setattr(os, "replace", отказ_на_третьем)

    assert сборщик.build() == 1

    _проверить_старый_dist(сборщик, старые)
    _проверить_временные_каталоги_убраны(сборщик)


def test_успех_заменяет_четыре_bsl_и_не_трогает_ручные_epf(
    сборщик: ModuleType,
    tmp_path: Path,
) -> None:
    _подготовить_исходники(сборщик, tmp_path / "exporter")
    старые = _старый_dist(сборщик)
    epf = сборщик.DIST / "РучнаяОбработка.epf"
    epf.write_bytes(b"synthetic-epf")

    assert сборщик.build() == 0

    assert all((сборщик.DIST / name).read_bytes() != old for name, old in старые.items())
    assert epf.read_bytes() == b"synthetic-epf"
    _проверить_временные_каталоги_убраны(сборщик)
