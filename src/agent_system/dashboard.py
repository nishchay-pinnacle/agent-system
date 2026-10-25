"""Loopback dashboard with narrow, worker-applied research commands."""

import argparse
import json
import secrets
from functools import partial
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib.resources import files
from pathlib import Path
from socket import socket
from socketserver import BaseServer
from urllib.parse import parse_qs, unquote, urlsplit

from pydantic import JsonValue

from agent_system.company_intelligence.approvals import UserCommand, enqueue, inbox
from agent_system.company_intelligence.artifacts import (
    read_events,
    read_json,
    source_path,
)
from agent_system.company_intelligence.call_traces import (
    call_details,
    call_summaries,
    safe_path,
)
from agent_system.company_intelligence.coordination import Document
from agent_system.company_intelligence.execution import quality_preview
from agent_system.company_intelligence.page_content import cleaned_path
from agent_system.company_intelligence.research_questions import QUESTIONS
from agent_system.company_intelligence.schemas import Research, Source
from agent_system.company_intelligence.site_index import search_inventory
from agent_system.config import data_directory
from agent_system.worker import job_id_argument


def job_directory(data_dir: Path, job_id: str) -> Path:
    canonical = job_id_argument(job_id)
    directory = data_dir / "jobs" / canonical
    if directory.is_symlink() or not (directory / "dashboard.json").is_file():
        raise ValueError("Research job not found")
    return directory


class DashboardHandler(BaseHTTPRequestHandler):
    def __init__(
        self,
        request: socket | tuple[bytes, socket],
        client_address: object,
        server: BaseServer,
        *,
        directory: Path,
        csrf_token: str = "",
    ) -> None:
        self.csrf_token = csrf_token
        self.directory = directory
        super().__init__(request, client_address, server)

    def do_GET(self) -> None:
        parsed = urlsplit(self.path)
        route = unquote(parsed.path)
        query = parse_qs(parsed.query)
        try:
            if route == "/":
                self.send_body(
                    200,
                    files("agent_system").joinpath("dashboard.html").read_bytes(),
                    "text/html; charset=utf-8",
                )
                return
            if route in {
                "/dashboard.css",
                "/dashboard.js",
                "/dashboard-timing.js",
                "/dashboard-system.js",
            }:
                self.send_body(
                    200,
                    files("agent_system").joinpath(route[1:]).read_bytes(),
                    "text/css; charset=utf-8"
                    if route.endswith("css")
                    else "text/javascript; charset=utf-8",
                )
                return
            if route == "/api/system":
                dashboard = read_json(safe_path(self.directory, "dashboard.json"))
                system = dashboard.get("system")
                worker_path = self.directory / "worker.json"
                commands: list[JsonValue] = []
                if (self.directory / "commands.sqlite").exists():
                    with inbox(self.directory) as connection:
                        commands = [
                            {
                                "command": UserCommand.model_validate_json(
                                    payload
                                ).model_dump(mode="json"),
                                "outcome": outcome,
                            }
                            for payload, outcome in connection.execute(
                                "SELECT payload,outcome FROM commands ORDER BY rowid"
                            )
                        ]
                self.send_json(
                    200,
                    {
                        "system": system,
                        "job_id": dashboard["job_id"],
                        "status": dashboard["status"],
                        "settings": dashboard["settings"],
                        "worker": read_json(worker_path)
                        if worker_path.exists()
                        else {},
                        "csrf_token": self.csrf_token,
                        "commands": commands,
                    },
                )
            elif route == "/api/quality-preview":
                dashboard = read_json(safe_path(self.directory, "dashboard.json"))
                state = Research.model_validate(
                    {
                        "instructions": "",
                        **{
                            k: v
                            for k, v in dashboard.items()
                            if k not in {"system", "assessments"}
                        },
                    }
                )
                doc = Document.model_validate(dashboard.get("system"))
                self.send_json(
                    200, quality_preview(doc, state, query.get("batch", [None])[0])
                )
            elif route == "/api/calls":
                self.send_json(
                    200,
                    call_summaries(
                        self.directory,
                        query.get("phase", [""])[0],
                        query.get("status", [""])[0],
                        query.get("counts", ["false"])[0] == "true",
                        int(query.get("offset", ["0"])[0]),
                    ),
                )
            elif route.startswith("/api/call/"):
                self.send_json(
                    200, call_details(self.directory, route.removeprefix("/api/call/"))
                )
            elif route == "/api/inventory":
                safe_path(self.directory, "site_index.json")
                self.send_json(
                    200,
                    search_inventory(
                        self.directory,
                        query.get("q", [""])[0],
                        20,
                        int(query.get("offset", ["0"])[0]),
                    ),
                )
            elif route == "/api/orientation":
                self.send_json(
                    200, read_json(safe_path(self.directory, "orientation.json"))
                )
            elif route == "/api/state":
                dashboard = read_json(safe_path(self.directory, "dashboard.json"))
                records = sorted((self.directory / "investigations").glob("*.json"))[
                    -12:
                ]
                issues: list[JsonValue] = []

                def optional_artifact(relative: str, fallback: JsonValue) -> JsonValue:
                    path = safe_path(self.directory, relative)
                    try:
                        return read_json(path)
                    except (ValueError, OSError):
                        issues.append(
                            f"{relative} unavailable or incomplete; displaying saved checkpoint projection where available"
                        )
                        return fallback

                self.send_json(
                    200,
                    {
                        "dashboard": dashboard,
                        "questions": {k: v for k, v in QUESTIONS.items()},
                        "profile": optional_artifact(
                            "profile.json", dashboard.get("profile", {})
                        ),
                        "sources": optional_artifact(
                            "sources.json", dashboard.get("sources", {})
                        ),
                        "investigations": [
                            optional_artifact(str(p.relative_to(self.directory)), {})
                            for p in records
                        ],
                        "artifact_errors": issues,
                    },
                )
            elif route == "/api/events":
                safe_path(self.directory, "events.jsonl")
                after = int(query.get("after", ["0"])[0])
                if after < 0:
                    raise ValueError("after must be nonnegative")
                self.send_json(
                    200, {"events": [e for e in read_events(self.directory, after)]}
                )
            elif route.startswith("/api/source/"):
                sid = route.removeprefix("/api/source/")
                manifest = read_json(safe_path(self.directory, "sources.json"))
                if sid not in manifest:
                    self.send_json(404, {"error": "Source not found"})
                    return
                source = Source.model_validate(manifest[sid])
                representation = query.get("representation", ["raw"])[0]
                if representation not in {"raw", "cleaned"}:
                    raise ValueError("Unknown representation")
                if source.outcome != "success":
                    raise ValueError("Source has no successful saved scrape")
                if representation == "cleaned" and not source.cleaned_filename:
                    self.send_json(
                        404,
                        {
                            "error": "Cleaned text unavailable for this historical source"
                        },
                    )
                    return
                path = (
                    cleaned_path(self.directory, source)
                    if representation == "cleaned"
                    else source_path(self.directory, source)
                )
                lines = path.read_text(encoding="utf-8").splitlines()
                start = int(query.get("start_line", ["1"])[0])
                count = int(query.get("max_lines", ["200"])[0])
                if start < 1 or not 1 <= count <= 500:
                    raise ValueError("Invalid source pagination")
                end = min(len(lines), start + count - 1)
                result: dict[str, JsonValue] = {
                    "source_id": sid,
                    "title": source.title,
                    "url": source.requested_url,
                    "representation": representation,
                    "cleaner_version": source.cleaner_version
                    if representation == "cleaned"
                    else None,
                    "start_line": start,
                    "end_line": end,
                    "total_lines": len(lines),
                    "content": "\n".join(lines[start - 1 : end]),
                    "more": end < len(lines),
                    "cleaning_stats": source.model_dump(mode="json")["cleaning_stats"],
                    "cleaning_summary": source.cleaning_summary,
                    "content_hash": source.cleaned_hash
                    if representation == "cleaned"
                    else source.raw_hash,
                }
                self.send_json(200, result)
            elif route == "/api/result":
                path = safe_path(self.directory, "result.json")
                self.send_json(
                    200 if path.exists() else 404,
                    read_json(path)
                    if path.exists()
                    else {"status": "not_yet_available"},
                )
            else:
                self.send_json(404, {"error": "Route not found"})
        except (ValueError, OSError) as exc:
            self.send_json(400, {"error": str(exc)[:200]})

    def do_POST(self) -> None:
        try:
            host = self.headers.get("Host", "")
            expected = f"127.0.0.1:{self.connection.getsockname()[1]}"
            if host != expected or self.headers.get("Origin") != f"http://{expected}":
                self.send_json(403, {"error": "Same-origin loopback commands only"})
                return
            if not self.csrf_token or not secrets.compare_digest(
                self.headers.get("X-CSRF-Token", ""), self.csrf_token
            ):
                self.send_json(403, {"error": "Invalid CSRF token"})
                return
            if self.headers.get("Content-Type") != "application/json":
                raise ValueError("JSON content type required")
            length = int(self.headers.get("Content-Length", "0"))
            if not 0 < length <= 16384:
                raise ValueError("Invalid command size")
            command = UserCommand.model_validate_json(self.rfile.read(length))
            dashboard = read_json(safe_path(self.directory, "dashboard.json"))
            if (
                command.job_id != dashboard["job_id"]
                or self.path != f"/api/jobs/{command.job_id}/commands"
            ):
                raise ValueError("Wrong job command route")
            if dashboard.get("schema_version") != 3:
                raise ValueError("Historical jobs do not accept version-three commands")
            outcome = enqueue(
                self.directory, command, Document.model_validate(dashboard["system"])
            )
            self.send_json(202, {"command_id": command.command_id, "outcome": outcome})
        except (ValueError, OSError) as exc:
            self.send_json(409, {"error": str(exc)[:300]})

    def send_json(self, status: int, value: dict[str, JsonValue]) -> None:
        self.send_body(
            status, json.dumps(value).encode(), "application/json; charset=utf-8"
        )

    def send_body(self, status: int, body: bytes, content_type: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header(
            "Content-Security-Policy",
            "default-src 'self'; script-src 'self'; style-src 'self'; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'",
        )
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args: object) -> None:
        pass


def make_server(directory: Path, port: int) -> ThreadingHTTPServer:
    return ThreadingHTTPServer(
        ("127.0.0.1", port),
        partial(
            DashboardHandler, directory=directory, csrf_token=secrets.token_urlsafe(32)
        ),
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--job", required=True, type=job_id_argument)
    parser.add_argument("--port", default=8765, type=int)
    args = parser.parse_args()
    try:
        directory = job_directory(data_directory(), args.job)
        with make_server(directory, args.port) as server:
            print(
                f"Local dashboard: http://127.0.0.1:{server.server_port}",
                flush=True,
            )
            server.serve_forever()
    except KeyboardInterrupt:
        return 0
    except (ValueError, OSError) as exc:
        parser.exit(1, f"Dashboard: {exc}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
