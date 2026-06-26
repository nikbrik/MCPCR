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

## День 18: Scheduled GitHub Monitor

Day 18 добавляет фонового scheduled worker-а и read-only MCP tools поверх его persisted aggregate.

Архитектура demo:

```text
Terminal 1: server.py worker
  -> каждые N секунд читает GitHub public API
  -> пишет .data/day18/runs.jsonl, state.json, latest_summary.json
  -> печатает tick logs в stderr

Terminal 2: coding_writer LLM agent
  -> периодически вызывает github_watch_summary через MCP
  -> получает уже накопленный aggregate, не делает fresh fetch
  -> передаёт aggregate в LLM и печатает человеческую сводку
```

### Worker

Быстрый live demo:

```bash
cd /Users/nikita/Documents/mcp-server
python3 server.py worker \
  --storage-dir .data/day18 \
  --repo nikbrik/coding_writer \
  --interval 5s \
  --demo
```

Ожидаемые stderr logs:

```text
[worker] start repo=nikbrik/coding_writer interval=5s storage=... max_runs=forever
[worker] tick=1 repo=nikbrik/coding_writer status=ok samples=1 stars=... forks=... open_issues=... next_run=...
[worker] summary updated path=.../.data/day18/latest_summary.json
```

Bounded smoke без бесконечного процесса:

```bash
python3 server.py worker --storage-dir .data/day18 --repo nikbrik/coding_writer --interval 1s --max-runs 2 --demo
```

### LLM agent loop

Во втором терминале запускается именно agent loop с LLM под капотом. Дефолтный интервал редкий (`2m`), чтобы медленная модель успевала ответить без наложения запросов.

```bash
cd /Users/nikita/code/coding_writer
cw mcp watch-agent day18-github-watch github_watch_summary
```

Для короткой проверки можно ограничить число циклов:

```bash
cw mcp watch-agent day18-github-watch github_watch_summary --interval 2m --max-runs 1
```

### MCP read tools

MCP mode остаётся stdio JSON-RPC. В stdout идут только protocol messages.
Human logs есть только у worker mode и идут в stderr.

Новые tools:

```text
github_watch_status
github_watch_summary
github_watch_history
```

`github_watch_summary` возвращает JSON aggregate из persisted `runs/state/jobs`, например:

```json
{
  "repo": "nikbrik/coding_writer",
  "total_runs": 2,
  "ok_runs": 2,
  "error_runs": 0,
  "health": "healthy",
  "latest": {"stars": 1, "forks": 0, "open_issues": 3},
  "delta": {"stars": 0, "forks": 0, "open_issues": 0},
  "summary_text": "nikbrik/coding_writer: samples=2/2, stars=1, forks=0, open_issues=3, delta_stars=0, worker=healthy"
}
```

### Day 18 smoke

```bash
python3 -m py_compile server.py scripts/smoke_github_repo_info.py scripts/smoke_day18_watch.py
python3 scripts/smoke_day18_watch.py nikbrik/coding_writer
```

The smoke runs the worker for two ticks, verifies persisted files, then starts MCP stdio mode and calls `github_watch_status`, `github_watch_summary`, and `github_watch_history`.

