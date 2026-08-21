"""Снятие группы синонимов.

Группу можно было завести и нельзя было снять — ни из кода, ни из CLI, ни из
браузера. Единственный способ отменить ошибку состоял в правке
`dictionary.json` руками, а неудачная группа тихо портит выдачу всем: слово
начинает подставляться там, где его не ждут.

Группа опознаётся по составу, а не по номеру: номер сдвинется от любой правки
соседей, а состав — то, что человек видит на экране.
"""

from __future__ import annotations

from mcp1c.dictionary import Dictionary


def test_группа_снимается_по_составу(tmp_path):
    словарь = Dictionary(path=tmp_path / "dictionary.json")
    словарь.add_synonyms(["возчик", "перевозчик", "экспедитор"])
    словарь.add_synonyms(["склад", "хранилище"])

    убрано = словарь.remove_synonyms(["возчик", "перевозчик", "экспедитор"])

    assert убрано is True
    assert словарь.synonym_groups == [["склад", "хранилище"]]


def test_порядок_и_регистр_не_мешают(tmp_path):
    """Человек вводит слова как помнит, а не как они записаны."""
    словарь = Dictionary(path=tmp_path / "dictionary.json")
    словарь.add_synonyms(["возчик", "перевозчик"])

    assert словарь.remove_synonyms(["Перевозчик", "ВОЗЧИК"]) is True
    assert словарь.synonym_groups == []


def test_снятие_несуществующей_группы_не_ошибка(tmp_path):
    словарь = Dictionary(path=tmp_path / "dictionary.json")
    словарь.add_synonyms(["возчик", "перевозчик"])

    assert словарь.remove_synonyms(["склад", "хранилище"]) is False
    assert len(словарь.synonym_groups) == 1


def test_частичное_совпадение_не_снимает(tmp_path):
    """«возчик» входит в группу, но группа — это весь состав целиком."""
    словарь = Dictionary(path=tmp_path / "dictionary.json")
    словарь.add_synonyms(["возчик", "перевозчик", "экспедитор"])

    assert словарь.remove_synonyms(["возчик", "перевозчик"]) is False
    assert len(словарь.synonym_groups) == 1


def test_снятая_группа_перестаёт_действовать_на_поиск(tmp_path):
    """Проверяется наблюдаемое: подстановка слова прекращается."""
    словарь = Dictionary(path=tmp_path / "dictionary.json")
    словарь.add_synonyms(["возчик", "перевозчик"])
    assert "перевозчик" in словарь.synonyms()["возчик"]

    словарь.remove_synonyms(["возчик", "перевозчик"])

    assert "возчик" not in словарь.synonyms(with_builtin=False)


def test_встроенные_группы_не_снимаются(tmp_path):
    """Они в коде и меняются с поставкой — снятие на месте создало бы расхождение."""
    словарь = Dictionary(path=tmp_path / "dictionary.json")
    встроенное = словарь.synonyms()
    слово = next(iter(встроенное))

    assert словарь.remove_synonyms([слово, *встроенное[слово]]) is False
    assert слово in словарь.synonyms()


def test_снятие_переживает_перезапуск(tmp_path):
    путь = tmp_path / "dictionary.json"
    словарь = Dictionary(path=путь)
    словарь.add_synonyms(["возчик", "перевозчик"])
    словарь.save()

    словарь.remove_synonyms(["возчик", "перевозчик"])
    словарь.save()

    assert Dictionary.load(путь).synonym_groups == []
