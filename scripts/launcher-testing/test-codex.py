#!/usr/bin/env python3
"""Manual E2E test with real Codex and two local, scripted Responses servers.

Run `cargo build`, then `python3 scripts/launcher-testing/test-codex.py`.
Requires Python 3.11+ and Codex on PATH; no API keys or external model calls.
This is deliberately separate from environment-independent Rust unit tests.
"""

import argparse
import gzip
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import threading
import tomllib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


class ResponsesHandler(BaseHTTPRequestHandler):
    def log_message(self, *_args):
        pass

    def do_POST(self):
        body = self.rfile.read(int(self.headers["Content-Length"]))
        if self.headers.get("Content-Encoding") == "gzip":
            body = gzip.decompress(body)
        request = json.loads(body)
        records = self.server.records
        index = len(records)
        records.append((self.path, dict(self.headers), request))
        if self.server.provider == "main" and index == 0:
            item = self.function_call(
                request, "spawn_agent", "spawn-reviewer",
                {"agent_type": "reviewer", "message": "Report completion", "fork_context": False},
            )
        elif self.server.provider == "main" and index == 1:
            outputs = [item for item in request["input"] if item["type"] == "function_call_output"]
            spawned = json.loads(outputs[-1]["output"])
            item = self.function_call(
                request, "wait_agent", "wait-reviewer",
                {"targets": [spawned["agent_id"]], "timeout_ms": 10000},
            )
        else:
            item = {
                "type": "message", "id": "msg_fixture", "role": "assistant",
                "status": "completed",
                "content": [{"type": "output_text", "text": "e2e done", "annotations": []}],
            }
        response = {
            "id": f"resp_{self.server.provider}_{index}", "object": "response",
            "status": "completed", "output": [item],
            "usage": {"input_tokens": 1, "output_tokens": 1, "total_tokens": 2},
        }
        events = [
            ("response.created", {"response": {"id": response["id"], "status": "in_progress", "output": []}}),
            ("response.output_item.added", {"output_index": 0, "item": item}),
            ("response.output_item.done", {"output_index": 0, "item": item}),
            ("response.completed", {"response": response}),
        ]
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.end_headers()
        for name, data in events:
            event = json.dumps({"type": name, **data})
            self.wfile.write(f"event: {name}\ndata: {event}\n\n".encode())

    @staticmethod
    def function_call(request, name, call_id, arguments):
        call = {"type": "function_call", "name": name, "call_id": call_id, "arguments": json.dumps(arguments)}
        for tool in request["tools"]:
            if tool["type"] == "namespace" and any(child["name"] == name for child in tool["tools"]):
                call["namespace"] = tool["name"]
                return call
            if tool.get("name") == name:
                return call
        raise AssertionError(f"Codex did not expose {name}")


def write_config(home, directory, name, value):
    path = home / directory
    path.mkdir(parents=True, exist_ok=True)
    # JSON is a YAML subset, so no YAML dependency is needed.
    (path / f"{name}.yaml").write_text(json.dumps(value))


def run_fixture(root, granite, codex):
    config = root / "granite"
    source = root / "codex-source"
    source.mkdir()
    servers = []
    try:
        for provider in ["main", "child"]:
            server = ThreadingHTTPServer(("127.0.0.1", 0), ResponsesHandler)
            server.provider = provider
            server.records = []
            servers.append(server)
            threading.Thread(target=server.serve_forever, daemon=True).start()
            write_config(config, "providers", provider, {
                "provider_id": provider, "type": "openai-compatible", "config": {
                    "base_url": f"http://127.0.0.1:{server.server_port}",
                    "api_key": f"{provider}-key",
                    "custom_headers": {"X-Test-Provider": provider, f"X-{provider}-only": provider},
                },
            })
            write_config(config, "models", f"{provider}-model", {
                "model_id": f"{provider}-model", "model_type": "custom", "provider_id": provider,
                "config": {"family": "test", "supported_functions": ["Chat", "ToolCalling"]},
            })
        write_config(config, "capabilities", "primary", {
            "capability_id": "primary", "type": "agent-model", "config": {"model_id": "main-model"},
        })
        reviewer = {
            "capability_id": "reviewer", "type": "sub-agent", "config": {
                "model_id": "child-model", "description": "test reviewer", "prompt": "CHILD_ROLE", "tools": [],
            },
        }
        write_config(config, "capabilities", "reviewer", reviewer)
        launcher = {
            "launcher_id": "e2e", "type": "codex", "enabled_capabilities": ["primary", "reviewer"],
            "config": {"command_path": codex, "model_overrides": {"name": "Overridden", "request_max_retries": 0}},
        }
        write_config(config, "launchers", "e2e", launcher)
        env = dict(os.environ, GRANITE_CLI_HOME=str(config), CODEX_HOME=str(source))

        def launch(extra_args):
            result = subprocess.run(
                [str(granite), "launch", "e2e", *extra_args], cwd=root, env=env,
                input="", text=True, capture_output=True, timeout=45,
            )
            (root / "stdout.txt").write_text(result.stdout)
            (root / "stderr.txt").write_text(result.stderr)
            return result

        result = launch([
            "--", "--no-daemon", "exec", "--skip-git-repo-check", "--ephemeral", "--json",
            "-C", str(root), "E2E fixture",
        ])
        assert result.returncode == 0, result.stderr
        assert "e2e done" in result.stdout, result.stdout
        for server in servers:
            provider = server.provider
            assert len(server.records) >= (3 if provider == "main" else 1), server.records
            for index, (path, headers, request) in enumerate(server.records):
                (root / f"{provider}-request-{index}.json").write_text(json.dumps(request, indent=2))
                lower = {key.lower(): value for key, value in headers.items()}
                assert path == "/v1/responses", path
                assert request["model"] == f"{provider}-model", request["model"]
                assert lower["x-test-provider"] == provider, headers
                assert lower["authorization"] == f"Bearer {provider}-key", headers
                other = "child" if provider == "main" else "main"
                assert f"x-{other}-only" not in lower, headers
        generated = tomllib.loads((config / "launcher-state/e2e/config.toml").read_text())
        assert generated["model_providers"]["main"]["name"] == "Overridden"
        print("PASS: main/child routing, /v1/responses, per-provider auth/headers, merged TOML")

        # Restricted roles must fail before Codex starts or makes a request.
        before = [len(server.records) for server in servers]
        reviewer["config"]["tools"] = ["FileRead", "Search"]
        write_config(config, "capabilities", "reviewer", reviewer)
        result = launch([])
        assert result.returncode != 0
        assert "cannot enforce the read-only tool list" in result.stderr, result.stderr
        assert before == [len(server.records) for server in servers]
        print("PASS: unsupported read-only role rejected before model traffic")

        # Both main-provider and sub-agent-provider collisions must be explained.
        reviewer["config"]["tools"] = []
        write_config(config, "capabilities", "reviewer", reviewer)
        for provider in ["main", "child"]:
            launcher["config"]["extra_providers"] = {provider: {"base_url": "http://unused.invalid/v1"}}
            write_config(config, "launchers", "e2e", launcher)
            result = launch(["--dry-run"])
            assert result.returncode != 0
            assert f"conflicts with provider id '{provider}'" in result.stderr, result.stderr
        print("PASS: main/sub-agent provider collisions rejected with conflicting IDs")
    finally:
        for server in servers:
            server.shutdown()
            server.server_close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--granite", type=Path, default=Path(__file__).resolve().parents[2] / "target/debug/granite-cli")
    parser.add_argument("--codex", default=shutil.which("codex"))
    args = parser.parse_args()
    if not args.codex:
        parser.error("install Codex or pass --codex /path/to/codex")
    if not args.granite.is_file():
        parser.error("build granite-cli first with cargo build, or pass --granite /path/to/granite-cli")
    print(subprocess.check_output([args.codex, "--version"], text=True).strip())
    root = Path(tempfile.mkdtemp(prefix="granite-codex-e2e-"))
    print(f"Artifacts: {root}")
    run_fixture(root, args.granite.resolve(), args.codex)


if __name__ == "__main__":
    main()
