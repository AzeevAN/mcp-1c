# Подключение MCP-клиентов

HTTP endpoint сервера — `http://127.0.0.1:5001/mcp` для локального запуска или
`https://mcp.example.com/mcp` за TLS reverse proxy. Транспорт —
Streamable HTTP. Устаревший SSE не поддерживается.

## Что происходит при подключении

Клиент отправляет `initialize`, затем получает список через `tools/list`.
Названия, описания и схемы параметров всех инструментов остаются в контексте
агента всю сессию. Предметные данные туда заранее не загружаются: они приходят
только после `tools/call`.

Поэтому увеличение числа инструментов имеет постоянную цену контекста, а
увеличение Registry — нет, пока агент не запросил конкретный объект.

## Токен чтения

Если на сервере задан `API_TOKEN`, клиент передаёт один из заголовков:

```http
X-Api-Token: ВАШ_API_TOKEN
```

```http
Authorization: Bearer ВАШ_API_TOKEN
```

Токен должен состоять из ASCII-символов. Генерация:

```bash
python3 -c "import secrets; print(secrets.token_urlsafe(32))"
```

В MCP-клиент кладётся `API_TOKEN`, не `ADMIN_TOKEN`. Административный токен
тоже принимается на чтение, но его утечка дополнительно даст право менять и
удалять источники.

Проверить endpoint до настройки клиента:

```bash
curl -i -X POST \
  -H 'X-Api-Token: ВАШ_API_TOKEN' \
  -H 'Content-Type: application/json' \
  -H 'Accept: application/json, text/event-stream' \
  -d '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-06-18","capabilities":{},"clientInfo":{"name":"probe","version":"1"}}}' \
  http://127.0.0.1:5001/mcp
```

`200` означает, что endpoint и токен доступны. `401` — токен отсутствует,
неверен или заголовок не дошёл.

## Claude Code

`.mcp.json` в проекте:

```json
{
  "mcpServers": {
    "1c": {
      "type": "http",
      "url": "http://127.0.0.1:5001/mcp",
      "headers": {
        "X-Api-Token": "${MCP1C_API_TOKEN}"
      }
    }
  }
}
```

Токен хранится вне репозитория:

```bash
export MCP1C_API_TOKEN='значение_API_TOKEN'
```

Командный вариант:

```bash
claude mcp add --transport http 1c http://127.0.0.1:5001/mcp \
  --header "X-Api-Token: $MCP1C_API_TOKEN"
```

## Codex CLI

`~/.codex/config.toml` или `.codex/config.toml`:

```toml
[mcp_servers.mcp1c]
url = "http://127.0.0.1:5001/mcp"

[mcp_servers.mcp1c.http_headers]
X-Api-Token = "значение_API_TOKEN"
```

Формат локального клиента может меняться между версиями Codex. Если текущая
версия не принимает `http_headers`, сверьте её справку MCP или используйте
локальный `stdio`, где сетевой токен не нужен.

## Cursor

`.cursor/mcp.json`:

```json
{
  "mcpServers": {
    "1c": {
      "type": "streamable-http",
      "url": "http://127.0.0.1:5001/mcp",
      "headers": {
        "X-Api-Token": "значение_API_TOKEN"
      }
    }
  }
}
```

## VS Code с Copilot

`.vscode/mcp.json`:

```json
{
  "servers": {
    "1c": {
      "type": "http",
      "url": "http://127.0.0.1:5001/mcp",
      "headers": {
        "X-Api-Token": "значение_API_TOKEN"
      }
    }
  }
}
```

## Qwen Code

```json
{
  "mcpServers": {
    "1c": {
      "httpUrl": "http://127.0.0.1:5001/mcp",
      "headers": {
        "X-Api-Token": "значение_API_TOKEN"
      }
    }
  }
}
```

Здесь `httpUrl` выбирает Streamable HTTP. Ключ `url` в этом формате может
означать старый SSE, который сервер намеренно не предоставляет.

## Другие клиенты

Для Windsurf, Cline, Roo Code и других клиентов нужны те же три значения:

- транспорт Streamable HTTP;
- URL с окончанием `/mcp`;
- заголовок `X-Api-Token`, если на сервере включена авторизация чтения.

Имена верхних ключей (`mcpServers`, `servers`) зависят от клиента.

## Локальный stdio

Клиент может самостоятельно запускать сервер без сети:

```json
{
  "mcpServers": {
    "1c": {
      "command": "python3",
      "args": [
        "-m",
        "mcp1c.server",
        "--transport",
        "stdio",
        "--data",
        "/абсолютный/путь/к/data"
      ],
      "env": {
        "PYTHONPATH": "/абсолютный/путь/к/mcp-1c/src"
      }
    }
  }
}
```

В `stdio` токен не нужен: процесс принадлежит одному клиенту, а сетевой
endpoint отсутствует.

## Диагностика подключения

1. `curl http://127.0.0.1:5001/health` — контейнер доступен.
2. Инициализационный `curl` выше — endpoint и токен работают.
3. URL оканчивается на `/mcp`, не на `/sse`.
4. Клиент использует Streamable HTTP, не SSE.
5. В конфиге агента находится токен чтения, а не пустая подстановка.
6. Для удалённого адреса используется HTTPS и корректный DNS.
7. После изменения конфигурации клиента процесс агента перезапущен.

Не сохраняйте реальные токены в отслеживаемых файлах. Используйте переменную
окружения или пользовательский конфиг за пределами репозитория.
