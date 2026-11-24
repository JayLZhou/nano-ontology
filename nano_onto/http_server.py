"""MCP over HTTP with per-user authentication.

The stdio server (``nano-onto serve-mcp``) fixes one actor per process. This
transport serves many users from one process: every request carries a bearer
token, and the *token* -- not the model -- decides who is acting::

    nano-onto token --tokens tokens.yaml --actor alice --role employee   # prints the token once
    nano-onto serve-http --schema ontology.yaml --db shop.db --tokens tokens.yaml --port 8765

    POST http://127.0.0.1:8765/mcp      Authorization: Bearer <token>

It implements the request/response half of MCP's Streamable HTTP transport:
each POST carries one JSON-RPC message and gets one ``application/json`` reply
(notifications get ``202 Accepted``). There is no server-initiated stream, so
``GET`` returns 405, as the specification allows. Only SHA-256 hashes of tokens
are stored. The server binds to localhost by default and rejects browser
requests from other origins; put TLS in front of it before exposing it.
"""

from __future__ import annotations

import hashlib
import json
import secrets
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlparse

import yaml

from .engine import Actor, Engine
from .mcp_server import SUPPORTED_PROTOCOLS, MCPServer, _error

MAX_BODY = 1_000_000
_LOCAL_HOSTS = {"127.0.0.1", "localhost", "::1"}


def _digest(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def load_tokens(path: str | Path) -> dict[str, Actor]:
    """``{sha256(token): Actor}`` from a tokens file (a YAML list of ``{actor, role, sha256}``)."""
    file = Path(path)
    entries = (yaml.safe_load(file.read_text(encoding="utf-8")) or []) if file.exists() else []
    if not isinstance(entries, list) or not all(isinstance(e, dict) and {"actor", "role", "sha256"} <= set(e)
                                                for e in entries):
        raise ValueError(f"{path}: expected a list of {{actor, role, sha256}} entries")
    return {str(e["sha256"]): Actor(str(e["actor"]), str(e["role"])) for e in entries}


def issue_token(path: str | Path, actor: str, role: str) -> str:
    """Create a token for ``actor``/``role``, store its hash, and return the token (shown only once)."""
    file = Path(path)
    load_tokens(file)  # refuse to append to a malformed file
    entries = (yaml.safe_load(file.read_text(encoding="utf-8")) or []) if file.exists() else []
    token = "nonto_" + secrets.token_urlsafe(32)
    entries.append({"actor": actor, "role": role, "sha256": _digest(token)})
    file.touch(mode=0o600)  # private before anything is written to it
    file.chmod(0o600)
    file.write_text(yaml.safe_dump(entries, allow_unicode=True, sort_keys=False), encoding="utf-8")
    return token


def revoke_tokens(path: str | Path, actor: str) -> int:
    """Remove every token of ``actor``; returns how many were removed."""
    file = Path(path)
    entries = (yaml.safe_load(file.read_text(encoding="utf-8")) or []) if file.exists() else []
    kept = [e for e in entries if str(e.get("actor")) != actor]
    file.write_text(yaml.safe_dump(kept, allow_unicode=True, sort_keys=False), encoding="utf-8")
    return len(entries) - len(kept)


class TokenFile:
    """Bearer tokens -> actors, re-read whenever the file changes (issuing or revoking needs no restart)."""

    def __init__(self, path: str | Path, engine: Engine):
        self.file, self.engine = Path(path), engine
        self._mtime: Any = None
        self._tokens: dict[str, Actor] = {}
        self._lock = threading.Lock()
        for actor in load_tokens(self.file).values():  # fail at start-up, not on the first request
            if engine.onto.roles and actor.role not in engine.onto.roles:
                raise ValueError(f"token for {actor.id!r} has unknown role {actor.role!r}")

    def _current(self) -> dict[str, Actor]:
        with self._lock:
            mtime = self.file.stat().st_mtime_ns if self.file.exists() else None
            if mtime != self._mtime:
                try:
                    self._tokens = load_tokens(self.file)
                except (ValueError, OSError, yaml.YAMLError):
                    self._tokens = {}  # an unreadable file authorises nobody
                self._mtime = mtime
            return self._tokens

    def actor(self, authorization: str | None) -> Actor | None:
        """The actor an ``Authorization`` header stands for, or ``None``."""
        scheme, _, token = (authorization or "").partition(" ")
        actor = self._current().get(_digest(token.strip())) if scheme.lower() == "bearer" and token.strip() else None
        if actor is not None and self.engine.onto.roles and actor.role not in self.engine.onto.roles:
            return None
        return actor


def origin_allowed(origin: str | None, allowed: tuple[str, ...] = ()) -> bool:
    """Browsers send ``Origin``; accept none (not a browser), localhost, or an explicitly allowed one."""
    if not origin:
        return True
    try:
        return origin in allowed or urlparse(origin).hostname in _LOCAL_HOSTS
    except ValueError:
        return False


def make_server(engine: Engine, tokens_path: str | Path, host: str = "127.0.0.1", port: int = 8765,
                allowed_origins: tuple[str, ...] = (), on_commit: Callable[[], None] | None = None) -> ThreadingHTTPServer:
    tokens = TokenFile(tokens_path, engine)

    class Handler(BaseHTTPRequestHandler):
        server_version = "nano-onto"
        protocol_version = "HTTP/1.1"

        def log_message(self, *args: Any) -> None:  # stay quiet; the audit log is the record
            pass

        def _send(self, status: int, payload: Any = None, headers: dict[str, str] | None = None) -> None:
            body = b"" if payload is None else json.dumps(payload, ensure_ascii=False, default=str).encode("utf-8")
            self.send_response(status)
            if body:
                self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            for key, value in (headers or {}).items():
                self.send_header(key, value)
            self.end_headers()
            self.wfile.write(body)

        def _actor(self) -> Actor | None:
            return tokens.actor(self.headers.get("Authorization"))

        def _origin_ok(self) -> bool:
            return origin_allowed(self.headers.get("Origin"), allowed_origins)

        def do_POST(self) -> None:  # noqa: N802
            try:
                length = int(self.headers.get("Content-Length") or 0)
            except ValueError:
                length = -1
            if length < 0 or length > MAX_BODY:
                self.close_connection = True
                return self._send(413, _error(None, -32600, "request too large"))
            raw = self.rfile.read(length)
            if urlparse(self.path).path.rstrip("/") != "/mcp":
                return self._send(404, _error(None, -32601, "not found; POST to /mcp"))
            if not self._origin_ok():
                return self._send(403, _error(None, -32600, "origin not allowed"))
            actor = self._actor()
            if actor is None:
                return self._send(401, _error(None, -32001, "missing or invalid bearer token"),
                                  {"WWW-Authenticate": 'Bearer realm="nano-onto"'})
            version = self.headers.get("MCP-Protocol-Version")
            if version and version not in SUPPORTED_PROTOCOLS:
                return self._send(400, _error(None, -32600, f"unsupported MCP-Protocol-Version {version!r}"))
            try:
                message = json.loads(raw)
            except (ValueError, RecursionError):  # bad JSON, bad encoding, or nesting deep enough to hit the stack
                return self._send(400, _error(None, -32700, "parse error"))
            if not isinstance(message, dict):
                return self._send(400, _error(None, -32600, "expected one JSON-RPC message per request"))
            with engine.store.lock:  # one request at a time on the shared connection
                response = MCPServer(engine, actor, on_commit).handle(message)
            if response is None:
                return self._send(202)
            self._send(200, response)

        def do_GET(self) -> None:  # noqa: N802
            if urlparse(self.path).path == "/healthz":
                return self._send(200, {"status": "ok", "ontology": engine.onto.stamp})
            self._send(405, _error(None, -32601, "this server has no event stream; POST to /mcp"), {"Allow": "POST"})

        do_DELETE = do_PUT = do_GET  # noqa: N815

    server = ThreadingHTTPServer((host, port), Handler)
    server.daemon_threads = True
    return server
