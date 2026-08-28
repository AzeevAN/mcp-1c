# MCP-сервер структуры конфигураций 1С

Локальный справочник для агентов, которые пишут BSL: метаданные конфигураций,
тексты модулей и форм, связи объектов, справка платформы и язык запросов 1С.
Сервер работает одним Python-процессом без внешней базы данных.

Репозиторий не содержит справку фирмы «1С», выгрузки конфигураций и их
производные индексы. Всё состояние конкретной установки находится в отдельном
каталоге `data/` и не попадает в git.

## Состояние — 2026-08-29

| Контур | Состояние |
|---|---|
| Транспорт | Streamable HTTP `/mcp` и локальный `stdio`; SSE удалён |
| Метаданные | schema v1 XML/JSON, граф, карточки, виртуальные таблицы |
| Код и расширения | процедуры, тела, формы, места вызовов, происхождение объектов и полей; отдельный сеансовый снимок активности |
| Синтаксис | справки нескольких версий платформы плюс язык запросов |
| Дашборд | рабочая SPA и классический совместимый UI |
| Авторизация | `API_TOKEN` на чтение, `ADMIN_TOKEN` на запись |
| Тесты | `.venv/bin/python -m pytest`, 1804 |

Воспроизводимый прогон:

```bash
.venv/bin/pip install --require-hashes -r requirements-dev-lock.txt
.venv/bin/python -m pytest          # 1804 теста (прогон 2026-08-29)
```

## Навигация

| Раздел | Где читать |
|---|---|
| Полный Docker/Linux runbook | [ниже в этом README](#запуск-в-docker) |
| Дашборд: classic и SPA | [dashboard/README.md](dashboard/README.md) |
| Конфигурации MCP-клиентов | [docs/clients.md](docs/clients.md) |
| Все инструменты и порядок вызовов | [docs/tools.md](docs/tools.md) |
| Источники, CLI, bench и ручной сервер | [docs/operations.md](docs/operations.md) |
| Архитектура, кэш и проверки | [docs/architecture.md](docs/architecture.md) |
| Обработка выгрузки для 1С | [exporter-1c/README.md](exporter-1c/README.md) |

---

# Запуск в Docker

Ниже описан полный путь от чистого Linux-сервера до работающего контейнера.
Критичные шаги не вынесены во внешнюю документацию: права, три режима, токены,
healthcheck и удалённый HTTPS можно настроить по этому разделу.

## Что запускается

Базовый [docker-compose.yml](docker-compose.yml):

- собирает target `runtime-core`;
- запускает процесс как UID/GID `10001:10001`;
- монтирует подготовленный каталог хоста в `/data`;
- публикует порт только на `127.0.0.1`;
- включает `no-new-privileges` и удаляет все Linux capabilities;
- ограничивает Docker JSON-логи тремя файлами по 10 МиБ;
- запускает MCP без дашборда.

Два override-файла включают интерфейс:

| Вариант | Дополнительный файл | Образ |
|---|---|---|
| Без дашборда | нет | `mcp1c:latest`, `runtime-core` |
| Классический дашборд | `docker-compose.classic.yml` | тот же `runtime-core` |
| SPA | `docker-compose.dashboard.yml` | `mcp1c:dashboard`, `runtime-dashboard` |

`docker-compose.remote.yml` добавляется последним к любому варианту, если
сервер доступен с другой машины через HTTPS reverse proxy.

## Требования

- 64-битный Linux или Docker Desktop;
- Docker Engine с Compose v2 (`docker compose`, не старый `docker-compose`);
- Git и `curl` для установки и проверки;
- свободный локальный порт `5001` либо другое значение `MCP1C_PORT`;
- место под исходники и индексы в отдельном каталоге.

Проверка:

```bash
docker version
docker compose version
git --version
curl --version
```

На Linux пользователь развёртывания должен иметь доступ к Docker. Добавление в
группу `docker` фактически даёт административные права на машину; принимайте
это решение осознанно или запускайте команды Docker через `sudo`.

## 1. Получить проект

```bash
git clone https://github.com/AzeevAN/mcp-1c.git
cd mcp-1c
```

Для обновления существующей установки используйте только ожидаемую ветку и
проверяйте изменения перед сборкой:

```bash
git status --short --branch
git pull --ff-only
```

## 2. Подготовить окружение

```bash
cp .env.example .env
```

Основные значения `.env`:

```dotenv
MCP1C_DATA_DIR=/srv/mcp1c/data
MCP1C_PORT=5001
API_TOKEN=
ADMIN_TOKEN=
```

Для локального loopback-запуска токены можно оставить пустыми. Для сервера,
к которому обращаются другие машины, оба токена обязательны и должны быть
разными.

Сгенерируйте два значения и вручную перенесите их в `.env`:

```bash
python3 -c "import secrets; print(secrets.token_urlsafe(32))"
python3 -c "import secrets; print(secrets.token_urlsafe(32))"
```

`.env` находится в `.gitignore`. Не добавляйте реальные токены в Compose,
README, shell history или конфиги, которые публикуются вместе с проектом.

Переменные:

| Переменная | Назначение | По умолчанию |
|---|---|---|
| `MCP1C_DATA_DIR` | bind source на машине Docker | `./data` |
| `MCP1C_PORT` | loopback-порт хоста | `5001` |
| `API_TOKEN` | чтение MCP и дашборда | пусто: чтение открыто достигшему адреса клиенту |
| `ADMIN_TOKEN` | загрузка, удаление, incoming, словарь, reload | пусто: маршруты записи отключены |
| `MCP1C_DASHBOARD` | `off`, `classic`, `spa` | задаётся выбранным Compose-файлом |
| `MCP1C_DASHBOARD_DIST` | готовая статика SPA | внутри образа `/app/dashboard/dist` |

`MCP1C_DASHBOARD` не нужно записывать в `.env`: явный override делает режим
видимым в самой команде запуска.

## 3. Подготовить каталог данных на Linux

Это обязательный шаг. Compose использует bind mount и намеренно содержит
`create_host_path: false`: отсутствующий каталог останавливает запуск вместо
того, чтобы Docker молча создал root-owned `data/`.

Контейнер работает как `10001:10001`. Для обычного rootful Docker на Linux:

```bash
sudo install -d -o 10001 -g 10001 -m 0750 /srv/mcp1c/data
sudo install -d -o 10001 -g 10001 -m 0750 /srv/mcp1c/data/bootstrap
sudo install -d -o 10001 -g 10001 -m 0750 /srv/mcp1c/data/incoming
```

Проверка владельца и режима:

```bash
stat -c '%u:%g %a %n' \
  /srv/mcp1c/data \
  /srv/mcp1c/data/bootstrap \
  /srv/mcp1c/data/incoming
```

Ожидается `10001:10001 750` для всех трёх строк.

Почему недостаточно `mkdir -p data/bootstrap`: каталог получит UID
пользователя развёртывания или `root`, а процесс UID `10001` сможет читать его,
но не создавать `registry.json`, `sources/`, `index/` и журналы. `chown`,
выполненный при сборке образа, не помогает: bind mount перекрывает
подготовленный внутри образа `/data`.

Не исправляйте это командами `chmod -R 777` и не запускайте контейнер от root.
Данные конфигураций должны оставаться закрытыми, а non-root процесс — частью
защиты.

### Локальный каталог внутри проекта

На чистом Linux вместо `/srv` можно использовать `./data`:

```bash
mkdir -p data
sudo chown -R 10001:10001 data
sudo chmod 0750 data
```

Тогда оставьте `MCP1C_DATA_DIR=./data`.

### Docker Desktop

На macOS и Windows Docker Desktop использует файловое посредничество своей
Linux VM, поэтому числовой владелец на хосте может отличаться от
`10001:10001`. Создайте каталог явно, но не меняйте владельца без причины:

```bash
mkdir -p data/bootstrap data/incoming
```

Фактическую запись всё равно проверит контейнер перед стартом.

### Rootless Docker и user namespace remap

При remap числовой UID хоста может отличаться от UID внутри контейнера. Не
применяйте `chown 10001:10001` вслепую. После сборки проверьте отображение
коротким запуском с тем же bind mount:

```bash
docker compose build mcp1c
docker run --rm \
  --entrypoint sh \
  --mount type=bind,src=/srv/mcp1c/data,dst=/data \
  mcp1c:latest \
  -c 'id; test -w /data'
```

Если `test` возвращает ненулевой код, назначьте host UID/GID согласно
настройке `subuid`/`subgid` вашего Docker daemon либо используйте обычный
rootful контур. Сам сервер в сообщении печатает фактические UID/GID процесса.

### SELinux

Bind mount помечен `selinux: Z`: на SELinux-хосте Compose выдаёт каталогу
приватную метку контейнера, на остальных системах параметр не влияет. Если
политика организации запрещает автоматическое relabel, согласуйте постоянный
контекст каталога с администратором вместо отключения SELinux.

## 4. Положить начальные источники

### Не перепутайте два разных ZIP из 1С

Слова «выгрузка конфигурации» относятся здесь к двум разным операциям. Архивы
не взаимозаменяемы и загружаются разными путями:

| Что за файл | Как получить | Что внутри | Куда передать |
|---|---|---|---|
| `СтруктураКонфигурации_*.zip` | внешняя обработка проекта `ВыгрузкаСтруктурыКонфигурации` | `manifest.xml` или `manifest.json`, затем `objects/*.xml` или `objects/*.json` | форма загрузки дашборда; также `bootstrap/` или `mcp1c.cli reg-add` |
| `СнимокРасширений_*.json` | отдельная обработка из `exporter-1c/`, запущенная в нужном сеансе | зарегистрированные, действующие и не применённые расширения | форма загрузки дашборда или `mcp1c.cli reg-add`; сначала нужна структура конфигурации |
| ZIP выгрузки конфигурации в файлы | **Конфигуратор → Конфигурация → Выгрузить конфигурацию в файлы… → Архив** | `Configuration.xml`, модули, формы и другие файлы конфигурации | только `data/incoming/`, затем кнопка «Разобрать» |
| `shcntx_ru.hbk` и `shquery_ru.hbk` | каталог установленной платформы 1С | справка платформы и язык запросов | форма загрузки дашборда; также `bootstrap/` или `mcp1c.cli reg-add` |

ZIP из `incoming/` не создаёт конфигурацию в Registry: он добавляет модули и
формы к уже загруженной структуре, а также строит компактный каталог
происхождения объектов и прямых нессылочных реквизитов расширений. Поэтому
сначала загрузите через дашборд
`СтруктураКонфигурации_*.zip`, убедитесь, что конфигурация появилась на странице
«Источники», и только затем разбирайте относящийся к ней incoming-архив. Если
конфигураций нет, кнопки «Разобрать» нет намеренно — код не к чему привязать.

Основную файловую выгрузку разбирайте раньше выгрузок расширений. Доказательство
расширения привязано к SHA-256 обоих архивов и сохраняется вместе с его кодом;
исходный ZIP после этого не нужен. Если базы ещё не было или её переразобрали,
`get_object` честно показывает `происхождение неизвестно`: повторно разберите
соответствующее расширение, чтобы построить дельту к новому поколению базы.

Не подходят ни `.cf` из команды «Сохранить конфигурацию в файл», ни `.dt`
выгрузки информационной базы. Для `incoming/` в диалоге «Выгрузить конфигурацию
в файлы…» нужно выбрать именно вариант **«Архив»**, а не каталог XML-файлов.

В `bootstrap/` принимаются:

- ZIP структуры конфигурации по schema v1;
- `shcntx_ru.hbk` — справка платформы;
- `shquery_ru.hbk` — язык запросов.

На Linux копируйте сразу с владельцем контейнера:

```bash
sudo install -o 10001 -g 10001 -m 0640 \
  /путь/к/СтруктураКонфигурации.zip \
  /srv/mcp1c/data/bootstrap/

sudo install -o 10001 -g 10001 -m 0640 \
  /opt/1cv8/8.3.X.Y/shcntx_ru.hbk \
  /srv/mcp1c/data/bootstrap/

sudo install -o 10001 -g 10001 -m 0640 \
  /opt/1cv8/8.3.X.Y/shquery_ru.hbk \
  /srv/mcp1c/data/bootstrap/
```

Пути и наличие `.hbk` зависят от установленной поставки платформы. В
репозитории этих файлов нет.

Большой архив команды Конфигуратора «Выгрузить конфигурацию в файлы…» кладите
в `incoming/`, не в `bootstrap/` и не в форму загрузки дашборда. Он разбирается
по явной административной команде из дашборда и не блокирует каждый старт
контейнера.

Запустить пустой сервер допустимо: health будет зелёным, но инструменты честно
сообщат, что конфигурации и справка не загружены. Исходники можно добавить
позже через дашборд или CLI.

## 5. Проверить Compose до запуска

Без дашборда:

```bash
docker compose -f docker-compose.yml config --quiet
```

Классический дашборд:

```bash
docker compose \
  -f docker-compose.yml \
  -f docker-compose.classic.yml \
  config --quiet
```

SPA:

```bash
docker compose \
  -f docker-compose.yml \
  -f docker-compose.dashboard.yml \
  config --quiet
```

Отсутствие вывода и код `0` означают, что Compose-файлы и переменные
согласованы. Эта команда не проверяет права bind mount — их докажет стартовый
write probe.

## 6. Запустить один из трёх вариантов

### Только MCP, без дашборда

```bash
docker compose \
  -f docker-compose.yml \
  up -d --build --force-recreate
```

Доступны `/mcp`, `/health` и служебный API. `/`, `/sources`, `/queries` и
SPA-маршруты не регистрируются.

### MCP и классический дашборд

```bash
docker compose \
  -f docker-compose.yml \
  -f docker-compose.classic.yml \
  up -d --build --force-recreate
```

Это стабильный HTML-интерфейс без Node в runtime.

### MCP и SPA

```bash
docker compose \
  -f docker-compose.yml \
  -f docker-compose.dashboard.yml \
  up -d --build --force-recreate
```

Node используется только в build-stage. В рабочий образ переходят Python и
готовые статические файлы. SPA является основным современным интерфейсом;
классический HTML сохранён как совместимый fallback. MCP и данные при
переключении не меняются.

Все команды управляют одним service `mcp1c`. Чтобы переключить режим, выполните
другую команду `up --force-recreate`; одновременно запускать три контейнера с
одним портом и одним `data/` не нужно.

`restart` не применяет новый Dockerfile или новый frontend. После изменения
кода используйте `up -d --build --force-recreate`.

## 7. Проверить работающий контейнер

```bash
docker compose ps
docker compose logs --tail 100 mcp1c
curl --fail --show-error http://127.0.0.1:5001/health
```

Минимальный ответ:

```json
{
  "status": "ok",
  "configurations_total": 0,
  "syntax_loaded": false,
  "query_language_loaded": false
}
```

Счётчики зависят от ваших источников. Без токена `/health` не раскрывает их
имена; с токеном чтения возвращает подробный состав.

Проверка пользователя и write-контракта без вывода предметных данных:

```bash
docker compose exec mcp1c sh -c '
  id
  test "$(id -u)" = 10001
  test "$(id -g)" = 10001
  test -w /data
  test -w /data/sources
  test -w /data/index
'
```

Ожидается `uid=10001(mcp1c) gid=10001(mcp1c)` и код завершения `0`.

Для классического дашборда:

```bash
curl --fail --show-error http://127.0.0.1:5001/
```

Для SPA тот же адрес должен вернуть HTML, а asset — существовать в образе:

```bash
docker compose exec mcp1c test -f /app/dashboard/dist/index.html
curl --fail --show-error http://127.0.0.1:5001/
```

В режиме без UI запрос `/` должен вернуть `404`; это ожидаемая проверка, а не
ошибка запуска.

## 8. Удалённый сервер через HTTPS

Backend намеренно остаётся на `127.0.0.1`. Наружу его публикует TLS reverse
proxy на той же машине. Remote override:

- требует непустые и разные по назначению `API_TOKEN` и `ADMIN_TOKEN` ещё при
  разборе Compose;
- сохраняет loopback bind;
- включает доверие к `X-Forwarded-*` только для этого контура.

Без дашборда:

```bash
docker compose \
  -f docker-compose.yml \
  -f docker-compose.remote.yml \
  up -d --build --force-recreate
```

Классический дашборд:

```bash
docker compose \
  -f docker-compose.yml \
  -f docker-compose.classic.yml \
  -f docker-compose.remote.yml \
  up -d --build --force-recreate
```

SPA:

```bash
docker compose \
  -f docker-compose.yml \
  -f docker-compose.dashboard.yml \
  -f docker-compose.remote.yml \
  up -d --build --force-recreate
```

Последовательность файлов важна: базовый, выбранный UI, затем remote.

Минимальный Caddyfile после настройки DNS и открытия портов 80/443:

```caddyfile
mcp.example.com {
    reverse_proxy 127.0.0.1:5001
}
```

Клиент подключается к `https://mcp.example.com/mcp`. Порт `5001` в firewall
наружу не открывается. Не добавляйте `0.0.0.0:5001:8000` и не включайте
`--trust-proxy-headers` при прямом доступе клиента к backend: иначе клиент
сможет подделать схему запроса.

Проверка с сервера:

```bash
curl --fail --show-error http://127.0.0.1:5001/health
curl --fail --show-error https://mcp.example.com/health
```

Затем выполните инициализационный запрос из
[инструкции клиентов](docs/clients.md#токен-чтения).

Если reverse proxy сам ограничивает размер тела, его предел для двух upload
маршрутов должен быть не меньше 501 МиБ. Иначе большой `.hbk` получит `413` до
того, как запрос дойдёт до MCP-сервера. Остальные серверные маршруты сохраняют
меньшие собственные лимиты.

## 9. Переключение режима, обновление и остановка

Посмотреть итоговую конфигурацию выбранного режима:

```bash
docker compose \
  -f docker-compose.yml \
  -f docker-compose.dashboard.yml \
  config
```

Не публикуйте этот вывод: он содержит значения переменных окружения.

Обновление кода:

```bash
git status --short --branch
git pull --ff-only
docker compose \
  -f docker-compose.yml \
  -f docker-compose.classic.yml \
  up -d --build --force-recreate
docker compose ps
docker compose logs --tail 100 mcp1c
```

Остановка без удаления данных:

```bash
docker compose stop mcp1c
```

Удаление контейнера и сети также не удаляет bind-mounted `data/`:

```bash
docker compose down
```

Команды не используют `down -v`: у проекта нет named volume, но привычка
удалять volumes опасна при дальнейшем расширении Compose.

## 10. Резервная копия и перенос

Для согласованной файловой копии остановите writer, сохраните числовых
владельцев и запустите тот же режим снова:

```bash
docker compose stop mcp1c
sudo tar --numeric-owner -C /srv/mcp1c \
  -czf /путь/к/backup/mcp1c-data.tar.gz data
docker compose start mcp1c
```

Восстановление выполняется в новый пустой каталог при остановленном
контейнере. После распаковки проверьте `10001:10001`, затем запустите Compose и
сверьте `/health` и логи. `data/` переносится целиком: выборочное копирование
только `registry.json` без его источников и индексов создаёт несогласованную
установку.

## 11. Диагностика запуска

### `bind source path does not exist`

`MCP1C_DATA_DIR` указывает на отсутствующий каталог. Создайте его явно по шагу
3 и проверьте, что Compose читает ожидаемый `.env`.

### `Каталог данных ... недоступен для записи`

Стартовый probe не смог создать и удалить файл. Сообщение содержит точный путь,
`uid` и `gid`. Для обычного Linux:

```bash
docker compose stop mcp1c
sudo chown -R 10001:10001 /srv/mcp1c/data
sudo find /srv/mcp1c/data -type d -exec chmod 0750 {} +
sudo find /srv/mcp1c/data -type f -exec chmod 0640 {} +
```

После этого повторите выбранную команду `up`. На rootless/userns сначала
проверьте отображение UID, описанное выше.

### Контейнер постоянно перезапускается

```bash
docker compose ps
docker compose logs --tail 200 mcp1c
docker inspect mcp1c --format '{{.RestartCount}} {{.State.OOMKilled}}'
```

Права дают явную ошибку до загрузки Registry. `OOMKilled=true` — отдельная
проблема памяти и не исправляется правами.

### `/health` не отвечает

Проверьте `docker compose ps`, логи, занятость `MCP1C_PORT` и firewall.
Healthcheck внутри образа обращается к `127.0.0.1:8000`; внешний порт на него
не влияет.

### `401` на `/mcp`

Задан `API_TOKEN`, но клиент не передал его или передал другое значение.
Проверьте запрос из [docs/clients.md](docs/clients.md#токен-чтения).

### `404` на загрузке или reload

Пустой `ADMIN_TOKEN` намеренно отключает изменяющие маршруты. Заполните `.env`
и пересоздайте контейнер; простая правка файла окружения не меняет уже
запущенный процесс.

### `413 Request Entity Too Large`

Сервер принимает файл до 500 МиБ и multipart-тело до 501 МиБ только на
`/sources` и `/api/v1/sources/upload`. Если файл меньше, ищите меньший предел
в reverse proxy. Повышать общий лимит `/mcp` и прочих API не нужно.

### Источник лежит в `bootstrap`, но не появился

ZIP кода без schema-v1 manifest относится к `incoming/`. Неполная выгрузка
`truncated=true` в bootstrap отклоняется без явного административного
разрешения. Точную причину смотрите в стартовых сообщениях контейнера.

---

# Запуск без Docker

```bash
python3 -m venv .venv
.venv/bin/pip install --require-hashes -r requirements-lock.txt
PYTHONPATH=src .venv/bin/python -m mcp1c.server \
  --host 127.0.0.1 \
  --port 8000 \
  --data data
```

Для локального MCP-клиента можно использовать `stdio`:

```bash
PYTHONPATH=src .venv/bin/python -m mcp1c.server \
  --transport stdio \
  --data /абсолютный/путь/к/data
```

Ручной запуск не включает `--require-writable-data` автоматически. Это
позволяет осознанно читать готовый Registry с read-only носителя. Если нужны
загрузка, словарь и административные операции, добавьте флаг и устраните все
ошибки проверки до старта.

---

# Подключение агента

HTTP URL по умолчанию:

```text
http://127.0.0.1:5001/mcp
```

Удалённый URL:

```text
https://mcp.example.com/mcp
```

Клиент выполняет `initialize`, получает схемы через `tools/list`, а предметные
данные запрашивает через `tools/call`. Описания инструментов находятся в
контексте всю сессию, данные — только по запросу.

| Клиент | Формат | Полный копируемый конфиг |
|---|---|---|
| Claude Code | `.mcp.json`, `type: http` | [docs/clients.md#claude-code](docs/clients.md#claude-code) |
| Codex CLI | `config.toml` | [docs/clients.md#codex-cli](docs/clients.md#codex-cli) |
| Cursor | `.cursor/mcp.json`, `streamable-http` | [docs/clients.md#cursor](docs/clients.md#cursor) |
| VS Code Copilot | `.vscode/mcp.json`, ключ `servers` | [docs/clients.md#vs-code-с-copilot](docs/clients.md#vs-code-с-copilot) |
| Qwen Code | `httpUrl` | [docs/clients.md#qwen-code](docs/clients.md#qwen-code) |
| Локальный процесс | `stdio` | [docs/clients.md#локальный-stdio](docs/clients.md#локальный-stdio) |

SSE не поддерживается. Если клиент использует `/sse` или трактует `url` как
SSE, он не подключится.

---

# Инструменты MCP

| Инструмент | Назначение |
|---|---|
| `list_configurations` | выбрать конфигурацию и увидеть доступные источники |
| `list_extensions` | фактическая активность расширений из отдельного снимка сеанса |
| `search_objects` | человеческая формулировка → точное имя объекта |
| `search_procedures` | имя или назначение → точный адрес процедуры |
| `get_procedure` | оглавление модуля или ограниченное тело процедуры |
| `get_callers` | места вызовов, подписки, задания и события форм |
| `get_object` | поля, их доказанное происхождение, таблицы запроса, связи и кодовые сведения объекта |
| `get_related` | непосредственные входящие и исходящие связи |
| `compare_configurations` | различия одного объекта между конфигурациями |
| `search_syntax` | поиск по платформе и языку запросов |
| `get_syntax` | сигнатура, доступность, версия, пример и замена |

Обязательный порядок для метаданных:

```text
list_configurations → search_objects → get_object → get_related
                                           ↓
                              search_syntax → get_syntax
```

Для кода:

```text
list_configurations → search_procedures → get_procedure → get_callers
```

Если вывод зависит от того, какие расширения действительно применены в сеансе:

```text
list_configurations → list_extensions → search_procedures(extension=...)
```

Без отдельного снимка `list_extensions` возвращает `unknown`. Позиция в нём —
порядок элементов, возвращённый API платформы, а не обещание порядка исполнения
модулей.

Пометка `get_object` «объявлен расширением» означает только статическую
файловую выгрузку. Она не доказывает, что расширение активно в текущем сеансе:
для этого отдельно вызывается `list_extensions`. При одинаковом добавлении
двух расширений карточка перечисляет оба и не выбирает победителя. Табличные
части и ссылочные поля пока не помечаются — для них нет доказанного корпуса.

Пропуск карточки оставляет только правдоподобное имя без фактических полей;
пропуск `get_callers` скрывает последствия изменения. Полные параметры,
уровни `detail`, версия платформы, независимость источников и границы
провайдера описаны в [docs/tools.md](docs/tools.md).

---

# Источники данных

| Источник | Откуда берётся | Что будет без него |
|---|---|---|
| Структура конфигурации | обработка из `exporter-1c/` | нет объектов, реквизитов и графа |
| Код конфигурации | выгрузка конфигурации в файлы | нет процедур основной реализации и базы для доказательства происхождения |
| Код расширения | отдельная файловая выгрузка расширения | нет изменений и доказанного происхождения объектов/полей этого расширения |
| Активность расширений | `СнимокРасширений_*.json` из отдельной обработки | активность и порядок ответа платформы остаются `unknown` |
| Справка платформы | `shcntx_ru.hbk` установленной 1С | нет методов, свойств и событий платформы |
| Язык запросов | `shquery_ru.hbk` установленной 1С | нет таблиц, функций и статей языка запросов |
| Локальный словарь | `data/dictionary.json` | нет терминологии конкретной установки |

Источники независимы и учитываются отдельно. Сведения не переносятся из одного
источника догадкой, если другой отсутствует. Подробная граница —
[docs/data-sources.md](docs/data-sources.md), формат структуры —
[docs/schema-v1.md](docs/schema-v1.md).

Обработка и совместимость с платформой 8.3.5 описаны в
[exporter-1c/README.md](exporter-1c/README.md).

---

# Управление и CLI

`python -m mcp1c.cli` содержит все команды:

```text
info  stats  show  related  find
reg-add  reg-list  reg-search
dict-show  dict-synonyms  dict-alias
reg-search-procedures  reg-get-procedure  reg-get-callers
```

`python -m mcp1c.server` поддерживает `--data`, `--transport`, `--host`,
`--port`, `--trust-proxy-headers`, `--require-writable-data`.

`python -m mcp1c.bench` поддерживает `--data`, `--sets`, `--auto`, `--config`,
`--extension`, `--limit`, `--save`, `--baseline`, `--check-notes` и домены
`syntax`, `metadata`, `procedures`.

Полные таблицы параметров, примеры загрузки, incoming, словаря, reload и
стенда находятся в [docs/operations.md](docs/operations.md).

---

# Безопасность

| Контроль | Поведение |
|---|---|
| `API_TOKEN` | закрывает MCP, страницы и API чтения |
| `ADMIN_TOKEN` | включает маршруты изменения; без него они отвечают 404 |
| Loopback bind | Compose не публикует backend во внешнюю сеть |
| HTTPS profile | требует оба токена и доверяет proxy-заголовкам явно |
| Non-root | процесс контейнера — `10001:10001` |
| Filesystem | bind существует заранее, write probe выполняется до Registry |
| Process | `no-new-privileges`, `cap_drop: ALL`, ограниченная ротация логов |
| HTTP body | отдельные пределы login, queries, uploads, MCP и прочих API |
| Архивы | лимиты распаковки, защита путей и атомарная публикация |
| Данные | весь `data/` и проприетарные форматы исключены из git |

Токен можно передать как `X-Api-Token` или `Authorization: Bearer`. Для
административных curl-запросов используется `X-Admin-Token`. Токены должны быть
длинными, случайными и ASCII.

`/health` открыт для healthcheck, но без права чтения отдаёт только безопасные
счётчики. `/login` и статика формы доступны до входа; предметный API остаётся
закрытым. Cookie браузера — `HttpOnly`, `SameSite=Strict`, а за доверенным
HTTPS proxy также `Secure`.

Инструкции по сообщению об уязвимости — [SECURITY.md](SECURITY.md).

---

# Разработка и документы

Python-проверка:

```bash
.venv/bin/python -m pytest
```

SPA-проверка:

```bash
cd dashboard
npm ci
npm test
npm run typecheck
npm run build
```

Тесты используют только синтетические обезличенные фикстуры и не зависят от
локального `data/`. Качество поиска измеряется `mcp1c.bench`, а не процентным
assert.

| Документ | Содержание |
|---|---|
| [CHANGELOG.md](CHANGELOG.md) | изменения и найденные факты о 1С |
| [CONTRIBUTING.md](CONTRIBUTING.md) | публичные правила вклада |
| [docs/architecture.md](docs/architecture.md) | модули, кэш, зависимости и проверки |
| [docs/schema-v1.md](docs/schema-v1.md) | контракт формата выгрузки |
| [docs/data-sources.md](docs/data-sources.md) | происхождение сведений |
| [docs/query-language-design.md](docs/query-language-design.md) | источник языка запросов |
| [docs/dashboard-design.md](docs/dashboard-design.md) | контракт дашборда |
| [docs/modules-intake-design.md](docs/modules-intake-design.md) | безопасный приём кода |
| [docs/modules-provider-design.md](docs/modules-provider-design.md) | индексы и инструменты кода |
| [docs/standard-procedure-intents.md](docs/standard-procedure-intents.md) | распознаваемые типовые события |

Внешняя БД, векторы, графовая БД и ленивая загрузка не добавляются без нового
измеренного сценария. Текущий объём обслуживается одним процессом и файловым
каталогом данных.

---

# Лицензия

[Apache License 2.0](LICENSE). Проект разработан независимо и не аффилирован с
ООО «1С». «1С» и «1С:Предприятие» являются товарными знаками ООО «1С»;
подробности — [NOTICE](NOTICE).

Лицензия распространяется на код проекта, а не на справку платформы и данные
конкретных внедрений, которые пользователь загружает в `data/`.
