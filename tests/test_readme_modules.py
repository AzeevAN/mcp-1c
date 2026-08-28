"""Публичный README полностью описывает завершённый провайдер кода."""

from pathlib import Path


ROOT = Path(__file__).parents[1]


def _text(relative: str) -> str:
    return (ROOT / relative).read_text(encoding="utf-8")


def test_readme_описывает_три_cli_команды_и_все_их_параметры():
    text = _text("docs/operations.md")
    section = text.split("## Все команды `mcp1c.cli`", 1)[1]
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
    text = _text("docs/tools.md")
    section = text.split("## Рабочая последовательность для кода", 1)[1]
    section = section.split("\n## ", 1)[0].lower()

    assert "search_procedures" in section and "точн" in section
    assert "get_procedure" in section and "тело" in section
    assert "get_callers" in section and "последств" in section


def test_readme_разделяет_источник_кода_конфигурации_и_расширения():
    text = _text("docs/tools.md")
    section = text.split("## Источники независимы", 1)[1].split("\n## ", 1)[0]

    assert "| Код конфигурации |" in section
    assert "| Код расширения |" in section
    assert "без кода конфигурации" in section.lower()
    assert "без кода расширения" in section.lower()


def test_readme_имеет_отдельный_раздел_границ_провайдера():
    text = _text("docs/tools.md")
    section = text.split("## Границы провайдера кода", 1)[1].split("\n## ", 1)[0]
    lowered = section.lower()

    assert "экспортные процедуры" in lowered
    assert "не выполняет bsl" in lowered and "строк" in lowered
    assert "скомпилирован" in lowered
    assert "плоской структурой" in lowered and "форм" in lowered
    assert "динамические вызовы" in lowered and "модуль" in lowered


def test_readme_объясняет_единую_диагностику_покрытия_кода():
    text = _text("docs/operations.md").lower()

    assert "готов с ограничениями" in text
    assert "полностью, частично и не прочитаны" in text
    assert "первые 20" in text and "число оставшихся" in text
    assert "list_configurations" in text
    assert "reg-list" in text
    assert "`/sources`" in text
    assert "нулевой счётчик" in text and "не доказывает отсутствие" in text


def test_readme_описывает_строгий_roundtrip_кэша_кода():
    text = _text("docs/operations.md").lower()
    section = text.split("## выгрузка конфигурации в файлы", 1)[1]
    section = section.split("\n## ", 1)[0]

    assert "локаторы, агрегаты покрытия" in section
    assert "тела модулей" in section and "сырая запись формы" in section
    assert "семантически повреждён" in section
    assert "read-only" in section and "пересбор" in section
    assert "cold" in section and "warm" in section and "совпада" in section
    assert "256 миб" in section and "512 миб" in section
    assert "локальные причины каждой формы" in section


def test_документация_различает_zip_структуры_и_архив_конфигуратора():
    readme = _text("README.md")
    dashboard = _text("dashboard/README.md")
    operations = _text("docs/operations.md")

    for text in (readme, dashboard, operations):
        assert "Выгрузить конфигурацию в файлы…" in text
        assert "Архив" in text
        assert "СтруктураКонфигурации_*.zip" in text
        assert "data/incoming/" in text

    assert "только `data/incoming/`" in readme
    assert "без родительской конфигурации" in dashboard
    assert "не передаётся через форму «Загрузить»" in operations
    assert "кнопка «Разобрать» не показывается" in operations


def test_readme_фиксирует_холодный_и_тёплый_замер_памяти_контейнера():
    text = _text("docs/architecture.md")

    assert "2026-08-21" in text
    assert "1 339,6 МиБ" in text
    assert "1 335,7 МиБ" in text
    assert "1 404 649 472 Б" in text and "1 400 582 144 Б" in text
    assert "744,3 МиБ" in text
    assert "584,5 МиБ" in text
    assert "571,4 МиБ" in text
    assert "556,8 МиБ" in text
    assert "612 851 712 Б" in text and "599 142 400 Б" in text
    assert "762 180 КиБ" in text
    assert "764 228 КиБ" in text and "570 180 КиБ" in text
    assert "728,3 МиБ" in text and "533,2 МиБ" in text
    assert "111,216 с" in text and "11,495 с" in text
    assert "четыре источника кода" in text and "16 файлов кэша" in text
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
    assert "loaded_at" in text and "O_NOFOLLOW" in text
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
