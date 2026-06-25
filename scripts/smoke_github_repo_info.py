#!/usr/bin/env python3
"""Smoke-test the MCP server by listing and calling github_repo_info."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
SERVER = ROOT / "server.py"


def send(process: subprocess.Popen[str], payload: dict[str, Any]) -> dict[str, Any]:
    assert process.stdin is not None
    assert process.stdout is not None

    process.stdin.write(json.dumps(payload) + "\n")
    process.stdin.flush()

    line = process.stdout.readline()
    if not line:
        stderr = process.stderr.read() if process.stderr else ""
        raise RuntimeError(f"MCP server closed stdout. stderr: {stderr}")
    return json.loads(line)


def require_result(response: dict[str, Any], request_id: int) -> dict[str, Any]:
    if response.get("id") != request_id:
        raise AssertionError(f"Unexpected response id: {response.get('id')}")
    if "error" in response:
        raise AssertionError(f"MCP error: {response['error']}")

    result = response.get("result")
    if not isinstance(result, dict):
        raise AssertionError("Response result is not an object")
    return result


def parse_tool_payload(result: dict[str, Any]) -> tuple[dict[str, Any], bool]:
    content = result.get("content")
    if not isinstance(content, list) or not content:
        raise AssertionError("Tool response does not contain content")

    first_item = content[0]
    if not isinstance(first_item, dict) or first_item.get("type") != "text":
        raise AssertionError("Tool response does not contain text content")

    text = first_item.get("text")
    if not isinstance(text, str):
        raise AssertionError("Tool text content is missing")

    payload = json.loads(text)
    if not isinstance(payload, dict):
        raise AssertionError("Tool text content is not a JSON object")
    return payload, bool(result.get("isError"))


def validate_success_payload(payload: dict[str, Any]) -> None:
    required_keys = (
        "api_url",
        "full_name",
        "description",
        "html_url",
        "default_branch",
        "language",
        "stars",
        "forks",
        "open_issues",
        "visibility",
        "updated_at",
    )
    for key in required_keys:
        if key not in payload:
            raise AssertionError(f"Tool payload is missing {key}")


def validate_error_payload(payload: dict[str, Any]) -> None:
    for key in ("api_url", "error"):
        if key not in payload:
            raise AssertionError(f"Tool error payload is missing {key}")


def main() -> int:
    owner = sys.argv[1] if len(sys.argv) > 1 else "nikbrik"
    repo = sys.argv[2] if len(sys.argv) > 2 else "coding_writer"

    process = subprocess.Popen(
        [sys.executable, str(SERVER)],
        cwd=str(ROOT),
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )

    try:
        initialize = require_result(
            send(
                process,
                {
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "initialize",
                    "params": {
                        "protocolVersion": "2024-11-05",
                        "capabilities": {},
                        "clientInfo": {"name": "smoke-client", "version": "0.1.0"},
                    },
                },
            ),
            1,
        )

        assert process.stdin is not None
        process.stdin.write(
            json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized", "params": {}}) + "\n"
        )
        process.stdin.flush()

        tools = require_result(send(process, {"jsonrpc": "2.0", "id": 2, "method": "tools/list"}), 2)
        tool_names = [tool.get("name") for tool in tools.get("tools", []) if isinstance(tool, dict)]
        if "github_repo_info" not in tool_names:
            raise AssertionError("github_repo_info was not registered")

        tool_result = require_result(
            send(
                process,
                {
                    "jsonrpc": "2.0",
                    "id": 3,
                    "method": "tools/call",
                    "params": {"name": "github_repo_info", "arguments": {"owner": owner, "repo": repo}},
                },
            ),
            3,
        )
        payload, is_error = parse_tool_payload(tool_result)

        print(f"server: {initialize['serverInfo']['name']} {initialize['serverInfo']['version']}")
        print(f"registered tools: {', '.join(tool_names)}")
        print(f"called GitHub API: {payload['api_url']}")

        if is_error:
            validate_error_payload(payload)
            print(f"github_repo_info tool error: status={payload.get('status')}, error={payload['error']!r}")
        else:
            validate_success_payload(payload)
            print(
                "github_repo_info result used: "
                f"full_name={payload['full_name']!r}, "
                f"stars={payload['stars']}, "
                f"language={payload['language']!r}, "
                f"updated_at={payload['updated_at']!r}"
            )
    finally:
        process.terminate()
        process.wait(timeout=5)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
