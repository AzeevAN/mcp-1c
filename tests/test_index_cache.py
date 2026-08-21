"""Кэш поисковых индексов: круг «сохранить — поднять» и отказы.

Опасность кэша не в том, что он сломается заметно, а в том, что он тихо
отдаст устаревший индекс: поиск при этом работает и выглядит правдоподобно.
Поэтому тестов на отказ здесь больше, чем на успех.
"""

from __future__ import annotations

import os

import pytest

from mcp1c import index_cache


def test_поднятый_индекс_отвечает_как_построенный(tmp_path, sample_index, sample_payloads):
    path = tmp_path / "objects"
    index_cache.save(sample_index, path, source_sha256="abc", kind="objects")

    restored = index_cache.load(path, sample_payloads, source_sha256="abc", kind="objects")

    assert restored is not None
    for query in ("номенклатура", "реализация товаров", "Справочник.Номенклатура", "загрузка"):
        built = [(h.doc.id, round(h.score, 9)) for h in sample_index.search(query)]
        cached = [(h.doc.id, round(h.score, 9)) for h in restored.search(query)]
        assert cached == built, f"выдача разошлась на запросе «{query}»"


def test_полезная_нагрузка_подключается_заново(tmp_path, sample_index, sample_payloads):
    path = tmp_path / "objects"
    index_cache.save(sample_index, path, source_sha256="abc", kind="objects")

    restored = index_cache.load(path, sample_payloads, source_sha256="abc", kind="objects")

    assert restored.docs["Справочник.Номенклатура"].payload == "номенклатура"


def test_перевыгруженный_источник_обесценивает_кэш(tmp_path, sample_index, sample_payloads):
    path = tmp_path / "objects"
    index_cache.save(sample_index, path, source_sha256="старый", kind="objects")

    assert index_cache.load(path, sample_payloads, source_sha256="новый", kind="objects") is None


def test_правка_кода_обесценивает_кэш(tmp_path, sample_index, sample_payloads, monkeypatch):
    """Постинги зависят от токенизатора, весов полей и STEM_LENGTH.

    Тронули логику поиска — сохранённые постинги больше ей не отвечают, и
    подняться они не должны, иначе поиск тихо работает по старым правилам.
    """
    path = tmp_path / "objects"
    index_cache.save(sample_index, path, source_sha256="abc", kind="objects")

    monkeypatch.setattr(index_cache, "_code_digest", lambda: "код-стал-другим")

    assert index_cache.load(path, sample_payloads, source_sha256="abc", kind="objects") is None


def test_чужой_вид_индекса_не_поднимается(tmp_path, sample_index, sample_payloads):
    """Объекты и реквизиты — разные индексы, перепутать их нельзя."""
    path = tmp_path / "objects"
    index_cache.save(sample_index, path, source_sha256="abc", kind="objects")

    assert index_cache.load(path, sample_payloads, source_sha256="abc", kind="fields") is None


def test_обрезанный_файл_не_роняет_старт(tmp_path, sample_index, sample_payloads):
    path = tmp_path / "objects"
    index_cache.save(sample_index, path, source_sha256="abc", kind="objects")
    raw = path.read_bytes()
    path.write_bytes(raw[: len(raw) // 2])

    assert index_cache.load(path, sample_payloads, source_sha256="abc", kind="objects") is None


def test_распакованный_кэш_не_может_превысить_бюджет(tmp_path, monkeypatch):
    path = tmp_path / "blob"
    index_cache.save_blob(
        {"payload": "x" * 4096}, path, source_sha256="abc", kind="blob"
    )
    monkeypatch.setattr(index_cache, "MAX_CACHE_PAYLOAD_BYTES", 64, raising=False)

    assert index_cache.load_blob(path, source_sha256="abc", kind="blob") is None


def test_файл_кэша_не_читается_за_пределом_бюджета(tmp_path, monkeypatch):
    path = tmp_path / "blob"
    index_cache.save_blob(
        {"payload": "x" * 4096}, path, source_sha256="abc", kind="blob"
    )
    monkeypatch.setattr(index_cache, "MAX_CACHE_FILE_BYTES", 32, raising=False)

    assert index_cache.load_blob(path, source_sha256="abc", kind="blob") is None


def test_файл_без_заголовка_не_поднимается(tmp_path, sample_payloads):
    path = tmp_path / "objects"
    path.write_bytes("ни заголовка, ни блоба".encode("utf-8"))

    assert index_cache.load(path, sample_payloads, source_sha256="abc", kind="objects") is None


def test_отсутствующий_файл_это_промах_а_не_ошибка(tmp_path, sample_payloads):
    path = tmp_path / "нет-такого"

    assert index_cache.load(path, sample_payloads, source_sha256="abc", kind="objects") is None


def test_имя_файла_не_зависит_от_момента_записи(tmp_path):
    """Иначе каждая пересборка кладёт рядом новый файл, и каталог растёт."""
    first = index_cache.path_for(tmp_path, "РозницаДляКазахстана", "objects")
    second = index_cache.path_for(tmp_path, "РозницаДляКазахстана", "objects")

    assert first == second


def test_виды_индексов_не_делят_один_файл(tmp_path):
    objects = index_cache.path_for(tmp_path, "Розница", "objects")
    fields = index_cache.path_for(tmp_path, "Розница", "fields")

    assert objects != fields


def test_идентификатор_источника_не_ломает_путь(tmp_path):
    """Идентификатор — имя конфигурации, в нём бывает что угодно."""
    path = index_cache.path_for(tmp_path, "Учёт/Склад: копия", "objects")

    assert path.parent == tmp_path
    assert "/" not in path.name


def test_повторная_запись_перезаписывает_тот_же_файл(tmp_path, sample_index):
    path = index_cache.path_for(tmp_path, "Розница", "objects")
    index_cache.save(sample_index, path, source_sha256="abc", kind="objects")
    index_cache.save(sample_index, path, source_sha256="def", kind="objects")

    assert len(list(tmp_path.iterdir())) == 1


def test_после_записи_не_остаётся_временного_файла(tmp_path, sample_index):
    path = index_cache.path_for(tmp_path, "Розница", "objects")
    index_cache.save(sample_index, path, source_sha256="abc", kind="objects")

    assert [p.name for p in tmp_path.iterdir()] == [path.name]


def test_уборка_сносит_файлы_исчезнувшего_источника(tmp_path, sample_index):
    живой = index_cache.path_for(tmp_path, "Розница", "objects")
    забытый = index_cache.path_for(tmp_path, "УдалённаяКонфигурация", "objects")
    index_cache.save(sample_index, живой, source_sha256="abc", kind="objects")
    index_cache.save(sample_index, забытый, source_sha256="abc", kind="objects")

    removed = index_cache.sweep(tmp_path, keep=[живой.name])

    assert removed == [забытый.name]
    assert живой.exists()
    assert not забытый.exists()


def test_уборка_сносит_обрывок_прерванной_записи(tmp_path):
    (tmp_path / "Розница.objects.tmp").write_bytes(b"half")

    removed = index_cache.sweep(tmp_path, keep=["Розница.objects"])

    assert removed == ["Розница.objects.tmp"]


def test_уборка_молчит_когда_каталога_нет(tmp_path):
    assert index_cache.sweep(tmp_path / "ещё-не-создан", keep=[]) == []


@pytest.mark.skipif(os.geteuid() == 0, reason="под root права на каталог не мешают")
def test_уборка_не_роняет_старт_на_каталоге_только_для_чтения(tmp_path):
    """Том смонтирован только на чтение — старт из-за уборки падать не должен."""
    directory = tmp_path / "cache"
    directory.mkdir()
    (directory / "чужой.objects").write_text("мусор", encoding="utf-8")
    directory.chmod(0o500)
    try:
        assert index_cache.sweep(directory, keep=[]) == []
    finally:
        directory.chmod(0o700)


def test_блоб_с_несовпавшим_штампом_не_доходит_до_marshal(tmp_path, sample_payloads):
    """Порядок проверок: сначала заголовок, потом содержимое.

    `marshal` на испорченных данных способен уронить интерпретатор, и
    перехватить это уже нечем. Поэтому блоб с чужим штампом разбираться не
    должен вовсе — тест кладёт заведомо негодное содержимое.
    """
    path = tmp_path / "objects"
    header = b'{"cache_version":1,"kind":"objects","python":"0.0","marshal":4,"code":"x","source":"y"}'
    path.write_bytes(header + b"\n" + b"\x00\xff" * 64)

    assert index_cache.load(path, sample_payloads, source_sha256="abc", kind="objects") is None


def test_длинное_имя_укорачивается_и_остаётся_различимым():
    """Предел имени файла — 255 байт, а вызывающие приписывают к нашему своё.

    Кириллица весит два байта на букву, поэтому предел достижим на сотне
    символов. Два имени, совпадающие в начале, обязаны остаться разными:
    иначе две конфигурации делили бы один каталог кода.
    """
    длинное = "Розница" * 40
    другое = длинное + "Другая"

    первое = index_cache.safe_name(длинное)
    второе = index_cache.safe_name(другое)

    assert len(первое.encode("utf-8")) <= 120
    assert len(второе.encode("utf-8")) <= 120
    assert первое != второе
    # Короткие имена правило не трогает вовсе.
    assert index_cache.safe_name("Розница 2.3") == "Розница 2.3"
    # Одно и то же имя всегда даёт один результат: каталог кода должен
    # находиться после перезапуска, а не заводиться заново.
    assert index_cache.safe_name(длинное) == первое
