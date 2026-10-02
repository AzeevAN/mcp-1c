"""Детерминированные контракты потокобезопасного Snowball stemmer."""

from __future__ import annotations

import threading
from pathlib import Path

from mcp1c import search


def test_stemmer_создаётся_отдельно_для_каждого_потока(monkeypatch):
    созданные: list[int] = []
    замок = threading.Lock()
    старт = threading.Barrier(9)
    ошибки: list[BaseException] = []

    class ЛокальныйСтеммер:
        def __init__(self):
            self.owner = threading.get_ident()

        def stemWord(self, token: str) -> str:
            assert threading.get_ident() == self.owner
            return f"stem:{token}"

    class ОбщийСтеммерЗапрещён:
        def stemWord(self, _token: str) -> str:
            raise AssertionError("вызван process-global stemmer")

    def фабрика():
        with замок:
            созданные.append(threading.get_ident())
        return ЛокальныйСтеммер()

    monkeypatch.setattr(search, "_STEMMER_FACTORY", фабрика, raising=False)
    monkeypatch.setattr(search, "_STEMMER_LOCAL", threading.local(), raising=False)
    monkeypatch.setattr(
        search,
        "_STEMMER",
        ОбщийСтеммерЗапрещён(),
        raising=False,
    )
    with search._STEM_CACHE_LOCK:
        search._STEM_CACHE.clear()

    def вычислить(index: int) -> None:
        try:
            старт.wait(timeout=3)
            assert search.stem(f"потоковоеслово{index}").startswith("stem:")
        except BaseException as error:
            with замок:
                ошибки.append(error)

    потоки = [threading.Thread(target=вычислить, args=(index,)) for index in range(8)]
    for поток in потоки:
        поток.start()
    старт.wait(timeout=3)
    for поток in потоки:
        поток.join(timeout=3)

    assert not any(поток.is_alive() for поток in потоки)
    assert ошибки == []
    assert len(созданные) == len(потоки)
    assert len(set(созданные)) == len(потоки)


def test_ленивая_заморозка_индекса_не_теряет_постинги_при_гонке(monkeypatch):
    """Второй поток не должен упаковать уже очищенные словари наполнения.

    Индекс публикуется незамороженным, и первая заморозка может прийти из
    двух потоков сразу: публикатор кэша (`export_state`) и запрос `search`.
    Первый поток останавливается между упаковкой токенов и основ — ровно там,
    где словарь токенов уже пуст, а `_dirty` ещё взведён.
    """
    assert search.__file__.startswith(str(Path(__file__).resolve().parents[1]))
    index = search.SearchIndex(
        [
            search.Doc(id="Справочник.Номенклатура", kind="catalog",
                       fields={"name": "Номенклатура"}),
            search.Doc(id="Справочник.Контрагенты", kind="catalog",
                       fields={"name": "Контрагенты"}),
        ]
    )
    исходная_упаковка = search.SearchIndex._pack
    первый_поток = threading.get_ident()
    упаковок_в_первом = 0
    первый_остановился = threading.Event()
    второй_закончил = threading.Event()
    ошибки: list[BaseException] = []

    def упаковка_с_паузой(self, postings):
        nonlocal упаковок_в_первом
        if threading.get_ident() == первый_поток:
            упаковок_в_первом += 1
            if упаковок_в_первом == 2:
                первый_остановился.set()
                # С замком второй поток ждёт первого и сюда не доходит, пауза
                # истекает по таймауту; без замка он успевает пройти заморозку
                # целиком по уже очищенному словарю токенов.
                второй_закончил.wait(timeout=0.5)
        return исходная_упаковка(self, postings)

    monkeypatch.setattr(search.SearchIndex, "_pack", упаковка_с_паузой)

    def второй() -> None:
        try:
            assert первый_остановился.wait(timeout=3)
            index.search("номенклатура")
        except BaseException as error:
            ошибки.append(error)
        finally:
            второй_закончил.set()

    поток = threading.Thread(target=второй)
    поток.start()
    index.export_state()
    поток.join(timeout=3)

    assert not поток.is_alive()
    assert ошибки == []
    состояние = index.export_state()
    assert "номенклатура" in состояние["token_keys"]
    assert состояние["exact"]
    assert [hit.doc.id for hit in index.search("номенклатура")][:1] == [
        "Справочник.Номенклатура"
    ]
