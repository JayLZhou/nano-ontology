"""A web console for people: browse the ontology and its data, run actions, decide approvals, read the audit log.

    nano-onto console --tokens tokens.yaml                       # each bearer token is one user
    nano-onto console --demo alice:employee --demo carol:cfo     # local demo: switch users in the page

It is the same engine the agents use, so the console is bound by the same rules: a user sees
only what their role may read, an action is refused, held or executed exactly as it would be for
an agent, and everything they try is in the audit log. The page is one static HTML file with no
external resources; everything else is the small JSON API below.

``--demo`` trusts the browser to say who it is. It exists for trying things out and for
screenshots, and only binds to localhost.
"""

from __future__ import annotations

import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable
from urllib.parse import parse_qs, urlparse

from .engine import ActionError, Actor, Engine, VersionError
from .http_server import _LOCAL_HOSTS, MAX_BODY, TokenFile, origin_allowed
from .mcp_server import describe

PAGE = Path(__file__).with_name("console.html")


class _NoSuchEndpoint(Exception):
    pass


def ontology_view(engine: Engine, actor: Actor) -> dict[str, Any]:
    """Everything the page needs to draw the ontology, as ``actor`` may see it."""
    onto = engine.onto
    out = describe(engine, actor)
    visible = out["object_types"]
    for name, spec in visible.items():
        ot = onto.object_types[name]
        spec["row_filter"] = ot.visible_when
        spec["description"] = ot.description
    out["link_types"] = [
        {"name": lt.name, "from": lt.from_type, "to": lt.to_type, "cardinality": lt.cardinality, "label": lt.label}
        for lt in onto.link_types.values() if lt.from_type in visible and lt.to_type in visible
    ]
    out["actions"] = {
        a.name: {
            "label": a.label,
            "description": a.description,
            "allowed_roles": a.allowed_roles,
            "can_run": a.allowed_roles is None or actor.role in a.allowed_roles,
            "parameters": [
                {"name": p.name, "type": p.type, "object_type": p.object_type, "required": p.required,
                 "enum": p.enum, "default": p.default, "description": p.description}
                for p in a.parameters.values()
            ],
            "rules": [
                {"id": r.id, "when": r.when, "outcome": r.outcome, "role": r.role, "message": r.message, "source": r.source}
                for r in a.rules
            ],
            "effects": [e.kind + " " + e.target for e in a.effects],
        }
        for a in onto.action_types.values()
    }
    return out


def make_console(engine: Engine, tokens_path: str | Path | None = None, demo_users: list[Actor] | None = None,
                 host: str = "127.0.0.1", port: int = 8787, auditors: tuple[str, ...] = (),
                 on_commit: Callable[[], None] | None = None) -> ThreadingHTTPServer:
    """``auditors`` are the roles that may read the whole audit log; everyone else sees their own entries."""
    if bool(tokens_path) == bool(demo_users):
        raise ValueError("the console needs either a tokens file or demo users (not both)")
    if demo_users and host not in _LOCAL_HOSTS:
        raise ValueError("demo users are only allowed when the console binds to localhost")
    for role in [*auditors, *(a.role for a in demo_users or [])]:
        if engine.onto.roles and role not in engine.onto.roles:
            raise ValueError(f"unknown role {role!r}; this ontology defines: {', '.join(engine.onto.roles)}")
    tokens = TokenFile(tokens_path, engine) if tokens_path else None
    demo = {a.id: a for a in demo_users or []}
    onto = engine.onto

    def session() -> dict[str, Any]:
        return {"ontology": onto.name, "version": onto.version, "mode": "demo" if demo else "token",
                "users": [{"id": a.id, "role": a.role, "label": onto.role_label(a.role)} for a in demo.values()]}

    def get(actor: Actor, path: str, query: dict[str, str]) -> Any:
        if path == "/api/me":
            return {"id": actor.id, "role": actor.role, "label": onto.role_label(actor.role), "auditor": actor.role in auditors}
        if path == "/api/ontology":
            return ontology_view(engine, actor)
        if path == "/api/objects":
            return engine.query(query["type"], None, int(query.get("limit", 200)), actor)
        if path == "/api/object":
            found = engine.get(query["type"], query["id"], actor)
            if found is None:
                raise ActionError("not found")
            return found
        if path == "/api/approvals":
            mine = [r for r in engine.store.list_approvals(None) if r["actor"]["id"] == actor.id]
            return {"inbox": [r for r in engine.pending(actor.role) if r["actor"]["id"] != actor.id],
                    "own_waiting": [r["id"] for r in engine.pending(actor.role) if r["actor"]["id"] == actor.id],
                    "mine": mine[::-1][:50]}
        if path == "/api/audit":
            rows = engine.store.audit_log(int(query.get("limit", 200)))
            scope = "all" if actor.role in auditors else "own"
            return {"scope": scope, "rows": [r for r in rows if scope == "all" or r["actor"] == actor.id][::-1]}
        raise _NoSuchEndpoint(path)

    def post(actor: Actor, path: str, body: dict[str, Any]) -> Any:
        if path in ("/api/explain", "/api/run"):
            params = body.get("params") or {}
            result = engine.execute(str(body.get("action", "")), params, actor, dry_run=path == "/api/explain")
        elif path == "/api/approve":
            result = engine.approve(str(body.get("request_id", "")), actor, body.get("note") or None)
        elif path == "/api/reject":
            result = engine.reject(str(body.get("request_id", "")), actor, str(body.get("reason") or ""))
        else:
            raise _NoSuchEndpoint(path)
        if result.webhooks and on_commit:
            on_commit()
        return result.to_dict()

    class Handler(BaseHTTPRequestHandler):
        server_version = "nano-onto-console"
        protocol_version = "HTTP/1.1"

        def log_message(self, *args: Any) -> None:  # the audit log is the record
            pass

        def _send(self, status: int, payload: Any, content_type: str = "application/json") -> None:
            body = payload if isinstance(payload, bytes) else json.dumps(payload, ensure_ascii=False, default=str).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", content_type + "; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Content-Security-Policy",
                             "default-src 'self' 'unsafe-inline'; img-src 'self' data:; frame-ancestors 'none'")
            self.end_headers()
            self.wfile.write(body)

        def _actor(self) -> Actor | None:
            if demo:
                return demo.get(self.headers.get("X-Demo-User", ""))
            return tokens.actor(self.headers.get("Authorization")) if tokens else None

        def _api(self, call: Callable[[Actor], Any]) -> None:
            if not origin_allowed(self.headers.get("Origin")):
                return self._send(403, {"error": "origin not allowed"})
            actor = self._actor()
            if actor is None:
                return self._send(401, {"error": "sign in first" if not demo else "choose a user"})
            try:
                with engine.store.lock:  # one request at a time on the shared connection
                    payload = call(actor)
            except _NoSuchEndpoint:
                return self._send(404, {"error": "not found"})
            except VersionError as exc:
                return self._send(409, {"error": str(exc)})
            except (ActionError, KeyError, TypeError, ValueError) as exc:
                return self._send(400, {"error": str(exc.args[0]) if isinstance(exc, KeyError) and exc.args else str(exc)})
            self._send(200, payload)

        def do_GET(self) -> None:  # noqa: N802
            url = urlparse(self.path)
            if url.path in ("/", "/index.html"):
                return self._send(200, PAGE.read_bytes(), "text/html")
            if url.path == "/api/session":
                return self._send(200, session())
            if url.path == "/healthz":
                return self._send(200, {"status": "ok", "ontology": onto.stamp})
            query = {k: v[0] for k, v in parse_qs(url.query).items()}
            self._api(lambda actor: get(actor, url.path, query))

        def do_POST(self) -> None:  # noqa: N802
            try:
                length = int(self.headers.get("Content-Length") or 0)
            except ValueError:
                length = -1
            if length < 0 or length > MAX_BODY:
                self.close_connection = True
                return self._send(413, {"error": "request too large"})
            raw = self.rfile.read(length)
            if "json" not in self.headers.get("Content-Type", ""):  # a plain HTML form cannot send this
                return self._send(415, {"error": "send application/json"})
            try:
                body = json.loads(raw or b"{}")
            except (ValueError, RecursionError):
                return self._send(400, {"error": "invalid JSON"})
            if not isinstance(body, dict):
                return self._send(400, {"error": "expected a JSON object"})
            self._api(lambda actor: post(actor, urlparse(self.path).path, body))

    server = ThreadingHTTPServer((host, port), Handler)
    server.daemon_threads = True
    return server
