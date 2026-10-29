"""Localhost dashboard with saved continuation decisions; no provider calls."""

import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib.resources import files
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from bs4 import BeautifulSoup
from markdown_it import MarkdownIt
from pydantic import JsonValue

from research_agent.storage import (
    ContinuationDecision,
    Run,
    local_path,
    read_json,
    run_directory,
    save_json,
)


def render_profile(content: str) -> str:
    # CommonMark escapes raw HTML and rejects unsafe link schemes. Tables are
    # already supported by the installed parser; no frontend dependency needed.
    rendered = MarkdownIt("commonmark", {"html": False}).enable("table").render(content)
    soup = BeautifulSoup(rendered, "html.parser")
    for image in soup.find_all("img"):
        image.replace_with(str(image.get("alt", "")))
    for link in soup.find_all("a", href=True):
        href = str(link["href"])
        if urlsplit(href).scheme not in {"http", "https", "mailto"}:
            link.unwrap()
        else:
            link["rel"] = "noopener noreferrer"
            link["target"] = "_blank"
    return str(soup)


def state(directory: Path) -> dict[str, JsonValue]:
    def optional(name: str) -> dict[str, JsonValue] | None:
        return (
            read_json(directory, name) if local_path(directory, name).exists() else None
        )

    calls: list[JsonValue] = []
    for path in sorted(local_path(directory, "debug").glob("call-*.response.json")):
        calls.append(read_json(directory, f"debug/{path.name}").get("usage"))
    sources: list[JsonValue] = []
    index = optional("source_index.json") or {}
    for path in sorted(local_path(directory, "sources").glob("s_*.json")):
        if path.name.endswith(".capture.json") or ".failed-" in path.name:
            continue
        metadata = read_json(directory, f"sources/{path.name}")
        reference = f"sources/{path.stem}.md"
        if not local_path(directory, reference).exists():
            continue
        sources.append(
            {
                "id": next(
                    (k for k, v in index.items() if v == metadata.get("requested_url")),
                    path.stem,
                ),
                "url": metadata.get("requested_url"),
                "title": metadata.get("title"),
                "content": local_path(directory, reference).read_text(),
            }
        )
    profile = local_path(directory, "profile.md")
    profile_text = profile.read_text() if profile.exists() else None
    return {
        "run": read_json(directory, "run.json"),
        "questions": optional("questions.json"),
        "context": optional("debug/context.json"),
        "firecrawl": optional("debug/firecrawl-counts.json"),
        "calls": calls,
        "profile": profile_text,
        "profile_html": render_profile(profile_text)
        if profile_text is not None
        else None,
        "sources": sources,
    }


def events(directory: Path, offset: int) -> dict[str, JsonValue]:
    path = local_path(directory, "events.jsonl")
    if not path.exists():
        return {"events": [], "offset": 0}
    result: list[JsonValue] = []
    with path.open("rb") as handle:
        handle.seek(min(max(0, offset), path.stat().st_size))
        while line := handle.readline():
            if not line.endswith(b"\n"):
                handle.seek(-len(line), 1)
                break
            result.append(json.loads(line))
        return {"events": result, "offset": handle.tell()}


def handler(root: Path, selected: str | None) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, format: str, *args: object) -> None:
            pass

        def do_POST(self) -> None:
            if urlsplit(self.path).path != "/api/continuation":
                self.send_error(501)
                return
            assert isinstance(self.server, ThreadingHTTPServer)
            host = self.headers.get("Host", "")
            if (
                host
                not in {
                    f"127.0.0.1:{self.server.server_port}",
                    f"localhost:{self.server.server_port}",
                }
                or self.headers.get("Origin") != f"http://{host}"
            ):
                self.send_error(403)
                return
            try:
                if self.headers.get("Content-Type") != "application/json":
                    raise ValueError("Expected JSON")
                length = int(self.headers.get("Content-Length", "0"))
                if not 0 < length <= 4096:
                    raise ValueError("Invalid body size")
                query = parse_qs(urlsplit(self.path).query)
                directory = run_directory(root, query.get("run", [selected or ""])[0])
                decision = ContinuationDecision.model_validate_json(
                    self.rfile.read(length)
                )
                run = Run.model_validate(read_json(directory, "run.json"))
                pending = run.continuation
                if (
                    pending is None
                    or pending.status != "pending"
                    or pending.request_id != decision.request_id
                ):
                    raise ValueError("No matching pending request")
                if decision.approve and (
                    run.settings.max_model_calls + decision.additional_calls > 1000
                    or run.research_page_budget + decision.additional_pages > 10000
                    or (run.stage != "research" and decision.additional_pages)
                ):
                    raise ValueError("Allowance exceeds supported limits")
                save_json(
                    directory, "continuation-decision.json", decision.model_dump()
                )
            except (ValueError, OSError):
                self.send_error(400, "Invalid continuation decision")
                return
            self.send_response(204)
            self.end_headers()

        def do_GET(self) -> None:
            # Reject DNS rebinding and cross-origin reads; bind only IPv4 localhost.
            assert isinstance(self.server, ThreadingHTTPServer)
            if self.headers.get("Host") not in {
                f"127.0.0.1:{self.server.server_port}",
                f"localhost:{self.server.server_port}",
            }:
                self.send_error(403)
                return
            parsed = urlsplit(self.path)
            query = parse_qs(parsed.query)
            try:
                if parsed.path == "/":
                    body = (
                        files("research_agent")
                        .joinpath("static/dashboard.html")
                        .read_bytes()
                    )
                    content_type = "text/html; charset=utf-8"
                else:
                    data: object
                    if parsed.path == "/api/runs":
                        data = {
                            "selected": selected,
                            "runs": sorted(
                                p.name
                                for p in root.iterdir()
                                if p.is_dir()
                                and not p.is_symlink()
                                and (p / "run.json").is_file()
                            )
                            if root.exists()
                            else [],
                        }
                    elif parsed.path in {"/api/state", "/api/events"}:
                        directory = run_directory(
                            root, query.get("run", [selected or ""])[0]
                        )
                        data = (
                            state(directory)
                            if parsed.path == "/api/state"
                            else events(directory, int(query.get("offset", ["0"])[0]))
                        )
                    else:
                        self.send_error(404)
                        return
                    body = json.dumps(data, ensure_ascii=False).encode()
                    content_type = "application/json"
            except (ValueError, OSError):
                self.send_error(400, "Invalid or unavailable run artifact")
                return
            self.send_response(200)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header(
                "Content-Security-Policy",
                "default-src 'self'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; connect-src 'self'; img-src 'none'; frame-ancestors 'none'",
            )
            self.end_headers()
            self.wfile.write(body)

    return Handler


def serve(root: Path, run_id: str | None = None, port: int = 8765) -> None:
    if run_id:
        run_directory(root, run_id)
    with ThreadingHTTPServer(("127.0.0.1", port), handler(root, run_id)) as server:
        print(f"Dashboard: http://127.0.0.1:{server.server_port}", flush=True)
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass
