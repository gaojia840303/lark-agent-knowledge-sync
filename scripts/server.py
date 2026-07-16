#!/usr/bin/env python3
"""Small authenticated HTTP runner for Feishu automation and weekly schedules."""

from __future__ import annotations

import argparse
import hmac
import json
import os
import sys
import threading
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

sys.path.insert(0, str(Path(__file__).resolve().parent))
import sync  # noqa: E402


class Registry:
    def __init__(self, config_dir: Path):
        self.config_dir = config_dir.resolve()
        self.runs: dict[str, dict] = {}
        self.lock = threading.Lock()

    def agent_ids(self):
        ids = set()
        for path in self._candidate_paths():
            try:
                config, _ = sync.load_config(path)
                ids.add(str(config["agent"]["id"]))
            except sync.SyncError:
                continue
        return sorted(ids)

    def _candidate_paths(self):
        paths = set(self.config_dir.glob("*.yaml")) | set(self.config_dir.glob("*.yml"))
        paths |= set(self.config_dir.glob("*/agent-sync.yaml")) | set(self.config_dir.glob("*/agent-sync.yml"))
        return sorted(paths)

    def config(self, agent_id: str):
        candidates = [
            self.config_dir / f"{agent_id}.yaml",
            self.config_dir / f"{agent_id}.yml",
            self.config_dir / "agent-sync.yaml",
            self.config_dir / "agent-sync.yml",
            *self._candidate_paths(),
        ]
        seen = set()
        for path in candidates:
            if not path.exists() or path in seen:
                continue
            seen.add(path)
            try:
                config, resolved = sync.load_config(path)
            except sync.SyncError:
                continue
            if config["agent"]["id"] == agent_id:
                return config, resolved
        raise sync.ConfigError(f"未找到 Agent 配置: {agent_id}")

    def scan(self, agent_id: str) -> dict:
        config, path = self.config(agent_id)
        runner = sync.ToolRunner(config)
        result = sync.scan(config, path, runner, sync.make_store(config, path, runner))
        with self.lock:
            self.runs[result["run_id"]] = result
        return result

    def apply(self, agent_id: str, proposal_id: str) -> dict:
        config, path = self.config(agent_id)
        runner = sync.ToolRunner(config)
        return sync.apply_proposal(config, proposal_id, runner, sync.make_store(config, path, runner))


def handler_factory(registry: Registry, secret: str):
    class Handler(BaseHTTPRequestHandler):
        server_version = "AgentKnowledgeSync/1.0"

        def _reply(self, status: int, body: dict):
            payload = json.dumps(body, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def _authorized(self) -> bool:
            supplied = self.headers.get("Authorization", "")
            expected = f"Bearer {secret}"
            return bool(secret) and hmac.compare_digest(supplied, expected)

        def _body(self) -> dict:
            length = int(self.headers.get("Content-Length", "0") or 0)
            if length > 65536:
                raise sync.ConfigError("请求正文过大")
            if not length:
                return {}
            parsed = json.loads(self.rfile.read(length).decode("utf-8"))
            if not isinstance(parsed, dict):
                raise sync.ConfigError("请求正文必须是 JSON 对象")
            return parsed

        def do_GET(self):
            path = urlparse(self.path).path
            if path == "/health":
                return self._reply(200, {"status": "ok"})
            if not self._authorized():
                return self._reply(401, {"error": "unauthorized"})
            parts = path.strip("/").split("/")
            if len(parts) == 3 and parts[:2] == ["v1", "runs"]:
                run = registry.runs.get(parts[2])
                return self._reply(200 if run else 404, run or {"error": "run not found"})
            if len(parts) == 4 and parts[:2] == ["v1", "agents"] and parts[3] == "status":
                config, config_path = registry.config(parts[2])
                runner = sync.ToolRunner(config)
                return self._reply(200, sync.make_store(config, config_path, runner).status())
            return self._reply(404, {"error": "not found"})

        def do_POST(self):
            if not self._authorized():
                return self._reply(401, {"error": "unauthorized"})
            try:
                path = urlparse(self.path).path
                parts = path.strip("/").split("/")
                body = self._body()
                if len(parts) == 4 and parts[:2] == ["v1", "agents"] and parts[3] == "sync":
                    # The body is intentionally ignored: it may not override sources or destinations.
                    return self._reply(202, registry.scan(parts[2]))
                if len(parts) == 4 and parts[:2] == ["v1", "proposals"] and parts[3] == "apply":
                    agent_id = str(body.get("agent_id", ""))
                    if not agent_id:
                        raise sync.ConfigError("缺少 agent_id")
                    return self._reply(200, registry.apply(agent_id, parts[2]))
                return self._reply(404, {"error": "not found"})
            except (sync.SyncError, ValueError, json.JSONDecodeError) as exc:
                return self._reply(400, {"error": str(exc)})
            except Exception as exc:  # keep the service available but avoid stack traces in responses
                return self._reply(500, {"error": f"internal error: {type(exc).__name__}"})

        def log_message(self, fmt, *args):
            sys.stderr.write((fmt % args) + "\n")

    return Handler


def weekly_loop(registry: Registry, weekday: int, clock: str, stop: threading.Event):
    hour, minute = (int(part) for part in clock.split(":", 1))
    last_date = None
    while not stop.wait(30):
        now = datetime.now()
        if now.weekday() != weekday or (now.hour, now.minute) != (hour, minute) or last_date == now.date():
            continue
        last_date = now.date()
        for agent_id in registry.agent_ids():
            try:
                registry.scan(agent_id)
            except Exception as exc:
                failure = {
                    "run_id": f"scheduler-{agent_id}-{now:%Y%m%d%H%M}",
                    "agent_id": agent_id,
                    "status": "failed",
                    "errors": [{"source": "scheduler", "error": str(exc)}],
                }
                with registry.lock:
                    registry.runs[failure["run_id"]] = failure


def main(argv=None):
    parser = argparse.ArgumentParser(description="Agent knowledge sync HTTP runner")
    parser.add_argument("--config-dir", default=os.getenv("AGENT_SYNC_CONFIG_DIR", ""))
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--weekly", action="store_true", help="enable the built-in weekly fallback schedule")
    parser.add_argument("--weekly-weekday", type=int, choices=range(7), default=0, metavar="0-6")
    parser.add_argument("--weekly-time", default="09:00", help="local time in HH:MM")
    args = parser.parse_args(argv)
    secret = os.getenv("AGENT_SYNC_WEBHOOK_SECRET", "")
    if not args.config_dir:
        parser.error("请传入 --config-dir 或设置 AGENT_SYNC_CONFIG_DIR")
    if len(secret) < 24:
        parser.error("AGENT_SYNC_WEBHOOK_SECRET 必须至少 24 个字符")
    if not __import__("re").fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", args.weekly_time):
        parser.error("--weekly-time 必须使用 HH:MM")
    registry = Registry(Path(args.config_dir))
    server = ThreadingHTTPServer((args.host, args.port), handler_factory(registry, secret))
    stop = threading.Event()
    scheduler = None
    if args.weekly:
        scheduler = threading.Thread(
            target=weekly_loop,
            args=(registry, args.weekly_weekday, args.weekly_time, stop),
            daemon=True,
            name="weekly-sync",
        )
        scheduler.start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        stop.set()
        server.server_close()


if __name__ == "__main__":
    main()
