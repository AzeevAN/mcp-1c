"""Полная загрузка schema v1 не меняет редакцию уже загруженной конфигурации."""

from __future__ import annotations

import pytest

from conftest import build_configuration, write_export
from mcp1c.cli import main
from mcp1c.registry import Registry, RegistryError

ИМЯ = "РозницаТест"


def _выгрузка(tmp_path, version: str):
    папка = tmp_path / f"export-{version or 'пусто'}"
    папка.mkdir()
    return write_export(папка, build_configuration(name=ИМЯ, version=version))


def _версия(registry: Registry) -> str:
    return registry.snapshot().configurations[ИМЯ].config.version


def test_другая_редакция_отклоняется_и_реестр_остаётся_прежним(tmp_path):
    registry = Registry(tmp_path / "data")
    registry.add_configuration(_выгрузка(tmp_path, "2.3.26.24"), keep_source=False)

    with pytest.raises(RegistryError, match="редакции 2.3.*редакции 3.0"):
        registry.add_configuration(
            _выгрузка(tmp_path, "3.0.14.178"),
            keep_source=False,
            same_edition_only=True,
        )

    assert _версия(registry) == "2.3.26.24"
    assert list(registry.snapshot().configurations) == [ИМЯ]


def test_смена_редакции_на_пустую_версию_отклоняется(tmp_path):
    registry = Registry(tmp_path / "data")
    registry.add_configuration(_выгрузка(tmp_path, "2.3.26.24"), keep_source=False)

    with pytest.raises(RegistryError, match="без версии"):
        registry.add_configuration(
            _выгрузка(tmp_path, ""), keep_source=False, same_edition_only=True
        )

    assert _версия(registry) == "2.3.26.24"


def test_та_же_редакция_проходит_и_заменяет_версию(tmp_path):
    registry = Registry(tmp_path / "data")
    registry.add_configuration(_выгрузка(tmp_path, "2.3.26.24"), keep_source=False)

    registry.add_configuration(
        _выгрузка(tmp_path, "2.3.27.1"), keep_source=False, same_edition_only=True
    )

    assert _версия(registry) == "2.3.27.1"


def test_первая_загрузка_с_флагом_проходит_любой_версией(tmp_path):
    registry = Registry(tmp_path / "data")

    registry.add_configuration(
        _выгрузка(tmp_path, "3.0.14.178"), keep_source=False, same_edition_only=True
    )

    assert _версия(registry) == "3.0.14.178"


def test_без_флага_смена_редакции_проходит_как_при_восстановлении(tmp_path):
    registry = Registry(tmp_path / "data")
    registry.add_configuration(_выгрузка(tmp_path, "2.3.26.24"), keep_source=False)

    registry.add_configuration(_выгрузка(tmp_path, "3.0.14.178"), keep_source=False)

    assert _версия(registry) == "3.0.14.178"


def test_cli_reg_add_отказывает_при_смене_редакции(tmp_path, capsys):
    data = tmp_path / "data"
    assert main(
        ["reg-add", str(_выгрузка(tmp_path, "2.3.26.24")), "--data", str(data)]
    ) == 0
    capsys.readouterr()

    code = main(
        ["reg-add", str(_выгрузка(tmp_path, "3.0.14.178")), "--data", str(data)]
    )

    assert code == 2
    assert "редакции 3.0" in capsys.readouterr().err
    восстановленный = Registry(data)
    восстановленный.restore()
    assert _версия(восстановленный) == "2.3.26.24"


@pytest.mark.parametrize(
    ('current', 'incoming', 'allowed'),
    [
        ('', '1.0.0.1', True),
        ('2.3.26.24', '3.0.14.178', False),
        ('20080620(0935)', '1.0.0.1', False),
    ],
    ids=['empty-to-numbered', 'different-editions', 'calendar-to-numbered'],
)
def test_registry_обновление_пустой_версии_публикует_новую_структуру_без_смены_других_редакций(
    tmp_path, current, incoming, allowed,
):
    from mcp1c.model import MetadataObject

    registry = Registry(tmp_path / 'data')
    original = registry.add_configuration(_выгрузка(tmp_path, current), keep_source=False)
    before_objects = set(registry.snapshot().configurations[ИМЯ].config.objects)
    removed_object = 'Документ.РеализацияТоваровУслуг'
    added_object = 'Справочник.НовыйОбъект'
    assert removed_object in before_objects
    assert added_object not in before_objects

    incoming_config = build_configuration(name=ИМЯ, version=incoming)
    incoming_config.objects.pop(removed_object)
    incoming_config.objects[added_object] = MetadataObject(
        full_name=added_object, kind='Справочник', name='НовыйОбъект',
        synonym='Новый синтетический объект',
    )
    directory = tmp_path / 'incoming-structural-update'
    directory.mkdir()
    archive = write_export(directory, incoming_config)

    if allowed:
        replacement = registry.add_configuration(
            archive, keep_source=False, same_edition_only=True,
        )
        assert replacement.sha256 != original.sha256
        snapshot = registry.snapshot()
        assert list(snapshot.configurations) == [ИМЯ]
        active = snapshot.configurations[ИМЯ].config
        assert active.version == incoming
        assert added_object in active.objects
        assert active.objects[added_object].synonym == 'Новый синтетический объект'
        assert removed_object not in active.objects
    else:
        with pytest.raises(RegistryError, match='другое поколение'):
            registry.add_configuration(
                archive, keep_source=False, same_edition_only=True,
            )
        active = registry.snapshot().configurations[ИМЯ].config
        assert active.version == current
        assert set(active.objects) == before_objects
        assert added_object not in active.objects
