#!/usr/bin/env python3
"""Minimal MCP server around the public GitHub REST API.

Transport: stdio.
Protocol style: JSON-RPC messages used by MCP clients.
Dependencies: Python standard library only.
"""

from __future__ import annotations

import json
import sys
import urllib.error
import urllib.parse
import urllib.request
from typing import Any


SERVER_NAME = "github-api-mcp-server"
SERVER_VERSION = "0.1.0"
PROTOCOL_VERSION = "2024-11-05"
GITHUB_API_BASE_URL = "https://api.github.com"
GITHUB_USER_AGENT = "mcp-server-github-demo"


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


class GitHubApiError(RuntimeError):
    def __init__(self, status: int | None, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.message = message


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


def handle_initialize(message_id: Any, params: dict[str, Any]) -> dict[str, Any]:
    return success(
        message_id,
        {
            "protocolVersion": params.get("protocolVersion") or PROTOCOL_VERSION,
            "capabilities": {"tools": {}},
            "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
        },
    )


def handle_tools_call(message_id: Any, params: dict[str, Any]) -> dict[str, Any]:
    name = params.get("name")
    arguments = params.get("arguments") or {}

    if name != GITHUB_REPO_INFO_TOOL["name"]:
        return error(message_id, -32602, f"Unknown tool: {name}")
    if not isinstance(arguments, dict):
        return success(message_id, tool_result({"error": "arguments must be an object"}, is_error=True))

    return success(message_id, call_github_repo_info(arguments))


def handle_request(message: dict[str, Any]) -> dict[str, Any] | None:
    message_id = message.get("id")
    method = message.get("method")
    params = message.get("params") or {}

    if message_id is None:
        return None
    if not isinstance(params, dict):
        return error(message_id, -32602, "params must be an object")

    if method == "initialize":
        return handle_initialize(message_id, params)
    if method == "ping":
        return success(message_id, {})
    if method == "tools/list":
        return success(message_id, {"tools": [GITHUB_REPO_INFO_TOOL]})
    if method == "tools/call":
        return handle_tools_call(message_id, params)

    return error(message_id, -32601, f"Method not found: {method}")


def main() -> int:
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

        response = handle_request(message)
        if response is not None:
            write_message(response)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
