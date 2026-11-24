"""Dependency-free MCP server (stdio, JSON-RPC 2.0, newline-delimited).

Every action in the ontology becomes an MCP tool. The *actor* is fixed when the
server starts (``nano-onto serve-mcp --actor alice --role employee``): the model
cannot choose its own role, so it cannot talk its way past a rule.

Implements the subset of MCP that tool servers need: ``initialize``,
``tools/list``, ``tools/call`` and ``ping``.
"""

from __future__ import annotations

import json
import sys
from typing import IO, Any, Callable

from . import __version__
from .compile import action_description, action_input_schema
from .engine import ActionError, Actor, Engine

SUPPORTED_PROTOCOLS = ("2025-11-25", "2025-06-18", "2025-03-26", "2024-11-05")

_BUILTINS: dict[str, dict[str, Any]] = {
    "onto_describe": {
        "description": "Describe the ontology: object types, properties, links, roles and actions.",
        "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
    },
    "onto_query": {
        "description": "List objects of a type, optionally filtered by exact property values. You only see what "
        "your role may read: hidden rows are left out and hidden properties show as ***.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "type": {"type": "string", "description": "Object type name"},
                "where": {"type": "object", "description": "Exact-match filters, e.g. {\"status\": \"approved\"}"},
                "limit": {"type": "integer", "default": 50},
            },
            "required": ["type"],
            "additionalProperties": False,
        },
    },
    "onto_get": {
        "description": "Get one object by type and ID, including its outgoing links (subject to your read permissions).",
        "inputSchema": {
            "type": "object",
            "properties": {"type": {"type": "string"}, "id": {"type": "string"}},
            "required": ["type", "id"],
            "additionalProperties": False,
        },
    },
    "onto_explain": {
        "description": "Dry-run an action: shows which rules would fire. Changes nothing.",
        "inputSchema": {
            "type": "object",
            "properties": {"action": {"type": "string"}, "params": {"type": "object"}},
            "required": ["action"],
            "additionalProperties": False,
        },
    },
    "onto_pending_approvals": {
        "description": "List approval requests waiting for the current user's role.",
        "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
    },
    "onto_request_status": {
        "description": "Status of an approval request (pending / executed / denied / rejected / failed / stale) and "
        "who still has to approve. Use it to check whether an action you submitted has actually happened.",
        "inputSchema": {
            "type": "object",
            "properties": {"request_id": {"type": "string"}},
            "required": ["request_id"],
            "additionalProperties": False,
        },
    },
    "onto_approve": {
        "description": "Approve a pending request as the current user. Only the required role can approve, "
        "and requesters cannot approve their own requests.",
        "inputSchema": {
            "type": "object",
            "properties": {"request_id": {"type": "string"}, "note": {"type": "string"}},
            "required": ["request_id"],
            "additionalProperties": False,
        },
    },
    "onto_reject": {
        "description": "Reject a pending request as the current user.",
        "inputSchema": {
            "type": "object",
            "properties": {"request_id": {"type": "string"}, "reason": {"type": "string"}},
            "required": ["request_id"],
            "additionalProperties": False,
        },
    },
}


def describe(engine: Engine, actor: Actor | None = None) -> dict[str, Any]:
    """The ontology as ``actor`` may see it (``None``: everything, with object counts)."""
    onto = engine.onto
    types: dict[str, Any] = {}
    for ot in onto.object_types.values():
        if not engine.can_read(ot, actor):
            continue
        types[ot.name] = {
            "label": ot.label,
            "primary_key": ot.primary_key,
            "properties": {
                p.name: {k: v for k, v in (("type", p.type), ("label", p.label), ("enum", p.enum),
                                           ("hidden", not engine._readable(p, actor))) if v}
                for p in ot.properties.values()
            },
            "links": {n: lt.to_type for n, lt in onto.links_from(ot.name).items()},
        }
        if actor is None or not ot.visible_when:  # a count would reveal rows the actor cannot see
            types[ot.name]["count"] = engine.store.count(ot.name)
    return {
        "name": onto.name,
        "version": onto.version,
        "roles": onto.roles,
        "object_types": types,
        "actions": {
            a.name: {"label": a.label, "allowed_roles": a.allowed_roles, "parameters": list(a.parameters)}
            for a in onto.action_types.values()
        },
    }


class MCPServer:
    def __init__(self, engine: Engine, actor: Actor, on_commit: Callable[[], None] | None = None):
        self.engine = engine
        self.actor = actor
        self.on_commit = on_commit  # e.g. wake the webhook worker

    # ---------------------------------------------------------------- tools
    def list_tools(self) -> list[dict[str, Any]]:
        tools = [{"name": n, **spec} for n, spec in _BUILTINS.items()]
        for action in self.engine.onto.action_types.values():
            tools.append(
                {
                    "name": action.name,
                    "title": action.label or action.name,
                    "description": action_description(action, self.engine.onto),
                    "inputSchema": action_input_schema(action, self.engine.onto),
                }
            )
        return tools

    def call_tool(self, name: str, args: dict[str, Any]) -> tuple[Any, bool]:
        """Return ``(payload, is_error)``."""
        eng = self.engine
        try:
            if name == "onto_describe":
                return describe(eng, self.actor), False
            if name == "onto_query":
                return eng.query(args["type"], args.get("where"), int(args.get("limit", 50)), self.actor), False
            if name == "onto_get":
                obj = eng.get(args["type"], str(args["id"]), self.actor)
                return (obj, False) if obj else ({"error": "not found"}, True)
            if name == "onto_request_status":
                req = eng.request(args["request_id"], self.actor)
                return (req, False) if req else ({"error": "not found"}, True)
            if name == "onto_explain":
                res = eng.explain(args["action"], args.get("params") or {}, self.actor)
                return res.to_dict(), False
            if name == "onto_pending_approvals":
                return eng.pending(self.actor.role), False
            if name == "onto_approve":
                res = eng.approve(args["request_id"], self.actor, args.get("note"))
                self._committed(res)
                return res.to_dict(), False
            if name == "onto_reject":
                res = eng.reject(args["request_id"], self.actor, args.get("reason", ""))
                return res.to_dict(), False
            if name in eng.onto.action_types:
                res = eng.execute(name, args, self.actor)
                self._committed(res)
                return res.to_dict(), res.status == "invalid"
        except ActionError as exc:
            return {"error": str(exc)}, True
        except KeyError as exc:
            return {"error": f"missing argument {exc}"}, True
        except (TypeError, ValueError) as exc:
            return {"error": f"bad argument: {exc}"}, True
        return {"error": f"unknown tool {name!r}"}, True

    def _committed(self, result: Any) -> None:
        if result.webhooks and self.on_commit:
            self.on_commit()

    # -------------------------------------------------------------- JSON-RPC
    def handle(self, message: dict[str, Any]) -> dict[str, Any] | None:
        if not isinstance(message, dict):
            return _error(None, -32600, "invalid request")
        method = message.get("method")
        msg_id = message.get("id")
        if msg_id is None:  # notification
            return None
        params = message.get("params") or {}
        if not isinstance(params, dict):
            return _error(msg_id, -32602, "params must be an object")
        try:
            if method == "initialize":
                requested = params.get("protocolVersion")
                version = requested if requested in SUPPORTED_PROTOCOLS else SUPPORTED_PROTOCOLS[0]
                result: Any = {
                    "protocolVersion": version,
                    "capabilities": {"tools": {"listChanged": False}},
                    "serverInfo": {"name": f"nano-onto-{self.engine.onto.name}", "version": __version__},
                    "instructions": (
                        f"Business actions for the '{self.engine.onto.name}' ontology, running as "
                        f"{self.actor.id} ({self.actor.role}). Rules and approvals are enforced server-side."
                    ),
                }
            elif method == "ping":
                result = {}
            elif method == "tools/list":
                result = {"tools": self.list_tools()}
            elif method == "tools/call":
                arguments = params.get("arguments") or {}
                if not isinstance(arguments, dict):
                    return _error(msg_id, -32602, "arguments must be an object")
                payload, is_error = self.call_tool(str(params.get("name", "")), arguments)
                text = json.dumps(payload, ensure_ascii=False, indent=2, default=str)
                result = {"content": [{"type": "text", "text": text}], "isError": is_error}
                if isinstance(payload, dict):
                    result["structuredContent"] = payload
            else:
                return _error(msg_id, -32601, f"method not found: {method}")
        except Exception as exc:  # noqa: BLE001 - never crash the stdio loop
            return _error(msg_id, -32603, f"internal error: {exc}")
        return {"jsonrpc": "2.0", "id": msg_id, "result": result}

    def serve(self, stdin: IO[str] | None = None, stdout: IO[str] | None = None) -> None:
        stdin = stdin or sys.stdin
        stdout = stdout or sys.stdout
        for line in stdin:
            line = line.strip()
            if not line:
                continue
            try:
                message = json.loads(line)
            except json.JSONDecodeError:
                response: dict[str, Any] | None = _error(None, -32700, "parse error")
            else:
                if isinstance(message, list):  # batch
                    replies = [r for r in (self.handle(m) for m in message) if r]
                    if replies:
                        stdout.write(json.dumps(replies, ensure_ascii=False) + "\n")
                        stdout.flush()
                    continue
                response = self.handle(message)
            if response is not None:
                stdout.write(json.dumps(response, ensure_ascii=False, default=str) + "\n")
                stdout.flush()


def _error(msg_id: Any, code: int, message: str) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": msg_id, "error": {"code": code, "message": message}}
