# GitHub MCP Server

Самописный MCP-сервер на Python вокруг публичного GitHub REST API.

Сервер не использует MCP SDK, GitHub SDK или внешние зависимости. MCP-часть реализована вручную через JSON-RPC сообщения по `stdio`.

## Инструмент

Сервер регистрирует один инструмент:

```text
github_repo_info
```

Он получает краткую информацию о публичном GitHub-репозитории.

## Входные Параметры

```json
{
  "owner": "nikbrik",
  "repo": "coding_writer"
}
```

Схема параметров отдается агенту через `tools/list`.

## GitHub API

Инструмент вызывает:

```text
GET https://api.github.com/repos/{owner}/{repo}
```

Заголовки:

```text
Accept: application/vnd.github+json
User-Agent: mcp-server-github-demo
```

Авторизация не используется. Сервер работает только с публичным GitHub API.

## Возврат Результата

Успешный MCP tool result:

```json
{
  "content": [
    {
      "type": "text",
      "text": "{...summary json...}"
    }
  ],
  "isError": false
}
```

Внутри `text` лежит JSON:

```json
{
  "api_url": "https://api.github.com/repos/nikbrik/coding_writer",
  "full_name": "nikbrik/coding_writer",
  "description": "...",
  "html_url": "https://github.com/nikbrik/coding_writer",
  "default_branch": "main",
  "language": "...",
  "stars": 0,
  "forks": 0,
  "open_issues": 0,
  "visibility": "public",
  "updated_at": "..."
}
```

Ошибки GitHub API возвращаются как tool error:

```json
{
  "content": [
    {
      "type": "text",
      "text": "{\"api_url\":\"...\",\"status\":404,\"error\":\"Not Found\"}"
    }
  ],
  "isError": true
}
```

## Подключение К Агенту

Пример MCP-конфига:

```json
{
  "mcpServers": {
    "github-api": {
      "command": "python3",
      "args": ["/Users/nikita/Documents/mcp-server/server.py"]
    }
  }
}
```

После подключения агент делает:

1. `tools/list` - получает инструмент `github_repo_info`;
2. `tools/call` - вызывает `github_repo_info` с аргументами `owner` и `repo`;
3. получает summary JSON и использует поля `full_name`, `stars`, `language`, `updated_at`.

## Проверка

Синтаксис:

```bash
python3 -m py_compile server.py scripts/smoke_github_repo_info.py
```

Успешный smoke:

```bash
python3 scripts/smoke_github_repo_info.py nikbrik coding_writer
```

Error smoke:

```bash
python3 scripts/smoke_github_repo_info.py nikbrik definitely-missing-repo
```

Во втором случае сервер не падает. Инструмент возвращает `isError=true`.
