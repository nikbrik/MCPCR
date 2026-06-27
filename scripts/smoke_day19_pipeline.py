#!/usr/bin/env python3
"""Smoke-test Day 19 MCP composition pipeline.

Expected flow:
  github_search_repos -> github_make_report -> save_report_to_file
"""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
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


def call_tool(process: subprocess.Popen[str], request_id: int, name: str, arguments: dict[str, Any] | None = None) -> tuple[dict[str, Any], bool]:
    return parse_tool_payload(
        require_result(
            send(
                process,
                {
                    "jsonrpc": "2.0",
                    "id": request_id,
                    "method": "tools/call",
                    "params": {"name": name, "arguments": arguments or {}},
                },
            ),
            request_id,
        )
    )


def main() -> int:
    query = sys.argv[1] if len(sys.argv) > 1 else "mcp server python"
    limit = int(sys.argv[2]) if len(sys.argv) > 2 else None

    with tempfile.TemporaryDirectory(prefix="day19-pipeline-") as tmp:
        storage_dir = Path(tmp)

        process = subprocess.Popen(
            [sys.executable, str(SERVER), "--storage-dir", str(storage_dir)],
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
                            "clientInfo": {"name": "day19-smoke", "version": "0.1.0"},
                        },
                    },
                ),
                1,
            )

            assert process.stdin is not None
            process.stdin.write(json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized", "params": {}}) + "\n")
            process.stdin.flush()

            tools = require_result(
                send(process, {"jsonrpc": "2.0", "id": 2, "method": "tools/list"}),
                2,
            )
            names = [tool.get("name") for tool in tools.get("tools", []) if isinstance(tool, dict)]
            for expected in ("github_search_repos", "github_make_report", "save_report_to_file"):
                if expected not in names:
                    raise AssertionError(f"missing tool: {expected}")

            search_args: dict[str, Any] = {"query": query}
            if limit is not None:
                search_args["limit"] = limit

            search_result, search_error = call_tool(process, 3, "github_search_repos", search_args)
            if search_error:
                raise AssertionError(f"search tool unexpectedly failed: {search_result}")
            search_id = search_result.get("search_id")
            if not isinstance(search_id, str) or not search_id:
                raise AssertionError(f"search_id missing in tool response: {search_result}")
            returned = int(search_result.get("returned", 0))
            if returned <= 0:
                raise AssertionError(f"search returned no repos: {search_result}")

            report_result, report_error = call_tool(
                process,
                4,
                "github_make_report",
                {"search_id": search_id},
            )
            if report_error:
                raise AssertionError(f"report tool unexpectedly failed: {report_result}")
            report_id = report_result.get("report_id")
            if not isinstance(report_id, str) or not report_id:
                raise AssertionError(f"report_id missing in tool response: {report_result}")

            save_result, save_error = call_tool(
                process,
                5,
                "save_report_to_file",
                {"report_id": report_id},
            )
            if save_error:
                raise AssertionError(f"save tool unexpectedly failed: {save_result}")
            path = Path(str(save_result.get("path", "")))
            if not path.exists():
                raise AssertionError(f"expected output file to exist: {path}")
            if not path.read_text(encoding="utf-8").strip():
                raise AssertionError(f"output file is empty: {path}")
            if path.parent.name != "output":
                raise AssertionError(f"output path not under output/: {path}")

            day19_events = storage_dir / "pipeline_runs.jsonl"
            if not day19_events.exists() or day19_events.stat().st_size == 0:
                raise AssertionError(f"pipeline_runs.jsonl missing or empty: {day19_events}")

            print(f"server: {initialize['serverInfo']['name']} {initialize['serverInfo']['version']}")
            print(f"registered tools: {', '.join(sorted(names))}")
            print(f"search: id={search_id} returned={returned}")
            print(f"report: id={report_id} query={report_result.get('query')!r}")
            print(f"output: {path}")
        finally:
            process.terminate()
            process.wait(timeout=5)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
