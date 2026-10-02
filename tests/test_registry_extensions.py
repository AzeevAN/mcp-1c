"""Расширение прежней версии — отдельная строка и свой каталог кода.

Приём кода отдельно от структуры удалён: расширение приходит только native-
поколением. В data/ прежней версии остаются строки
`<Конфигурация>:ext:<Имя>` и каталоги `data/extensions/<Конфигурация>/<Имя>/`
рядом с `data/modules/<Конфигурация>/`; startup обязан их поднять, `remove`
— снять только своё, а пути из registry.json и журнала рокировки — не увести
за пределы `extensions_dir`.
"""
from pathlib import Path

import pytest

from conftest import build_configuration, write_export
from mcp1c.registry import KIND_CONFIGURATION, KIND_EXTENSION, Registry, RegistryError
from native_export import записать_старый_код


def _код(tmp_path: Path, имя: str) -> Path:
    корень = tmp_path / f"код-{имя}"
    модуль = корень / "Catalogs" / "Р" / "Ext" / "ObjectModule.bsl"
    модуль.parent.mkdir(parents=True)
    модуль.write_text(f"Процедура {имя}() КонецПроцедуры", encoding="utf-8")
    return корень


def _реестр_прежней_версии(tmp_path: Path, *, base: bool, extensions: tuple[str, ...]):
    """schema v1 «Розница», её код и расширения строками прежней версии."""
    входящее = tmp_path / "in"
    входящее.mkdir()
    registry = Registry(tmp_path / "data")
    registry.add_configuration(
        write_export(входящее, build_configuration(name="Розница"))
    )
    registry.save()
    if base:
        записать_старый_код(registry, _код(tmp_path, "База"), configuration="Розница")
    for имя in extensions:
        записать_старый_код(
            registry, _код(tmp_path, имя), configuration="Розница", extension=имя
        )
    заново = Registry(registry.data_dir)
    assert заново.startup() == []
    assert заново.wait_for_module_builds(timeout=10)
    return заново


def test_снятие_расширения_сносит_только_свой_каталог(tmp_path):
    registry = _реестр_прежней_версии(
        tmp_path, base=True, extensions=("РасширениеА", "Мобильный")
    )

    registry.remove("Розница:ext:РасширениеА")

    assert not (registry.extensions_dir / "Розница" / "РасширениеА").exists()
    assert (registry.extensions_dir / "Розница" / "Мобильный").is_dir()
    assert (registry.modules_dir / "Розница").is_dir()
    assert "Розница:ext:РасширениеА" not in registry.sources
    assert "Розница:ext:Мобильный" in registry.sources
    assert "Розница:modules" in registry.sources


def test_источник_расширения_переживает_перезапуск(tmp_path):
    заново = _реестр_прежней_версии(
        tmp_path, base=False, extensions=("РасширениеА",)
    )

    assert "Розница:ext:РасширениеА" in заново.sources
    assert заново.sources["Розница:ext:РасширениеА"].kind == KIND_EXTENSION
    # Метаданные конфигурации тоже восстановлены — не только расширение.
    assert заново.sources["Розница"].kind == KIND_CONFIGURATION


def test_имя_расширения_с_необычными_символами_не_уводит_за_extensions_dir(tmp_path):
    """Имя приходит из registry.json и журнала рокировки — доверять ему нельзя."""
    registry = Registry(tmp_path / "data")

    with pytest.raises(RegistryError):
        registry._extension_root("Розница", "..")

    # Косая черта не создаёт вложенности: имя чистится до одного сегмента,
    # и результат остаётся внутри extensions_dir.
    корень = registry._extension_root("Розница", "../incoming")
    assert корень.parent == (registry.extensions_dir / "Розница").resolve()
    assert корень.name == ".._incoming"


def test_имя_расширения_точка_не_уводит_в_каталог_конфигурации(tmp_path):
    """`safe_name(".") == "."`, и `pathlib` схлопывает его при `resolve()`.

    Без явного отказа второй уровень пути `extensions_dir/<Конфигурация>/.`
    резолвится в САМ каталог конфигурации (существующий, чужой), а не в
    `extensions_dir` — проверка на принадлежность корню это пропускает, и
    снос такого «расширения» унёс бы все соседние.
    """
    registry = Registry(tmp_path / "data")

    for имя in (".", ""):
        with pytest.raises(RegistryError):
            registry._extension_root("Розница", имя)


def test_sweep_не_сносит_кэш_нового_вида(tmp_path):
    """`KIND_EXTENSION` обязан быть в `CACHE_KINDS` — иначе кэш сочли бы ничьим.

    Один вид из четырёх (`modules-toc`) — не полная проверка: она ловит
    отсутствие записи для `KIND_EXTENSION` вообще, а не снос трёх видов из
    четырёх при одном общем имени. Полную проверку даёт
    `tests/test_index_cache_modules.py::test_sweep_не_сносит_ни_один_
    индекс_модулей`.
    """
    from mcp1c import index_cache

    registry = _реестр_прежней_версии(
        tmp_path, base=False, extensions=("РасширениеА",)
    )
    registry.cache_dir.mkdir(parents=True, exist_ok=True)
    кэш = registry._cache_path("Розница:ext:РасширениеА", "modules-toc")
    кэш.write_bytes(b"stop")

    убрано = index_cache.sweep(registry.cache_dir, registry._cached_names())

    assert кэш.name not in убрано
    assert кэш.is_file()
