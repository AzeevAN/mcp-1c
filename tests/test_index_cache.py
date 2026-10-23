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

    files = [p.name for p in tmp_path.iterdir() if p.name != index_cache.KEY_FILE_NAME]
    assert files == [path.name]


def test_после_записи_не_остаётся_временного_файла(tmp_path, sample_index):
    path = index_cache.path_for(tmp_path, "Розница", "objects")
    index_cache.save(sample_index, path, source_sha256="abc", kind="objects")

    # Рядом остаётся только ключ подписи кэша, временных файлов нет.
    assert sorted(p.name for p in tmp_path.iterdir()) == sorted(
        [path.name, index_cache.KEY_FILE_NAME]
    )


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


def test_уборка_не_сносит_временный_файл_живой_записи(tmp_path, sample_index, monkeypatch):
    """Старт владельца подметает кэш, пока фоновая сборка ещё пишет его.

    `restore()` запускает сборку индексов кода в фоне, а `index_cache.sweep`
    идёт следом в том же startup: временный `.<файл>.*.tmp` недописанного
    кэша он принимал за обрывок и удалял, `os.replace` падал, и кэш на этом
    старте не сохранялся. Гонка воспроизводится без случайности: уборка
    вклинивается ровно между записью временного файла и заменой.
    """
    path = index_cache.path_for(tmp_path, "Розница", "objects")
    настоящая_замена = os.replace
    убрано: list[str] = []

    def замена_с_уборкой(источник, цель):
        убрано.extend(index_cache.sweep(tmp_path, keep=[path.name]))
        настоящая_замена(источник, цель)

    monkeypatch.setattr(index_cache.os, "replace", замена_с_уборкой)

    assert index_cache.save(sample_index, path, source_sha256="abc", kind="objects") == path
    assert убрано == []
    assert path.is_file()


def test_уборка_не_сносит_временный_ключ_живой_записи(tmp_path, sample_index, monkeypatch):
    """Ключ подписи создаётся через временный файл и `os.link` — та же гонка."""
    path = index_cache.path_for(tmp_path, "Розница", "objects")
    настоящая_ссылка = os.link
    убрано: list[str] = []

    def ссылка_с_уборкой(источник, цель, *args, **kwargs):
        убрано.extend(index_cache.sweep(tmp_path, keep=[path.name]))
        настоящая_ссылка(источник, цель, *args, **kwargs)

    monkeypatch.setattr(index_cache.os, "link", ссылка_с_уборкой)

    assert index_cache.save(sample_index, path, source_sha256="abc", kind="objects") == path
    assert убрано == []


def _состарить(path, seconds=None):
    import time

    было = time.time() - (seconds or index_cache.TEMPORARY_GRACE_SECONDS + 60)
    os.utime(path, (было, было))


def test_уборка_сносит_обрывок_после_завершения_записи(tmp_path, sample_index):
    """Живая запись того же файла защищает только свои временные файлы на
    время записи: старый обрывок, переживший SIGKILL, уходит при уборке."""
    path = index_cache.path_for(tmp_path, "Розница", "objects")
    обрывок = tmp_path / f".{path.name}.abcd1234.tmp"
    обрывок.write_bytes(b"half")
    _состарить(обрывок)
    index_cache.save(sample_index, path, source_sha256="abc", kind="objects")

    assert index_cache.sweep(tmp_path, keep=[path.name]) == [обрывок.name]


def test_уборка_не_сносит_свежий_временный_файл_другого_процесса(tmp_path):
    """Гость (CLI `reg-add` при живом сервере) тоже пишет кэш: Source A,
    справку, ролевой индекс при restore. Отметка `live_write` процессная, и
    уборка владельца при reload видит временный файл гостя без отметки.
    Свежий `.*.tmp` моложе порога не удаляется; старый — обрывок."""
    свежий = tmp_path / ".Розница.objects.k3j4m5n6.tmp"
    свежий.write_bytes(b"writing")
    старый = tmp_path / ".Розница.fields.q1w2e3r4.tmp"
    старый.write_bytes(b"half")
    _состарить(старый)

    assert index_cache.sweep(tmp_path, keep=["Розница.objects"]) == [старый.name]
    assert свежий.exists()


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


@pytest.mark.parametrize('limit', ['file', 'payload', 'global-file-clamp', 'global-payload-clamp'])
def test_m7_load_blob_соблюдает_малые_caps_до_marshal(tmp_path, monkeypatch, limit):
    path = tmp_path / 'bounded-marker'
    assert index_cache.save_blob({'text': 'x' * 4096}, path, source_sha256='stamp', kind='blob') == path
    file_cap, payload_cap = 1 << 20, 1 << 20
    if limit == 'file':
        file_cap = 32
    elif limit == 'payload':
        payload_cap = 32
    elif limit == 'global-file-clamp':
        monkeypatch.setattr(index_cache, 'MAX_CACHE_FILE_BYTES', 32)
    else:
        monkeypatch.setattr(index_cache, 'MAX_CACHE_PAYLOAD_BYTES', 32)

    def forbidden_loads(_payload):
        pytest.fail('Cache за пределом бюджета не должен доходить до marshal.loads')

    monkeypatch.setattr(index_cache.marshal, 'loads', forbidden_loads)
    assert index_cache.load_blob(path, source_sha256='stamp', kind='blob',
        max_file_bytes=file_cap, max_payload_bytes=payload_cap) is None


def test_m7_load_blob_принимает_валидный_малый_подписанный_payload_с_caps(tmp_path):
    path = tmp_path / 'bounded-marker'
    payload = {'verified': ['one', 'two']}
    assert index_cache.save_blob(payload, path, source_sha256='stamp', kind='blob') == path
    assert index_cache.load_blob(path, source_sha256='stamp', kind='blob',
        max_file_bytes=path.stat().st_size, max_payload_bytes=1024) == payload
    assert index_cache.load_blob(path, source_sha256='stamp', kind='blob',
        max_file_bytes=None, max_payload_bytes=None) == payload


def test_m7_load_blob_не_читает_symlink_даже_с_правильной_подписью(tmp_path):
    path = tmp_path / 'bounded-marker'
    payload = {'verified': True}
    assert index_cache.save_blob(payload, path, source_sha256='stamp', kind='blob') == path
    outside = tmp_path / 'actual-marker'
    path.rename(outside)
    raw = outside.read_bytes()
    path.symlink_to(outside)
    assert index_cache.load_blob(path, source_sha256='stamp', kind='blob',
        max_file_bytes=4096, max_payload_bytes=4096) is None
    assert outside.read_bytes() == raw


@pytest.mark.skipif(not hasattr(os, 'mkfifo') or not hasattr(os, 'O_NONBLOCK'), reason='Нужны POSIX FIFO и O_NONBLOCK')
@pytest.mark.parametrize('fifo_target', ['marker', 'key'])
def test_m7_fifo_cache_marker_или_key_не_блокирует_bounded_load(tmp_path, fifo_target):
    import subprocess
    import sys
    from pathlib import Path

    path = tmp_path / 'bounded-marker'
    assert index_cache.save_blob({'verified': True}, path, source_sha256='stamp', kind='blob') == path
    target = path if fifo_target == 'marker' else tmp_path / index_cache.KEY_FILE_NAME
    target.unlink()
    os.mkfifo(target, 0o600)
    script = '''
import sys
from mcp1c import index_cache
assert index_cache.load_blob(sys.argv[1], source_sha256='stamp', kind='blob', max_file_bytes=4096, max_payload_bytes=4096) is None
print('cache-miss')
'''
    env = dict(os.environ, PYTHONPATH=str(Path(__file__).resolve().parents[1] / 'src'))
    try:
        result = subprocess.run([sys.executable, '-c', script, str(path)],
            env=env, capture_output=True, text=True, timeout=3)
    except subprocess.TimeoutExpired:
        pytest.fail('FIFO cache marker/key заблокировал read; дочерний процесс остановлен', pytrace=False)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == 'cache-miss'


def test_m11_load_blob_передаёт_декодеру_view_исходного_буфера_после_HMAC(
    tmp_path, monkeypatch
):
    import random

    path = tmp_path / "m11-view-cache"
    payload = {"rows": list(range(100)), "body": random.Random(11).randbytes(8192)}
    assert index_cache.save_blob(payload, path, source_sha256="m11", kind="blob") == path
    frame = path.read_bytes()
    _header, _mac, compressed = frame.split(b"\n", 2)
    comparisons, inputs = [], []
    original_compare = index_cache.hmac.compare_digest
    original_decoder = index_cache.zlib.decompressobj

    def compare(*args, **kwargs):
        accepted = original_compare(*args, **kwargs)
        comparisons.append(accepted)
        return accepted

    class ObservedDecoder:
        def __init__(self, decoder):
            self.decoder = decoder

        def decompress(self, block, *args, **kwargs):
            inputs.append((block, comparisons == [True]))
            return self.decoder.decompress(block, *args, **kwargs)

        def __getattr__(self, name):
            return getattr(self.decoder, name)

    def decoder(*args, **kwargs):
        return ObservedDecoder(original_decoder(*args, **kwargs))

    monkeypatch.setattr(index_cache.hmac, "compare_digest", compare)
    monkeypatch.setattr(index_cache.zlib, "decompressobj", decoder)
    assert index_cache.load_blob(path, source_sha256="m11", kind="blob") == payload
    assert comparisons == [True]
    assert inputs
    assert all(verified for _block, verified in inputs)
    assert all(isinstance(block, memoryview) for block, _verified in inputs)
    # View должен ссылаться на целый прочитанный frame, а не на отдельную
    # уже скопированную bytes-срезку сжатого blob.
    assert all(len(block.obj) == len(frame) for block, _verified in inputs)
    assert sum(block.nbytes for block, _verified in inputs) == len(compressed)
    assert b"".join(block for block, _verified in inputs) == compressed


def test_m11_save_blob_пишет_header_mac_blob_раздельно_без_склейки(
    tmp_path, monkeypatch
):
    import random

    # Создание .cache-key само пишет файл; оно закончено до счётчика writer.
    assert index_cache.save_blob(
        {"seed": True}, tmp_path / "m11-seed", source_sha256="seed", kind="blob"
    ) is not None
    path = tmp_path / "m11-chunks-cache"
    payload = {"body": random.Random(12).randbytes(16384), "rows": (1, 2, 3)}
    original_compress = index_cache.gzip.compress
    original_mkstemp = index_cache.tempfile.mkstemp
    original_fdopen = index_cache.os.fdopen
    blobs, chunks, descriptors = [], [], set()

    def compress(*args, **kwargs):
        blob = original_compress(*args, **kwargs)
        blobs.append(blob)
        return blob

    def mkstemp(*args, **kwargs):
        descriptor, temporary = original_mkstemp(*args, **kwargs)
        if kwargs.get("prefix") == f".{path.name}.":
            descriptors.add(descriptor)
        return descriptor, temporary

    class ObservedWriter:
        def __init__(self, stream):
            self.stream = stream

        def __enter__(self):
            self.stream.__enter__()
            return self

        def __exit__(self, *args):
            return self.stream.__exit__(*args)

        def write(self, block):
            chunks.append(block)
            return self.stream.write(block)

        def __getattr__(self, name):
            return getattr(self.stream, name)

    def fdopen(descriptor, *args, **kwargs):
        stream = original_fdopen(descriptor, *args, **kwargs)
        if descriptor in descriptors:
            return ObservedWriter(stream)
        return stream

    monkeypatch.setattr(index_cache.gzip, "compress", compress)
    monkeypatch.setattr(index_cache.tempfile, "mkstemp", mkstemp)
    monkeypatch.setattr(index_cache.os, "fdopen", fdopen)
    assert index_cache.save_blob(payload, path, source_sha256="m11", kind="blob") == path
    assert len(blobs) == 1
    blob = blobs[0]
    assert len(chunks) >= 3
    assert any(chunk is blob for chunk in chunks)
    assert max(map(len, chunks)) <= len(blob)
    frame = path.read_bytes()
    header, mac, persisted_blob = frame.split(b"\n", 2)
    assert header.startswith(b"{")
    assert len(mac) == 64
    assert persisted_blob == blob
    assert b"".join(chunks) == frame
    assert index_cache.load_blob(path, source_sha256="m11", kind="blob") == payload
