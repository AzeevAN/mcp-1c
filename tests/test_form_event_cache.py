"""Повторное чтение обработчиков не должно снова группировать события формы."""

from mcp1c.modules_index import Формы


class _CountReads:
    def __init__(self, values):
        self.values = values
        self.reads = 0

    def __getitem__(self, index):
        self.reads += 1
        return self.values[index]


def test_события_не_перегруппируются_на_каждый_обработчик(корень_кода):
    forms = Формы.построить(корень_кода)
    counter = _CountReads(forms._события_проц)
    forms._события_проц = counter
    address = "Справочник.Пример.Форма.ФормаЭлемента"
    expected = forms.состав(address).события
    first_reads = counter.reads
    assert first_reads > 0
    for _ in range(10):
        assert forms.обработчик(address, "приоткрытии") == expected["ПриОткрытии"]
        assert forms.обработчик(address, "НесуществующийОбработчик") is None
        assert forms.состав(address).события == expected
    assert counter.reads == first_reads


def test_правка_выданного_состава_не_меняет_обработчики_и_сохранённый_индекс(
    корень_кода, tmp_path,
):
    forms = Формы.построить(корень_кода)
    address = "Справочник.Пример.Форма.ФормаЭлемента"
    before = tmp_path / "before.marshal"
    after = tmp_path / "after.marshal"
    forms.записать(before)
    events = forms.состав(address).события
    events.clear()
    assert forms.обработчик(address, "ПриОткрытии") == ("OnOpen",)
    assert forms.состав(address).события["ПриОткрытии"] == ("OnOpen",)
    forms.записать(after)
    assert before.read_bytes() == after.read_bytes()
    restored = Формы.прочитать(after)
    assert restored.обработчик(address, "ПриОткрытии") == ("OnOpen",)
