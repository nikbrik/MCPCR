#!/usr/bin/env python3
"""Smoke-test Day 18 scheduled storage and MCP read tools."""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import time
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
    first = content[0]
    if not isinstance(first, dict) or first.get("type") != "text":
        raise AssertionError("Tool response does not contain text")
    payload = json.loads(first.get("text", ""))
    if not isinstance(payload, dict):
        raise AssertionError("Tool text is not a JSON object")
    return payload, bool(result.get("isError"))


def call_tool(process: subprocess.Popen[str], request_id: int, name: str, arguments: dict[str, Any] | None = None) -> tuple[dict[str, Any], bool]:
    result = require_result(
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
    return parse_tool_payload(result)


def main() -> int:
    repo = sys.argv[1] if len(sys.argv) > 1 else "nikbrik/coding_writer"
    with tempfile.TemporaryDirectory(prefix="day18-watch-") as tmp:
        storage_dir = Path(tmp)
        worker = subprocess.run(
            [
                sys.executable,
                str(SERVER),
                "worker",
                "--storage-dir",
                str(storage_dir),
                "--repo",
                repo,
                "--interval",
                "1s",
                "--max-runs",
                "2",
                "--demo",
            ],
            cwd=str(ROOT),
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=20,
            check=False,
        )
        if worker.returncode != 0:
            raise RuntimeError(f"worker failed: stdout={worker.stdout!r} stderr={worker.stderr!r}")
        if worker.stdout.strip():
            raise AssertionError(f"worker wrote to stdout: {worker.stdout!r}")
        for path in (storage_dir / "runs.jsonl", storage_dir / "latest_summary.json", storage_dir / "state.json"):
            if not path.exists():
                raise AssertionError(f"expected storage file missing: {path}")

        time.sleep(2.2)

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
                            "clientInfo": {"name": "day18-smoke", "version": "0.1.0"},
                        },
                    },
                ),
                1,
            )
            assert process.stdin is not None
            process.stdin.write(json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized", "params": {}}) + "\n")
            process.stdin.flush()
            tools = require_result(send(process, {"jsonrpc": "2.0", "id": 2, "method": "tools/list"}), 2)
            tool_names = [tool.get("name") for tool in tools.get("tools", []) if isinstance(tool, dict)]
            for expected in ("github_watch_status", "github_watch_summary", "github_watch_history"):
                if expected not in tool_names:
                    raise AssertionError(f"missing tool: {expected}")
            status, status_error = call_tool(process, 3, "github_watch_status")
            summary, summary_error = call_tool(process, 4, "github_watch_summary")
            history, history_error = call_tool(process, 5, "github_watch_history", {"limit": 2})
            if status_error or summary_error or history_error:
                raise AssertionError(f"unexpected tool error: {status_error=} {summary_error=} {history_error=}")
            if summary.get("total_runs", 0) < 2 or len(history.get("runs", [])) != 2:
                raise AssertionError(f"aggregate did not include worker samples: {summary=} {history=}")
            if status.get("health") != "stale" or not status.get("missed_schedule"):
                raise AssertionError(f"status did not mark stopped worker stale: {status=}")
            if summary.get("health") != "stale" or not summary.get("missed_schedule"):
                raise AssertionError(f"summary did not mark stopped worker stale: {summary=}")
            print(f"server: {initialize['serverInfo']['name']} {initialize['serverInfo']['version']}")
            print(f"worker stderr lines: {len(worker.stderr.splitlines())}")
            print(f"registered watch tools: {', '.join(name for name in tool_names if name.startswith('github_watch_'))}")
            print(f"summary used: repo={summary['repo']!r}, total_runs={summary['total_runs']}, health={summary['health']!r}")
            print(f"history used: returned={len(history['runs'])}, storage={storage_dir}")
        finally:
            process.terminate()
            process.wait(timeout=5)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
