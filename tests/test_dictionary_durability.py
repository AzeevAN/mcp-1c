"""Надёжность локального словаря и registry.json на диске.

Сбой питания или ручная правка не должны оставлять сервер без старта, а
неудачная запись — неподтверждённую правку в памяти.
"""

from __future__ import annotations

import json
import os
import stat

import pytest

from mcp1c import dictionary as dictionary_module
from mcp1c.dictionary import Dictionary, DictionaryError
from mcp1c.registry import Registry, RegistryRecoveryBlocked


def _mode(path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


def test_save_делает_fsync_файла_и_каталога(tmp_path, monkeypatch):
    target = tmp_path / "dictionary.json"
    synced: list[str] = []
    original = os.fsync

    def recording(fd):
        synced.append("dir" if stat.S_ISDIR(os.fstat(fd).st_mode) else "file")
        return original(fd)

    monkeypatch.setattr(dictionary_module.os, "fsync", recording)
    Dictionary(synonym_groups=[["а", "б"]]).save(target)

    assert "file" in synced
    assert "dir" in synced
    assert synced.index("file") < synced.index("dir")


def test_save_сохраняет_прежние_права(tmp_path):
    target = tmp_path / "dictionary.json"
    target.write_text("{}", encoding="utf-8")
    target.chmod(0o640)

    Dictionary(synonym_groups=[["а", "б"]]).save(target)

    assert _mode(target) == 0o640


def test_новый_словарь_получает_права_по_umask(tmp_path):
    target = tmp_path / "dictionary.json"
    previous = os.umask(0o022)
    try:
        Dictionary().save(target)
    finally:
        os.umask(previous)

    # NamedTemporaryFile давал 0600: хост-пользователь терял право чтения.
    assert _mode(target) == 0o644


@pytest.mark.parametrize(
    "content",
    ["", "{", "[1, 2]", '{"aliases": [1]}', '{"synonym_groups": 5}'],
)
def test_битый_словарь_даёт_понятную_ошибку(tmp_path, content):
    target = tmp_path / "dictionary.json"
    target.write_text(content, encoding="utf-8")

    with pytest.raises(DictionaryError, match="dictionary.json"):
        Dictionary.load(target)


def test_реестр_стартует_с_битым_словарём_и_не_затирает_его(tmp_path):
    data = tmp_path / "data"
    data.mkdir()
    target = data / "dictionary.json"
    target.write_text('{"synonym_groups": [["возчик", "перев', encoding="utf-8")
    before = target.read_bytes()

    registry = Registry(data)
    messages = registry.startup()

    assert any("dictionary.json" in message for message in messages), messages
    with pytest.raises(DictionaryError, match="не записана"):
        registry.mutate_dictionary(lambda d: d.add_synonyms(["а", "б"]))
    assert target.read_bytes() == before

    # Исправленный файл подхватывается перезагрузкой, запись снова открыта.
    target.write_text(json.dumps({"synonym_groups": [["а", "б"]]}), encoding="utf-8")
    assert not any("dictionary.json" in m for m in registry.startup())
    registry.mutate_dictionary(lambda d: d.add_synonyms(["в", "г"]))
    assert Dictionary.load(target).synonym_groups == [["а", "б"], ["в", "г"]]


def test_reload_с_битым_словарём_оставляет_прежний(tmp_path):
    data = tmp_path / "data"
    registry = Registry(data)
    registry.mutate_dictionary(lambda d: d.add_synonyms(["возчик", "перевозчик"]))
    registry.dictionary_path.write_text("{", encoding="utf-8")

    messages = registry.startup()

    assert any("dictionary.json" in message for message in messages)
    assert registry.dictionary.synonym_groups == [["возчик", "перевозчик"]]


def test_сбой_записи_не_оставляет_правку_в_памяти(tmp_path, monkeypatch):
    registry = Registry(tmp_path / "data")
    registry.mutate_dictionary(lambda d: d.add_synonyms(["возчик", "перевозчик"]))
    live = registry.dictionary

    def failing(self, path=None):
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(Dictionary, "save", failing)
    with pytest.raises(OSError):
        registry.mutate_dictionary(lambda d: d.add_synonyms(["лишнее", "слово"]))
    monkeypatch.undo()

    assert registry.dictionary is live
    assert registry.dictionary.synonym_groups == [["возчик", "перевозчик"]]

    # Следующая удачная правка не уносит на диск отвергнутую.
    registry.mutate_dictionary(lambda d: d.add_synonyms(["склад", "хранилище"]))
    assert Dictionary.load(registry.dictionary_path).synonym_groups == [
        ["возчик", "перевозчик"],
        ["склад", "хранилище"],
    ]


def test_битая_мутация_не_оставляет_правку_в_памяти(tmp_path):
    registry = Registry(tmp_path / "data")

    def half_done(dictionary):
        dictionary.add_synonyms(["возчик", "перевозчик"])
        raise ValueError("отказ посреди правки")

    with pytest.raises(ValueError):
        registry.mutate_dictionary(half_done)

    assert registry.dictionary.synonym_groups == []


@pytest.mark.parametrize(
    "content", [b"", b"{", b"[]", '"строка"'.encode(), b'{"a": "\xff"}']
)
def test_битый_registry_json_закрывает_запись_и_не_затирается(tmp_path, content):
    data = tmp_path / "data"
    data.mkdir()
    registry_path = data / "registry.json"
    registry_path.write_bytes(content)
    before = registry_path.read_bytes()

    registry = Registry(data)
    messages = registry.startup()

    assert any("registry.json" in m and "только для чтения" in m for m in messages)
    with pytest.raises(RegistryRecoveryBlocked, match="registry.json"):
        registry.save()
    assert registry_path.read_bytes() == before


def test_cli_не_затирает_нечитаемый_словарь(tmp_path, capsys):
    from mcp1c import cli

    data = tmp_path / "data"
    data.mkdir()
    target = data / "dictionary.json"
    target.write_text("{", encoding="utf-8")
    before = target.read_bytes()

    code = cli.main(["dict-synonyms", "возчик", "перевозчик", "--data", str(data)])

    assert code == 2
    assert "dictionary.json" in capsys.readouterr().err
    assert target.read_bytes() == before

    assert cli.main(["dict-show", "--data", str(data)]) == 0
    assert "не читается" in capsys.readouterr().err


def test_правка_применяется_к_актуальному_файлу_а_не_к_памяти(tmp_path):
    registry = Registry(tmp_path / "data")
    registry.mutate_dictionary(lambda d: d.add_synonyms(["возчик", "перевозчик"]))
    # Оператор правит файл на томе (или `dict-*` CLI) без reload сервера.
    external = Dictionary.load(registry.dictionary_path)
    external.add_alias("склад", ["Справочник.Склады"])
    external.save(registry.dictionary_path)

    registry.mutate_dictionary(lambda d: d.add_synonyms(["склад", "хранилище"]))

    on_disk = Dictionary.load(registry.dictionary_path)
    assert on_disk.aliases == {"*": {"склад": ["Справочник.Склады"]}}
    assert on_disk.synonym_groups == [["возчик", "перевозчик"], ["склад", "хранилище"]]
    assert registry.dictionary.aliases == on_disk.aliases
    assert registry.dictionary.synonym_groups == on_disk.synonym_groups


def test_правка_не_затирает_файл_испорченный_после_старта(tmp_path):
    registry = Registry(tmp_path / "data")
    registry.mutate_dictionary(lambda d: d.add_synonyms(["возчик", "перевозчик"]))
    registry.dictionary_path.write_text("{", encoding="utf-8")

    with pytest.raises(DictionaryError, match="не записана"):
        registry.mutate_dictionary(lambda d: d.add_synonyms(["а", "б"]))

    assert registry.dictionary_path.read_text(encoding="utf-8") == "{"
    assert registry.dictionary_error
    assert registry.dictionary.synonym_groups == [["возчик", "перевозчик"]]


def test_правка_без_изменений_не_пишет_файл(tmp_path, monkeypatch):
    registry = Registry(tmp_path / "data")
    registry.mutate_dictionary(lambda d: d.add_synonyms(["возчик", "перевозчик"]))

    def forbidden(self, path=None):
        raise OSError(30, "Read-only file system")

    monkeypatch.setattr(Dictionary, "save", forbidden)
    assert registry.mutate_dictionary(lambda d: d.remove_synonyms(["нет", "такой"])) is False
    assert registry.mutate_dictionary(lambda d: d.remove_alias("нет такого")) is False


def test_cli_снятие_отсутствующей_группы_на_томе_только_для_чтения(tmp_path, capsys):
    from mcp1c import cli

    data = tmp_path / "data"
    data.mkdir()
    Dictionary(synonym_groups=[["возчик", "перевозчик"]]).save(data / "dictionary.json")
    before = (data / "dictionary.json").read_bytes()
    data.chmod(0o555)
    try:
        code = cli.main(["dict-synonyms", "нет", "такой", "--remove", "--data", str(data)])
    finally:
        data.chmod(0o755)

    assert code == 1
    assert "такой группы нет" in capsys.readouterr().out
    assert (data / "dictionary.json").read_bytes() == before


def test_отказ_fsync_каталога_после_replace_не_расходит_память_с_диском(
    tmp_path, monkeypatch
):
    registry = Registry(tmp_path / "data")
    registry.mutate_dictionary(lambda d: d.add_synonyms(["возчик", "перевозчик"]))
    original = os.fsync

    def failing_dir(fd):
        if stat.S_ISDIR(os.fstat(fd).st_mode):
            raise OSError(5, "Input/output error")
        return original(fd)

    monkeypatch.setattr(dictionary_module.os, "fsync", failing_dir)
    registry.mutate_dictionary(lambda d: d.add_synonyms(["склад", "хранилище"]))
    monkeypatch.undo()

    expected = [["возчик", "перевозчик"], ["склад", "хранилище"]]
    assert Dictionary.load(registry.dictionary_path).synonym_groups == expected
    assert registry.dictionary.synonym_groups == expected
