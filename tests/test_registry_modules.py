"""Строки кода прежней версии рядом с метаданными той же конфигурации.

Приём кода отдельно от структуры удалён: код приходит только native-
поколением. В data/ прежней версии остаются строка `KIND_MODULES` и каталог
`data/modules/<Имя>/`; startup обязан их поднять, а `remove` — снять, не
задев метаданные и соседние каталоги.
"""
from pathlib import Path

import pytest

from conftest import build_configuration, write_export
from mcp1c.registry import KIND_CONFIGURATION, KIND_MODULES, Registry, RegistryError
from native_export import записать_старый_код


def _старый_код(tmp_path: Path) -> Path:
    корень = tmp_path / "код"
    модуль = корень / "Catalogs" / "Т" / "Ext" / "ObjectModule.bsl"
    модуль.parent.mkdir(parents=True)
    модуль.write_text("Процедура А() КонецПроцедуры", encoding="utf-8")
    return корень


def _реестр_прежней_версии(tmp_path: Path) -> Registry:
    """schema v1 «Розница» и её код отдельной строкой, поднятые startup."""
    входящее = tmp_path / "in"
    входящее.mkdir()
    registry = Registry(tmp_path / "data")
    registry.add_configuration(
        write_export(входящее, build_configuration(name="Розница"))
    )
    registry.save()
    записать_старый_код(registry, _старый_код(tmp_path), configuration="Розница")
    заново = Registry(registry.data_dir)
    assert заново.startup() == []
    assert заново.wait_for_module_builds(timeout=10)
    return заново


def test_источник_модулей_переживает_перезапуск(tmp_path):
    заново = _реестр_прежней_версии(tmp_path)

    assert "Розница:modules" in заново.sources
    assert заново.sources["Розница:modules"].kind == KIND_MODULES
    # Метаданные конфигурации тоже восстановлены — не только модули.
    assert заново.sources["Розница"].kind == KIND_CONFIGURATION


def test_имя_конфигурации_не_уводит_за_modules_dir(tmp_path):
    """Имя приходит из журнала рокировки и registry.json: там бывает `..`."""
    registry = Registry(tmp_path / "data")

    with pytest.raises(RegistryError):
        registry._modules_root("..")

    # Косая черта не создаёт вложенности: имя чистится до одного сегмента.
    корень = registry._modules_root("../incoming")
    assert корень.parent == registry.modules_dir.resolve()
    assert корень.name == ".._incoming"


def test_снятие_источника_удаляет_каталог_с_кодом(tmp_path):
    """351 МБ на живой конфигурации: `orphan_sources` их не покажет."""
    registry = _реестр_прежней_версии(tmp_path)
    корень = registry.modules_dir / "Розница"
    assert корень.is_dir()

    registry.remove("Розница:modules")

    assert not корень.exists()
    assert "Розница:modules" not in registry.sources
    # Каталог приёма рядом — его снятие источника не касается.
    assert not registry.incoming_dir.exists() or registry.incoming_dir.is_dir()


def test_снятие_источника_не_держит_замок_на_время_удаления(tmp_path, monkeypatch):
    """Тот же замок берут `resolve()` и инструменты MCP: 11 072 файла — не миг."""
    import shutil
    import threading

    registry = _реестр_прежней_версии(tmp_path)
    свободен = []
    настоящий_rmtree = shutil.rmtree

    def под_наблюдением(путь, *args, **kwargs):
        # Проверяем из ЧУЖОГО потока: `RLock` повторно входим для своего.
        def попробовать():
            взят = registry._lock.acquire(timeout=2)
            свободен.append(взят)
            if взят:
                registry._lock.release()

        поток = threading.Thread(target=попробовать)
        поток.start()
        поток.join()
        return настоящий_rmtree(путь, *args, **kwargs)

    monkeypatch.setattr("mcp1c.registry.shutil.rmtree", под_наблюдением)

    registry.remove("Розница:modules")

    assert свободен == [True]
    assert not (registry.modules_dir / "Розница").exists()
