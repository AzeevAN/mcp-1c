"""Детерминированные контракты потокобезопасного Snowball stemmer."""

from __future__ import annotations

import threading

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
