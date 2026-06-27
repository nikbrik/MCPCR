#!/usr/bin/env python3
"""Minimal MCP server around the public GitHub REST API plus Day 18 scheduler.

Transport: stdio.
Protocol style: JSON-RPC messages used by MCP clients.
Dependencies: Python standard library only.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import sys
import secrets
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any


SERVER_NAME = "github-api-mcp-server"
SERVER_VERSION = "0.2.0"
PROTOCOL_VERSION = "2024-11-05"
GITHUB_API_BASE_URL = "https://api.github.com"
GITHUB_USER_AGENT = "mcp-server-github-demo"
DEFAULT_STORAGE_DIR = Path(__file__).resolve().parent / ".data" / "day18"
DEFAULT_WATCH_REPO = "nikbrik/coding_writer"
DEFAULT_INTERVAL_SECONDS = 300
SEARCH_DEFAULT_LIMIT = 5
SEARCH_MAX_LIMIT = 10


GITHUB_REPO_INFO_TOOL: dict[str, Any] = {
    "name": "github_repo_info",
    "description": "Fetch summary information for a public GitHub repository.",
    "inputSchema": {
        "type": "object",
        "properties": {
            "owner": {
                "type": "string",
                "description": "GitHub repository owner, for example: nikbrik.",
                "minLength": 1,
            },
            "repo": {
                "type": "string",
                "description": "GitHub repository name, for example: coding_writer.",
                "minLength": 1,
            },
        },
        "required": ["owner", "repo"],
        "additionalProperties": False,
    },
}

GITHUB_WATCH_STATUS_TOOL: dict[str, Any] = {
    "name": "github_watch_status",
    "description": "Read the Day 18 scheduled GitHub monitor status from persisted storage.",
    "inputSchema": {
        "type": "object",
        "properties": {},
        "additionalProperties": False,
    },
}

GITHUB_WATCH_SUMMARY_TOOL: dict[str, Any] = {
    "name": "github_watch_summary",
    "description": "Read the latest aggregated Day 18 GitHub monitor summary from persisted storage.",
    "inputSchema": {
        "type": "object",
        "properties": {},
        "additionalProperties": False,
    },
}

GITHUB_WATCH_HISTORY_TOOL: dict[str, Any] = {
    "name": "github_watch_history",
    "description": "Read recent Day 18 GitHub monitor samples from persisted storage.",
    "inputSchema": {
        "type": "object",
        "properties": {
            "limit": {
                "type": "integer",
                "description": "Maximum number of recent samples to return.",
                "minimum": 1,
                "maximum": 100,
            }
        },
        "additionalProperties": False,
    },
}

GITHUB_SEARCH_REPOS_TOOL: dict[str, Any] = {
    "name": "github_search_repos",
    "description": "Search public GitHub repositories by query and save artifacts for the next pipeline step.",
    "inputSchema": {
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": "GitHub repository search query.",
            },
            "limit": {
                "type": "integer",
                "description": "Maximum number of repositories to store.",
                "minimum": 1,
                "maximum": SEARCH_MAX_LIMIT,
            },
        },
        "required": ["query"],
        "additionalProperties": False,
    },
}

GITHUB_MAKE_REPORT_TOOL: dict[str, Any] = {
    "name": "github_make_report",
    "description": "Build a short report from a previous day19 search artifact and persist it.",
    "inputSchema": {
        "type": "object",
        "properties": {
            "search_id": {
                "type": "string",
                "description": "search_id from github_search_repos",
            },
        },
        "required": ["search_id"],
        "additionalProperties": False,
    },
}

SAVE_REPORT_TOOL: dict[str, Any] = {
    "name": "save_report_to_file",
    "description": "Persist a report artifact to markdown file and return path.",
    "inputSchema": {
        "type": "object",
        "properties": {
            "report_id": {
                "type": "string",
                "description": "report_id from github_make_report",
            },
        },
        "required": ["report_id"],
        "additionalProperties": False,
    },
}

TOOLS = [
    GITHUB_REPO_INFO_TOOL,
    GITHUB_WATCH_STATUS_TOOL,
    GITHUB_WATCH_SUMMARY_TOOL,
    GITHUB_WATCH_HISTORY_TOOL,
    GITHUB_SEARCH_REPOS_TOOL,
    GITHUB_MAKE_REPORT_TOOL,
    SAVE_REPORT_TOOL,
]


class GitHubApiError(RuntimeError):
    def __init__(self, status: int | None, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.message = message


class StorageError(RuntimeError):
    pass


def utc_now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def iso_time(value: dt.datetime | None = None) -> str:
    if value is None:
        value = utc_now()
    return value.isoformat().replace("+00:00", "Z")


def parse_duration(raw: str) -> float:
    text = str(raw).strip().lower()
    if not text:
        raise argparse.ArgumentTypeError("duration must be non-empty")
    unit = text[-1]
    number = text[:-1] if unit in {"s", "m", "h"} else text
    try:
        value = float(number)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"invalid duration: {raw}") from exc
    if value <= 0:
        raise argparse.ArgumentTypeError("duration must be positive")
    if unit == "m":
        value *= 60
    elif unit == "h":
        value *= 3600
    elif unit != "s" and not unit.isdigit():
        raise argparse.ArgumentTypeError(f"invalid duration unit: {raw}")
    return value


def json_seconds(seconds: float | int) -> int | float:
    value = float(seconds)
    return int(value) if value.is_integer() else value


def format_duration(seconds: float | int | None) -> str:
    if seconds is None:
        return "unknown"
    value = max(0.0, float(seconds))
    if not value.is_integer():
        return f"{value:g}s"
    seconds = int(value)
    if seconds < 60:
        return f"{seconds}s"
    if seconds % 3600 == 0:
        return f"{seconds // 3600}h"
    if seconds % 60 == 0:
        return f"{seconds // 60}m"
    return f"{seconds}s"


def parse_repo_slug(raw: str) -> tuple[str, str, str]:
    text = str(raw).strip()
    parts = text.split("/")
    if len(parts) != 2:
        raise ValueError("repo must have owner/name format")
    owner = validate_repo_part(parts[0], "owner")
    repo = validate_repo_part(parts[1], "repo")
    return owner, repo, f"{owner}/{repo}"


def write_message(message: dict[str, Any]) -> None:
    sys.stdout.write(json.dumps(message, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def success(message_id: Any, result: dict[str, Any]) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": message_id, "result": result}


def error(message_id: Any, code: int, message: str) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": message_id, "error": {"code": code, "message": message}}


def tool_result(payload: dict[str, Any], is_error: bool = False) -> dict[str, Any]:
    return {
        "content": [
            {
                "type": "text",
                "text": json.dumps(payload, ensure_ascii=False, indent=2),
            }
        ],
        "isError": is_error,
    }


def validate_repo_part(value: Any, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field_name} must be a non-empty string")

    normalized = value.strip()
    if "/" in normalized:
        raise ValueError(f"{field_name} must not contain '/'")
    return normalized


def github_repo_url(owner: str, repo: str) -> str:
    encoded_owner = urllib.parse.quote(owner, safe="")
    encoded_repo = urllib.parse.quote(repo, safe="")
    return f"{GITHUB_API_BASE_URL}/repos/{encoded_owner}/{encoded_repo}"


def github_error_message(body: str, fallback: str) -> str:
    try:
        payload = json.loads(body)
    except json.JSONDecodeError:
        return fallback

    if isinstance(payload, dict) and isinstance(payload.get("message"), str):
        return payload["message"]
    return fallback


def fetch_github_json(url: str) -> dict[str, Any]:
    request = urllib.request.Request(
        url,
        headers={
            "Accept": "application/vnd.github+json",
            "User-Agent": GITHUB_USER_AGENT,
        },
    )

    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            body = response.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        raise GitHubApiError(exc.code, github_error_message(body, f"GitHub API returned HTTP {exc.code}")) from exc
    except urllib.error.URLError as exc:
        raise GitHubApiError(None, f"GitHub API request failed: {exc.reason}") from exc
    except TimeoutError as exc:
        raise GitHubApiError(None, "GitHub API request timed out") from exc

    payload = json.loads(body)
    if not isinstance(payload, dict):
        raise GitHubApiError(None, "GitHub API returned non-object JSON")
    return payload


def summarize_repo(api_url: str, payload: dict[str, Any]) -> dict[str, Any]:
    return {
        "api_url": api_url,
        "full_name": payload.get("full_name"),
        "description": payload.get("description"),
        "html_url": payload.get("html_url"),
        "default_branch": payload.get("default_branch"),
        "language": payload.get("language"),
        "stars": payload.get("stargazers_count"),
        "forks": payload.get("forks_count"),
        "open_issues": payload.get("open_issues_count"),
        "visibility": payload.get("visibility"),
        "updated_at": payload.get("updated_at"),
    }


def call_github_repo_info(arguments: dict[str, Any]) -> dict[str, Any]:
    try:
        owner = validate_repo_part(arguments.get("owner"), "owner")
        repo = validate_repo_part(arguments.get("repo"), "repo")
    except ValueError as exc:
        return tool_result({"error": str(exc)}, is_error=True)

    api_url = github_repo_url(owner, repo)
    try:
        payload = fetch_github_json(api_url)
    except GitHubApiError as exc:
        error_payload: dict[str, Any] = {"api_url": api_url, "error": exc.message}
        if exc.status is not None:
            error_payload["status"] = exc.status
        return tool_result(error_payload, is_error=True)

    return tool_result(summarize_repo(api_url, payload))


def ensure_storage_dir(storage_dir: Path) -> None:
    storage_dir.mkdir(parents=True, exist_ok=True)


def read_json(path: Path, default: Any) -> Any:
    if not path.exists():
        return default
    try:
        with path.open("r", encoding="utf-8") as handle:
            return json.load(handle)
    except json.JSONDecodeError as exc:
        raise StorageError(f"broken JSON: {path}: {exc}") from exc


def atomic_write_json(path: Path, payload: Any) -> None:
    ensure_storage_dir(path.parent)
    tmp = path.with_name(f".{path.name}.tmp")
    with tmp.open("w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(tmp, path)


def parse_iso_time(raw: Any) -> dt.datetime | None:
    if not isinstance(raw, str) or not raw.strip():
        return None
    text = raw.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = dt.datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=dt.timezone.utc)
    return parsed.astimezone(dt.timezone.utc)


def schedule_health(state: dict[str, Any], jobs: dict[str, Any]) -> dict[str, Any]:
    health = state.get("health", "unknown")
    next_run_at = state.get("next_run_at") or jobs.get("next_run_at")
    interval_seconds = jobs.get("interval_seconds") or DEFAULT_INTERVAL_SECONDS
    missed_schedule = False
    overdue_seconds = 0

    next_run = parse_iso_time(next_run_at)
    if next_run is not None:
        try:
            interval = max(1.0, float(interval_seconds))
        except (TypeError, ValueError):
            interval = float(DEFAULT_INTERVAL_SECONDS)
        stale_after = next_run + dt.timedelta(seconds=interval)
        if utc_now() > stale_after:
            missed_schedule = True
            overdue_seconds = max(0, int((utc_now() - next_run).total_seconds()))
            health = "stale"

    return {
        "health": health,
        "missed_schedule": missed_schedule,
        "overdue_seconds": overdue_seconds,
    }


def append_jsonl(path: Path, payload: dict[str, Any]) -> None:
    ensure_storage_dir(path.parent)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n")
        handle.flush()
        os.fsync(handle.fileno())


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    out: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8") as handle:
        for line_no, line in enumerate(handle, start=1):
            text = line.strip()
            if not text:
                continue
            try:
                item = json.loads(text)
            except json.JSONDecodeError as exc:
                raise StorageError(f"broken JSONL: {path}:{line_no}: {exc}") from exc
            if isinstance(item, dict):
                out.append(item)
    return out


def storage_paths(storage_dir: Path) -> dict[str, Path]:
    return {
        "jobs": storage_dir / "jobs.json",
        "runs": storage_dir / "runs.jsonl",
        "state": storage_dir / "state.json",
        "summary": storage_dir / "latest_summary.json",
    }


def day19_storage_paths(storage_dir: Path) -> dict[str, Path]:
    return {
        "searches": storage_dir / "searches",
        "reports": storage_dir / "reports",
        "output": storage_dir / "output",
        "pipeline_runs": storage_dir / "pipeline_runs.jsonl",
    }


def load_runs(storage_dir: Path) -> list[dict[str, Any]]:
    return read_jsonl(storage_paths(storage_dir)["runs"])


def new_pipeline_id(prefix: str) -> str:
    return f"{prefix}_{int(time.time() * 1000)}_{secrets.token_hex(4)}"


def parse_search_limit(raw: Any) -> int:
    if raw is None:
        return SEARCH_DEFAULT_LIMIT
    if isinstance(raw, bool) or not isinstance(raw, int):
        raise ValueError("limit must be an integer")
    if raw < 1 or raw > SEARCH_MAX_LIMIT:
        raise ValueError("limit must be between 1 and 10")
    return raw


def parse_non_empty_string(raw: Any, field_name: str) -> str:
    if not isinstance(raw, str):
        raise ValueError(f"{field_name} must be a string")
    value = raw.strip()
    if not value:
        raise ValueError(f"{field_name} must not be empty")
    return value


def summarize_search_repo(payload: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(payload, dict):
        return {}
    return {
        "full_name": payload.get("full_name"),
        "description": payload.get("description"),
        "html_url": payload.get("html_url"),
        "language": payload.get("language"),
        "stars": payload.get("stargazers_count"),
        "forks": payload.get("forks_count"),
        "open_issues": payload.get("open_issues_count"),
        "updated_at": payload.get("updated_at"),
    }


def write_day19_run(storage_dir: Path, event: dict[str, Any]) -> None:
    append_jsonl(day19_storage_paths(storage_dir)["pipeline_runs"], {
        "id": new_pipeline_id("run"),
        "created_at": iso_time(),
        **event,
    })


def call_github_search_repos(storage_dir: Path, arguments: dict[str, Any]) -> dict[str, Any]:
    try:
        query = parse_non_empty_string(arguments.get("query"), "query")
        limit = parse_search_limit(arguments.get("limit"))
    except ValueError as exc:
        return tool_result({"error": str(exc)}, is_error=True)
    encoded_query = urllib.parse.quote(query, safe="")
    url = f"{GITHUB_API_BASE_URL}/search/repositories?q={encoded_query}&sort=stars&order=desc&per_page={limit}"
    try:
        payload = fetch_github_json(url)
    except GitHubApiError as exc:
        return tool_result({"error": exc.message, "query": query, "status": exc.status, "api_url": url}, is_error=True)
    items = payload.get("items")
    if not isinstance(items, list):
        return tool_result({"error": "GitHub search API returned unexpected payload", "query": query, "api_url": url}, is_error=True)
    repos = [summarize_search_repo(item) for item in items[:limit]]
    search_id = new_pipeline_id("search")
    search_payload = {
        "search_id": search_id,
        "created_at": iso_time(),
        "api_url": url,
        "query": query,
        "limit": limit,
        "total_count": payload.get("total_count"),
        "repositories": repos,
    }
    paths = day19_storage_paths(storage_dir)
    atomic_write_json(paths["searches"] / f"{search_id}.json", search_payload)
    write_day19_run(storage_dir, {
        "tool": GITHUB_SEARCH_REPOS_TOOL["name"],
        "search_id": search_id,
        "query": query,
        "repo_count": len(repos),
    })
    return tool_result({
        "search_id": search_id,
        "query": query,
        "limit": limit,
        "returned": len(repos),
        "total_count": payload.get("total_count", 0),
    })


def call_github_make_report(storage_dir: Path, arguments: dict[str, Any]) -> dict[str, Any]:
    try:
        search_id = parse_non_empty_string(arguments.get("search_id"), "search_id")
    except ValueError as exc:
        return tool_result({"error": str(exc)}, is_error=True)
    search_path = day19_storage_paths(storage_dir)["searches"] / f"{search_id}.json"
    if not search_path.exists():
        return tool_result({"error": f"search_id {search_id} not found"}, is_error=True)
    try:
        search_payload = read_json(search_path, {})
    except StorageError as exc:
        return tool_result({"error": str(exc)}, is_error=True)
    repos = search_payload.get("repositories")
    if not isinstance(repos, list):
        return tool_result({"error": f"invalid search artifact: {search_id}"}, is_error=True)
    report_id = new_pipeline_id("report")
    report = {
        "report_id": report_id,
        "search_id": search_id,
        "created_at": iso_time(),
        "query": search_payload.get("query"),
        "repo_count": len(repos),
        "top_repo": repos[0] if repos else None,
    }
    report_text = [
        "# GitHub Report",
        "",
        f"search_id: {search_id}",
        f"query: {search_payload.get('query', '')}",
        f"total_count: {search_payload.get('total_count', len(repos))}",
        f"returned: {len(repos)}",
        "",
        "## Repositories",
        "",
        "| full_name | stars | language | description |",
        "| --- | --- | --- | --- |",
    ]
    for item in repos:
        if not isinstance(item, dict):
            continue
        description = (item.get("description") or "")
        if description:
            description = description.replace("|", "•")
        report_text.append(
            "| {full_name} | {stars} | {language} | {description} |".format(
                full_name=item.get("full_name", ""),
                stars=item.get("stars", 0),
                language=item.get("language", "") or "",
                description=description,
            )
        )
    report_payload = {"report_text": "\n".join(report_text), "report": report}
    atomic_write_json(day19_storage_paths(storage_dir)["reports"] / f"{report_id}.json", report_payload)
    write_day19_run(storage_dir, {
        "tool": GITHUB_MAKE_REPORT_TOOL["name"],
        "search_id": search_id,
        "report_id": report_id,
        "repo_count": len(repos),
    })
    return tool_result({
        "report_id": report_id,
        "search_id": search_id,
        "query": search_payload.get("query"),
        "repo_count": len(repos),
    })


def call_save_report_to_file(storage_dir: Path, arguments: dict[str, Any]) -> dict[str, Any]:
    try:
        report_id = parse_non_empty_string(arguments.get("report_id"), "report_id")
    except ValueError as exc:
        return tool_result({"error": str(exc)}, is_error=True)
    report_payload_path = day19_storage_paths(storage_dir)["reports"] / f"{report_id}.json"
    if not report_payload_path.exists():
        return tool_result({"error": f"report_id {report_id} not found"}, is_error=True)
    report_payload = read_json(report_payload_path, {})
    if not report_payload:
        return tool_result({"error": f"report_id {report_id} has empty payload"}, is_error=True)
    output_path = day19_storage_paths(storage_dir)["output"] / f"{report_id}.md"
    ensure_storage_dir(output_path.parent)
    text = report_payload.get("report_text")
    if not isinstance(text, str) or not text.strip():
        return tool_result({"error": f"report_id {report_id} is missing markdown body"}, is_error=True)
    output_path.write_text(text, encoding="utf-8")
    write_day19_run(storage_dir, {
        "tool": SAVE_REPORT_TOOL["name"],
        "report_id": report_id,
        "path": str(output_path),
    })
    return tool_result({
        "path": str(output_path),
        "report_id": report_id,
    })


def sample_delta(first: dict[str, Any] | None, latest: dict[str, Any] | None) -> dict[str, Any]:
    delta: dict[str, Any] = {}
    if not first or not latest:
        return delta
    for key in ("stars", "forks", "open_issues"):
        start = first.get(key)
        end = latest.get(key)
        if isinstance(start, int) and isinstance(end, int):
            delta[key] = end - start
    return delta


def build_aggregate(storage_dir: Path) -> dict[str, Any]:
    paths = storage_paths(storage_dir)
    runs = load_runs(storage_dir)
    jobs = read_json(paths["jobs"], {})
    state = read_json(paths["state"], {})
    successful = [item for item in runs if item.get("status") == "ok"]
    failures = [item for item in runs if item.get("status") != "ok"]
    first = successful[0].get("repo") if successful else None
    latest_run = runs[-1] if runs else None
    latest = successful[-1].get("repo") if successful else None
    latest_error = failures[-1] if failures else None
    repo = jobs.get("repo") or (latest or {}).get("full_name") or (latest_run or {}).get("repo_slug") or DEFAULT_WATCH_REPO
    delta = sample_delta(first, latest)
    total_runs = len(runs)
    ok_runs = len(successful)
    error_runs = len(failures)
    interval_seconds = jobs.get("interval_seconds")
    health = schedule_health(state, jobs)
    summary_text = (
        f"{repo}: samples={ok_runs}/{total_runs}, "
        f"stars={(latest or {}).get('stars', '-')}, "
        f"forks={(latest or {}).get('forks', '-')}, "
        f"open_issues={(latest or {}).get('open_issues', '-')}, "
        f"delta_stars={delta.get('stars', 0)}, "
        f"worker={health['health']}"
    )
    return {
        "generated_at": iso_time(),
        "storage_dir": str(storage_dir),
        "repo": repo,
        "interval_seconds": interval_seconds,
        "interval": format_duration(interval_seconds) if interval_seconds else "unknown",
        "total_runs": total_runs,
        "ok_runs": ok_runs,
        "error_runs": error_runs,
        "latest_run_at": state.get("last_run_at") or (latest_run or {}).get("created_at"),
        "next_run_at": state.get("next_run_at") or jobs.get("next_run_at"),
        "health": health["health"],
        "missed_schedule": health["missed_schedule"],
        "overdue_seconds": health["overdue_seconds"],
        "latest": latest,
        "delta": delta,
        "latest_error": latest_error,
        "summary_text": summary_text,
    }


def persist_worker_config(storage_dir: Path, repo_slug: str, interval_seconds: float, next_run_at: str | None) -> None:
    paths = storage_paths(storage_dir)
    jobs = {
        "repo": repo_slug,
        "interval_seconds": json_seconds(interval_seconds),
        "interval": format_duration(interval_seconds),
        "next_run_at": next_run_at,
        "updated_at": iso_time(),
    }
    if not paths["jobs"].exists():
        jobs["created_at"] = jobs["updated_at"]
    else:
        existing = read_json(paths["jobs"], {})
        jobs["created_at"] = existing.get("created_at", jobs["updated_at"])
    atomic_write_json(paths["jobs"], jobs)


def run_github_watch_tick(storage_dir: Path, repo_slug: str, interval_seconds: float, tick: int) -> dict[str, Any]:
    owner, repo, normalized = parse_repo_slug(repo_slug)
    paths = storage_paths(storage_dir)
    now = utc_now()
    next_run = now + dt.timedelta(seconds=interval_seconds)
    persist_worker_config(storage_dir, normalized, interval_seconds, iso_time(next_run))
    api_url = github_repo_url(owner, repo)
    record: dict[str, Any] = {
        "id": f"run-{int(now.timestamp())}-{tick}",
        "tick": tick,
        "created_at": iso_time(now),
        "repo_slug": normalized,
        "api_url": api_url,
    }
    try:
        payload = fetch_github_json(api_url)
    except GitHubApiError as exc:
        record.update({"status": "error", "error": exc.message})
        if exc.status is not None:
            record["status_code"] = exc.status
    else:
        record.update({"status": "ok", "repo": summarize_repo(api_url, payload)})
    append_jsonl(paths["runs"], record)
    aggregate = build_aggregate(storage_dir)
    health = "healthy" if record["status"] == "ok" else "degraded"
    state = {
        "repo": normalized,
        "health": health,
        "last_run_at": record["created_at"],
        "next_run_at": iso_time(next_run),
        "last_status": record["status"],
        "total_runs": aggregate["total_runs"],
        "ok_runs": aggregate["ok_runs"],
        "error_runs": aggregate["error_runs"],
        "updated_at": iso_time(),
    }
    atomic_write_json(paths["state"], state)
    aggregate = build_aggregate(storage_dir)
    atomic_write_json(paths["summary"], aggregate)
    return {"record": record, "state": state, "summary": aggregate}


def worker_log(message: str) -> None:
    print(message, file=sys.stderr, flush=True)


def run_worker(args: argparse.Namespace) -> int:
    storage_dir = Path(args.storage_dir).expanduser().resolve()
    interval_seconds = float(args.interval)
    max_runs = 1 if args.once else args.max_runs
    repo_slug = args.repo
    ensure_storage_dir(storage_dir)
    worker_log(
        f"[worker] start repo={repo_slug} interval={format_duration(interval_seconds)} "
        f"storage={storage_dir} max_runs={max_runs or 'forever'}"
    )
    tick = 0
    while True:
        tick += 1
        started = time.monotonic()
        result = run_github_watch_tick(storage_dir, repo_slug, interval_seconds, tick)
        record = result["record"]
        summary = result["summary"]
        if record["status"] == "ok":
            latest = summary.get("latest") or {}
            worker_log(
                f"[worker] tick={tick} repo={repo_slug} status=ok samples={summary['ok_runs']} "
                f"stars={latest.get('stars', '-')} forks={latest.get('forks', '-')} "
                f"open_issues={latest.get('open_issues', '-')} next_run={summary.get('next_run_at')}"
            )
        else:
            worker_log(
                f"[worker] tick={tick} repo={repo_slug} status=error "
                f"error={record.get('error')} next_run={summary.get('next_run_at')}"
            )
        worker_log(f"[worker] summary updated path={storage_paths(storage_dir)['summary']}")
        if max_runs and tick >= max_runs:
            worker_log(f"[worker] stop reason=max_runs ticks={tick}")
            return 0
        elapsed = time.monotonic() - started
        time.sleep(max(0.0, interval_seconds - elapsed))


def call_github_watch_status(storage_dir: Path) -> dict[str, Any]:
    paths = storage_paths(storage_dir)
    state = read_json(paths["state"], {})
    jobs = read_json(paths["jobs"], {})
    if not state and not jobs:
        return tool_result({"error": "watch storage is empty", "storage_dir": str(storage_dir)}, is_error=True)
    health = schedule_health(state, jobs)
    payload = {
        "storage_dir": str(storage_dir),
        "repo": jobs.get("repo") or state.get("repo"),
        "interval_seconds": jobs.get("interval_seconds"),
        "interval": jobs.get("interval"),
        "health": health["health"],
        "missed_schedule": health["missed_schedule"],
        "overdue_seconds": health["overdue_seconds"],
        "last_run_at": state.get("last_run_at"),
        "next_run_at": state.get("next_run_at") or jobs.get("next_run_at"),
        "last_status": state.get("last_status"),
        "total_runs": state.get("total_runs", 0),
        "ok_runs": state.get("ok_runs", 0),
        "error_runs": state.get("error_runs", 0),
    }
    return tool_result(payload)


def call_github_watch_summary(storage_dir: Path) -> dict[str, Any]:
    payload = build_aggregate(storage_dir)
    if not payload.get("total_runs"):
        return tool_result({"error": "watch summary is empty", "storage_dir": str(storage_dir)}, is_error=True)
    return tool_result(payload)


def parse_history_limit(arguments: dict[str, Any]) -> int:
    raw = arguments.get("limit", 10)
    if isinstance(raw, bool) or not isinstance(raw, int):
        raise ValueError("limit must be an integer")
    if raw < 1 or raw > 100:
        raise ValueError("limit must be between 1 and 100")
    return raw


def call_github_watch_history(storage_dir: Path, arguments: dict[str, Any]) -> dict[str, Any]:
    try:
        limit = parse_history_limit(arguments)
    except ValueError as exc:
        return tool_result({"error": str(exc)}, is_error=True)
    runs = load_runs(storage_dir)
    return tool_result(
        {
            "storage_dir": str(storage_dir),
            "total_runs": len(runs),
            "limit": limit,
            "runs": runs[-limit:],
        }
    )


def day19_tool_output_files(storage_dir: Path) -> dict[str, Any]:
    paths = day19_storage_paths(storage_dir)
    return {
        "searches": str(paths["searches"]),
        "reports": str(paths["reports"]),
        "output": str(paths["output"]),
        "pipeline_runs": str(paths["pipeline_runs"]),
    }


def handle_initialize(message_id: Any, params: dict[str, Any]) -> dict[str, Any]:
    return success(
        message_id,
        {
            "protocolVersion": params.get("protocolVersion") or PROTOCOL_VERSION,
            "capabilities": {"tools": {}},
            "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
        },
    )


def handle_tools_call(message_id: Any, params: dict[str, Any], storage_dir: Path) -> dict[str, Any]:
    name = params.get("name")
    arguments = params.get("arguments") or {}

    if not isinstance(arguments, dict):
        return success(message_id, tool_result({"error": "arguments must be an object"}, is_error=True))

    if name == GITHUB_REPO_INFO_TOOL["name"]:
        return success(message_id, call_github_repo_info(arguments))
    if name == GITHUB_WATCH_STATUS_TOOL["name"]:
        return success(message_id, call_github_watch_status(storage_dir))
    if name == GITHUB_WATCH_SUMMARY_TOOL["name"]:
        return success(message_id, call_github_watch_summary(storage_dir))
    if name == GITHUB_WATCH_HISTORY_TOOL["name"]:
        return success(message_id, call_github_watch_history(storage_dir, arguments))
    if name == GITHUB_SEARCH_REPOS_TOOL["name"]:
        return success(message_id, call_github_search_repos(storage_dir, arguments))
    if name == GITHUB_MAKE_REPORT_TOOL["name"]:
        return success(message_id, call_github_make_report(storage_dir, arguments))
    if name == SAVE_REPORT_TOOL["name"]:
        return success(message_id, call_save_report_to_file(storage_dir, arguments))
    return error(message_id, -32602, f"Unknown tool: {name}")


def handle_request(message: dict[str, Any], storage_dir: Path) -> dict[str, Any] | None:
    message_id = message.get("id")
    method = message.get("method")
    params = message.get("params") or {}

    if message_id is None:
        return None
    if not isinstance(params, dict):
        return error(message_id, -32602, "params must be an object")

    try:
        if method == "initialize":
            return handle_initialize(message_id, params)
        if method == "ping":
            return success(message_id, {})
        if method == "tools/list":
            return success(message_id, {"tools": TOOLS})
        if method == "tools/call":
            return handle_tools_call(message_id, params, storage_dir)
    except StorageError as exc:
        return success(message_id, tool_result({"error": str(exc), "storage_dir": str(storage_dir)}, is_error=True))

    return error(message_id, -32601, f"Method not found: {method}")


def run_stdio(storage_dir: Path) -> int:
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue

        try:
            message = json.loads(line)
        except json.JSONDecodeError as exc:
            write_message(error(None, -32700, f"Parse error: {exc.msg}"))
            continue

        if not isinstance(message, dict):
            write_message(error(None, -32600, "Invalid request"))
            continue

        response = handle_request(message, storage_dir)
        if response is not None:
            write_message(response)

    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="GitHub MCP server and Day 18 scheduler")
    parser.add_argument("--storage-dir", default=str(DEFAULT_STORAGE_DIR), help="Day 18 scheduler storage directory")
    subparsers = parser.add_subparsers(dest="command")

    worker = subparsers.add_parser("worker", help="Run the Day 18 scheduled GitHub monitor worker")
    worker.add_argument("--storage-dir", default=str(DEFAULT_STORAGE_DIR), help="Day 18 scheduler storage directory")
    worker.add_argument("--repo", default=DEFAULT_WATCH_REPO, help="GitHub repository in owner/name format")
    worker.add_argument("--interval", type=parse_duration, default=float(DEFAULT_INTERVAL_SECONDS), help="Polling interval, for example 5s or 1m")
    worker.add_argument("--once", action="store_true", help="Run one scheduled tick and exit")
    worker.add_argument("--max-runs", type=int, default=0, help="Stop after this many ticks; 0 means forever")
    worker.add_argument("--demo", action="store_true", help="Demo alias; keeps human stderr logs enabled")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command == "worker":
        if args.max_runs < 0:
            parser.error("--max-runs must be non-negative")
        try:
            return run_worker(args)
        except KeyboardInterrupt:
            worker_log("[worker] stop reason=interrupted")
            return 130
    return run_stdio(Path(args.storage_dir).expanduser().resolve())


if __name__ == "__main__":
    raise SystemExit(main())
