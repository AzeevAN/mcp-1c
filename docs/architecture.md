# Архитектура и проверка

Сервер — один Python-процесс без внешнего сервиса базы данных. При старте он
восстанавливает `Registry`, поднимает поисковые индексы и обслуживает MCP,
health, административный API и выбранный дашборд. Необязательная общая
справка подключается отдельным read-only файлом SQLite.

## Основные модули

```text
src/mcp1c/
├── server.py             MCP SDK, HTTP-граница, токены, запуск
├── tools.py              одиннадцать основных операций MCP
├── reference_provider.py опциональная schema v1 и две справочные операции
├── registry.py           источники, поколения и атомарная публикация
├── loader.py             строгая schema v1 XML/JSON
├── syntax_parser.py      справка платформы
├── search.py             индекс и ранжирование
├── graph.py              связи метаданных
├── modules_index.py      процедуры, вызовы, формы и get_callers
├── index_cache.py        проверяемый расходный кэш
├── intake.py             безопасный разбор incoming-архивов
├── dictionary.py         встроенный и локальный словарь
├── dashboard_backend.py  auth, загрузка, задания и общие операции SPA
├── dashboard_runtime.py  режим on/off, JSON API и раздача SPA
└── runtime_config.py     проверка dashboard/access и Docker-токенов
```

`server.py` — единственный слой, зависящий от официального MCP SDK.
Предметные операции находятся ниже и тестируются без сетевого транспорта.

## Жизненный цикл запроса

1. Клиент инициализирует Streamable HTTP endpoint `/mcp`.
2. Внешний guard проверяет токен и предел HTTP-тела до parsing.
3. MCP SDK валидирует вызов по схеме инструмента.
4. `tools.py` берёт один неизменяемый снимок Registry.
5. Индекс выполняет поиск или строит карточку.
6. Ответ содержит только запрошенный фрагмент и предупреждения его источников.

Снимок создаётся под короткой блокировкой. Долгое чтение файлов и построение
индекса выполняются вне неё; публикация нового поколения происходит атомарно.
Так читатель не видит смесь старых и новых словарей одного Registry.

## Хранение и кэш

Авторитетные данные:

- `registry.json` и сохранённые исходники;
- разобранный индекс справки, если исходный `.hbk` после загрузки не сохранён;
- опубликованные модули и расширения;
- локальный `dictionary.json`;
- необязательный подписанный `reference/reference.mcp1cref` и извлечённая из
  него после проверки SQLite в `index/reference/databases/`.

Расходный поисковый кэш имеет штамп из версии Python, формата `marshal`,
SHA-256 пакета `src/mcp1c/*.py` и SHA-256 источника. Штамп проверяется до
десериализации. Несовпадение, повреждение или невозможность записи приводят к
перестроению, а не к остановке сервера.

Имена файлов кэша детерминированы. На старте удаляются только файлы, которые
не принадлежат ни одному заявленному источнику.

Общая справка не входит в Registry. При каждом запуске адаптер заново
проверяет файл и при успехе регистрирует две дополнительные MCP-операции.
Её производный кэш хранится в `index/reference/`, поэтому уборка Registry не
удаляет его; SHA-256 файла и общий отпечаток кода всё равно делают устаревший
кэш промахом. Ошибка базы оставляет сервер с одиннадцатью основными
операциями, а не роняет запуск.

## Безопасность файлов

- Архивы ограничены по числу записей, размеру одной записи, суммарному
  распакованному объёму и коэффициенту сжатия.
- ZIP проверяется по центральному каталогу и повторно при потоковом чтении.
- V8 raw deflate распаковывается только до ближайшего предела.
- Публикация источника использует `dir_fd`, `O_NOFOLLOW`, эксклюзивный
  временный файл, `fsync` и атомарный `replace`.
- Имена в архивах не могут выйти из целевого каталога.
- Контейнер работает как `10001:10001`, без Linux capabilities и с
  `no-new-privileges`.
- Bind mount не создаётся Compose автоматически; старт доказывает запись
  фактическим созданием и удалением пробного файла.

## Зависимости

Runtime-зависимости изолированы по назначению:

| Пакет | Причина |
|---|---|
| `mcp` | официальный протокол и транспорт |
| `cryptography` | проверенная Ed25519 detached-подпись manifest общей справки |
| `numpy` | компактные постинги и быстрые битовые операции поиска |
| `snowballstemmer` | русская морфология, которой нет в stdlib |

Читаемые входы зависимостей имеют hash-locked варианты:

| Контур | Вход | Lock |
|---|---|---|
| runtime и Docker | `requirements.txt` | `requirements-lock.txt` |
| тесты | `requirements-dev.txt` | `requirements-dev-lock.txt` |
| wheel/sdist | `requirements-build.txt` | `requirements-build-lock.txt` |
| audit и SBOM | `requirements-audit.txt` | `requirements-audit-lock.txt` |
| генератор lock | `requirements-lock-tool.in` | `requirements-lock-tool.txt` |

Docker устанавливает runtime через `pip --require-hashes`. Базовые образы
закреплены тегом и digest; CI проверяет runtime lock через `pip-audit` и
создаёт CycloneDX JSON SBOM.

Production-сборка SPA хранится в `src/mcp1c/dashboard_dist/` как package data.
`tools/sync_dashboard_assets.py --check` доказывает совпадение с
`dashboard/dist/`, а `tools/check_dashboard_artifacts.py` сравнивает состав и
SHA-256 файлов в source tree, wheel, sdist и wheel, повторно собранном из
sdist. Node.js используется только в frontend build job и Docker build stage;
в Python-пакете и runtime image его нет.

## Изоляция контекста Docker-сборки

Production image никогда не собирается из произвольного содержимого рабочей
папки. Локальная команда `python3 tools/build_image.py mcp1c:local` сначала
требует чистый checkout, затем передаёт в `docker build` поток `git archive
HEAD`. Поэтому ignored-файлы — `data/`, `.env`, ключи, локальные исследования,
частные корпуса и агентские настройки — вообще не являются входом BuildKit.

Release workflow использует Git context `{{defaultContext}}` action сборки,
то есть читает проверенный `GITHUB_SHA`, а не checkout workspace runner. Перед
сборкой workflow отдельно доказывает совпадение SHA release tag, `GITHUB_SHA`
и `HEAD`, а также принадлежность коммита `main`.

Вторая независимая граница — deny-by-default `.dockerignore`: сначала закрыт
весь контекст, затем явно разрешены только `requirements-lock.txt`, Python
runtime под `src/mcp1c/` и минимальные входы Vite под `dashboard/`. `Dockerfile`
не содержит `COPY .` или `ADD`. Приёмочный скрипт после сборки сравнивает полный
список файлов `/app` и `/data` с manifest отслеживаемых `src/mcp1c` плюс
runtime lock; одного поиска известных опасных расширений недостаточно.

## Публикация OCI-образа

`.github/workflows/release-image.yml` не имеет `push` или ручного запуска и
срабатывает только после публикации стабильного GitHub Release. До registry
доходит Git context точного tag SHA; tag, `pyproject.toml`, `__version__`,
`compose.yaml` и `.env.example` должны называть одну версию v2+. Один OCI index
содержит `linux/amd64` и `linux/arm64`, SemVer-теги и `latest`; BuildKit
прикрепляет SPDX SBOM и provenance уровня `mode=max`.

[GHCR создаёт первый package приватным](https://docs.github.com/en/packages/learn-github-packages/configuring-a-packages-access-control-and-visibility).
После первого publish владелец один раз переводит `mcp-1c` в Public в Package
settings и повторяет неуспешный workflow. Финальный job работает без registry
credentials и обязан скачать точный digest анонимно; пока это не прошло,
установочный образ не считается опубликованным. Переход Public необратим,
поэтому выполняется только в рамках явно разрешённого релиза.

## Воспроизводимые проверки

Python:

```bash
python3 -m venv .venv
.venv/bin/pip install --require-hashes -r requirements-dev-lock.txt
.venv/bin/python -m pytest
```

Тесты не читают рабочий `data/`: минимальные выгрузки, справка и код создаются
синтетически. Качество поиска измеряется отдельно через `mcp1c.bench`, потому
что изменение ранжирования нельзя принимать по одному процентному порогу.

SPA:

```bash
cd dashboard
npm ci
npm test
npm run typecheck
npm run build
cd ..
.venv/bin/python tools/sync_dashboard_assets.py --check
```

Compose без запуска контейнера:

```bash
docker compose -f compose.yaml config --quiet
```

Docker runtime проверяется четырьмя сочетаниями одного image ID:
`on/off` × `local/https-proxy`. В каждом запуске проверяются состояние
`healthy`, UID/GID процесса, запись в `/data`, `/health`, MCP, наличие или
отсутствие UI и сохранение Registry после пересоздания. Порт хоста остаётся
привязан к loopback; внешний HTTPS proxy не входит в Compose.

Полная изолированная матрица использует только временный `tmpfs /data`,
поднимает временный TLS proxy и удаляет свои контейнеры после проверки:

```bash
python3 tools/build_image.py mcp1c:accept
.venv/bin/python tools/lab/accept_universal_image.py mcp1c:accept
```

Между четырьмя режимами образ не пересобирается; скрипт сравнивает один image
ID, MCP version/tools, `on/off`, forwarded headers, Secure cookie, плохие
значения режимов, обязательный token contract и точный manifest файлов
runtime. Рабочий `data/` он не монтирует.

## Измерения

Проект не назначает `mem_limit` по одному локальному пику: cold-разбор, warm
старт и состав источников имеют разные профили. Полные команды измерителя:

```bash
PYTHONPATH=src .venv/bin/python tools/lab/measure_container_memory.py \
  --mode cold --data data --timeout 300 --poll-interval 0.5
PYTHONPATH=src .venv/bin/python tools/lab/measure_container_memory.py \
  --mode warm --data data --timeout 300 --poll-interval 0.5
```

Измеритель не удаляет и не заменяет источники. Сервер штатно атомарно
обновляет расходный кэш; проверка разрешает только этот переход и отдельно
сверяет `loaded_at`, символические ссылки, restart count, OOM и строки ошибок.

### Текущий срез провайдера кода — 2026-08-21

Текущий производственный срез основной обезличенной конфигурации на
2026-08-21:
**137 116 процедур**, **619 029 мест вызова**, **3 194 формы**,
**89 528 элементов форм** и **24 202 строки привязки событий**.

```bash
MODULES_ROOT=/путь/к/выгрузке
.venv/bin/python tools/lab/measure_modules_cache.py "$MODULES_ROOT"
```

Измеритель печатает JSON-агрегаты `procedures`, `calls`, `forms`, `elements` и
`event_rows`, не выводя имя или путь корпуса.

Исторический снимок прототипа от 2026-08-20 отделён от текущих агрегатов в
[modules-provider-design.md](modules-provider-design.md). Он сохраняет цену
четырёх слоёв индекса и причины выбора компактных структур.

### Приёмка selection v4 — 2026-08-21 и 2026-08-24

Четыре источника кода проверяются агрегатным скриптом:

```bash
PYTHONPATH=src .venv/bin/python \
  tools/lab/measure_modules_acceptance.py --data data --timeout 120
```

Warm-подъём занял 3,676 с, `startup_problems_total = 0`, пик процесса —
856,2 МиБ. Каталог содержал 8 650 иерархических `.bsl`, 828 модулей из
`Form.bin`, 1 523 плоских `.txt`, 1 079 модулей из `.Form` и 7
скомпилированных модулей без исходника. Из 6 150 форм полностью прочитана
структура 3 331, частично — 1 330, не прочитана — 1 489.

Повторный прогон schema v2 2026-08-24 на двух доступных обезличенных корпусах
разрешил 97 289 BSL-рёбер из 178 216 (54,59%). Все 1 079 читаемых записей
`module` плоских `.Form` открылись; медиана повторного чтения составила
0,052–0,067 мс, p95 — 0,271–0,346 мс. Первый проход не называется cold:
скрипт не управляет файловым кэшем ОС.

### Selection v5 — происхождение структуры, 2026-08-29

Пятый вариант сохраняет весь состав v4 и добавляет в атомарный корень кода
производный `.structure-origin.json.gz`. Для основной выгрузки это каталог
семантических адресов, для расширения — только доказанная разность с SHA-256
базового поколения. Исходные XML объектов и ZIP по-прежнему не копируются.
Старый корень v4 без этого файла остаётся пригодным для инструментов кода, но
incoming помечает его как требующий явного переразбора; происхождение до него
остаётся `unknown`.

Синтетическая приёмка покрывает обе раскладки файловой выгрузки, собственный
объект, добавленный реквизит, конфликт двух расширений, смену базового
поколения, restore после удаления ZIP и отказ записи без подмены прежнего
поколения. Живые источники при реализации не переразбирались.

### Отзывчивость и память контейнера — 2026-08-21

На холодном кэше трёх источников кода за окно фоновой сборки 72,12 с выполнено
по 73 секундных вызова `/health` и `search_objects`, отказов не было. Таймер
начинается после готовности `/health`:

```bash
.venv/bin/python tools/lab/measure_background_responsiveness.py \
  http://127.0.0.1:5002 /путь/к/копии/data
```

Финальный замер: четыре источника кода и 16 файлов кэша:

| Состояние | `memory.peak` cgroup | `memory.current` после `ready` | RSS PID 1 | HWM PID 1 | `docker stats` |
|---|---:|---:|---:|---:|---:|
| холодная пересборка | 1 404 649 472 Б (1 339,6 МиБ) | 1 400 582 144 Б (1 335,7 МиБ) | 762 180 КиБ (744,3 МиБ) | 764 228 КиБ (746,3 МиБ) | 728,3 МиБ |
| тёплый старт | 612 851 712 Б (584,5 МиБ) | 599 142 400 Б (571,4 МиБ) | 570 180 КиБ (556,8 МиБ) | 570 180 КиБ (556,8 МиБ) | 533,2 МиБ |

Холодный подъём занял 111,216 с, warm — 11,495 с. В обоих случаях
`restart_count = 0`, `oom_killed = false` и ровно 0 строк с каждым из терминов
`traceback`, `exception`, `critical` и `error`. Cgroup включает кэш страниц,
поэтому не равен RSS или `docker stats`; цифра не является основанием для
универсального `mem_limit`.

```bash
.venv/bin/python tools/lab/measure_container_memory.py --mode cold --data data \
  --timeout 300 --poll-interval 0.5
.venv/bin/python tools/lab/measure_container_memory.py --mode warm --data data \
  --timeout 300 --poll-interval 0.5
```

Cold удаляет только точные файлы `modules-toc`, `modules-calls`,
`modules-forms` и `modules-search`. Каталог и цели проверяются через
`O_NOFOLLOW`, `dir_fd`, `dev`, `ino` и `mtime_ns`. Сам измеритель не удаляет и не заменяет источники, распакованный код или `registry.json`.
Сервер штатно атомарно обновляет runtime-`loaded_at`, а проверка разрешает только этот переход.

### Качество поиска — 2026-08-29

| Набор | Запросов | P@1 | P@3 | P@5 | P@10 | MRR | Отрыв |
|---|---:|---:|---:|---:|---:|---:|---:|
| Метаданные | 21 | 81,0% | 90,5% | 90,5% | 95,2% | 0,862 | 93,2% |
| Процедуры модулей | 6 | 0% | 0% | 0% | 16,7% | 0,024 | 0% |
| Точные имена справки | 50 926 | 97,1% | 98,3% | 98,7% | 98,9% | 0,978 | 91,7% |
| Одноимённые | 10 544 | 98,7% | 99,8% | 99,9% | 100% | 0,992 | 93,9% |

Вся таблица снята точной командой ниже. Три ручных набора собраны из живых
формулировок и промахов, два последних строятся из самих данных:

```bash
PYTHONPATH=src .venv/bin/python -m mcp1c.bench \
  --data data --config ИмяКонфигурации \
  --auto --sets roznica-metadata,modules-procedures \
  --check-notes
```

Строка процедур отдельно воспроизводится с `--sets modules-procedures`. Это
baseline для улучшений, а не автоматический порог приёмки.

Архитектурные контракты отдельных подсистем:

- [schema-v1.md](schema-v1.md) — структура выгрузки;
- [data-sources.md](data-sources.md) — происхождение каждого сведения;
- [dashboard-design.md](dashboard-design.md) — HTTP/UI;
- [modules-intake-design.md](modules-intake-design.md) — приём кода;
- [modules-provider-design.md](modules-provider-design.md) — индексы кода.
