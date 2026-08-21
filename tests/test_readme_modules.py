"""Публичный README полностью описывает завершённый провайдер кода."""

from pathlib import Path


README = Path(__file__).parents[1] / "README.md"


def _text() -> str:
    return README.read_text(encoding="utf-8")


def test_readme_описывает_три_cli_команды_и_все_их_параметры():
    text = _text()
    section = text.split("## Отладка без агента — `mcp1c.cli`", 1)[1]
    section = section.split("\n## ", 1)[0]

    for command in (
        "reg-search-procedures",
        "reg-get-procedure",
        "reg-get-callers",
    ):
        assert command in section
    for option in (
        "--data",
        "--config",
        "--extension",
        "--scope",
        "--limit",
        "--start-line",
        "--lines",
    ):
        assert option in section


def test_readme_объясняет_потери_на_каждом_шаге_цепочки_кода():
    text = _text()
    section = text.split("## Порядок вызовов — и что теряется", 1)[1]
    section = section.split("\n## ", 1)[0].lower()

    assert "search_procedures" in section and "точн" in section
    assert "get_procedure" in section and "тело" in section
    assert "get_callers" in section and "последств" in section


def test_readme_разделяет_источник_кода_конфигурации_и_расширения():
    text = _text()
    section = text.split("## Источники независимы", 1)[1].split("\n## ", 1)[0]

    assert "| Код конфигурации |" in section
    assert "| Код расширения |" in section
    assert "без кода конфигурации" in section.lower()
    assert "без кода расширения" in section.lower()


def test_readme_имеет_отдельный_раздел_границ_провайдера():
    text = _text()
    section = text.split("## Чего провайдер не знает", 1)[1].split("\n## ", 1)[0]
    lowered = section.lower()

    assert "только по экспортным" in lowered
    assert "выполнить" in lowered and "строк" in lowered
    assert "скомпилирован" in lowered
    assert "плоск" in lowered and "структур" in lowered and "форм" in lowered
    assert "не разреш" in lowered and "модул" in lowered


def test_readme_объясняет_единую_диагностику_покрытия_кода():
    text = _text().lower()

    assert "готов с ограничениями" in text
    assert "полностью, частично и не прочитаны" in text
    assert "первые 20" in text and "число оставшихся" in text
    assert "list_configurations" in text
    assert "reg-list" in text
    assert "`/sources`" in text
    assert "нулевой счётчик" in text and "не доказывает отсутствие" in text


def test_readme_фиксирует_холодный_и_тёплый_замер_памяти_контейнера():
    text = _text()

    assert "2026-08-21" in text
    assert "1 142,9 МиБ" in text
    assert "1 130,6 МиБ" in text
    assert "1 198 469 120 Б" in text and "1 185 505 280 Б" in text
    assert "651,4 МиБ" in text
    assert "517,3 МиБ" in text
    assert "504,2 МиБ" in text
    assert "492,2 МиБ" in text
    assert "542 453 760 Б" in text and "528 736 256 Б" in text
    assert "667 008 КиБ" in text
    assert "669 056 КиБ" in text and "503 980 КиБ" in text
    assert "699,1 МиБ" in text and "469,4 МиБ" in text
    assert "125 с" in text and "19 с" in text
    assert "три источника кода" in text and "12 файлов кэша" in text
    assert "146 строк" in text
    assert "restart_count = 0" in text and "oom_killed = false" in text
    assert "ровно 0 строк" in text
    assert "`traceback`, `exception`, `critical` и `error`" in text
    assert "tools/lab/measure_container_memory.py --mode cold --data data" in text
    assert "tools/lab/measure_container_memory.py --mode warm --data data" in text
    assert "--timeout 300 --poll-interval 0.5" in text
    assert all(
        kind in text
        for kind in ("modules-toc", "modules-calls", "modules-forms", "modules-search")
    )
    assert "loaded_at" in text and "символическими ссылками" in text
    assert all(field in text for field in ("O_NOFOLLOW", "dir_fd", "mtime_ns"))
    assert "сам измеритель не удаляет и не заменяет источники" in text.lower()
    assert "сервер штатно атомарно обновляет" in text.lower()
    assert "runtime-`loaded_at`" in text
    assert "разрешает только этот переход" in text
    assert "72,12 с" in text and "после готовности `/health`" in text
    assert "кэш страниц" in text.lower()
    assert "mem_limit" in text
    assert "628 МБ" not in text
    assert "330 МБ" not in text
    assert "первый прогон" not in text.lower()
