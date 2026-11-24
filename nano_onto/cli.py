"""``nano-onto`` command line.

Schema and database default to ``$NANO_ONTO_SCHEMA`` / ``$NANO_ONTO_DB``, then to
``ontology.yaml`` / ``nano_onto.db`` in the current directory. ``--db`` also takes
``postgresql://...`` and ``duckdb://file`` URLs.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path
from typing import Any

import yaml

from .compile import render_skill
from .engine import ActionError, ActionResult, Actor, Engine
from .mcp_server import MCPServer, describe
from .schema import Ontology, SchemaError
from .store import StoreError, open_store

_STATUS_ICON = {
    "executed": "OK",
    "pending_approval": "WAIT",
    "denied": "DENIED",
    "invalid": "INVALID",
    "rejected": "REJECTED",
}


def _add_common(p: argparse.ArgumentParser, db: bool = True, actor: str | None = None) -> None:
    """``actor`` is ``"required"`` (the command acts as someone) or ``"optional"`` (it only reads)."""
    p.add_argument("--schema", default=os.environ.get("NANO_ONTO_SCHEMA", "ontology.yaml"))
    if db:
        p.add_argument("--db", default=os.environ.get("NANO_ONTO_DB", "nano_onto.db"))
    if actor:
        for flag, env in (("--actor", "NANO_ONTO_ACTOR"), ("--role", "NANO_ONTO_ROLE")):
            p.add_argument(flag, default=os.environ.get(env), required=actor == "required" and env not in os.environ)
    p.add_argument("--json", action="store_true", help="machine-readable output")


def _parse_params(pairs: list[str] | None, raw_json: str | None) -> dict[str, Any]:
    try:
        params = json.loads(raw_json) if raw_json else {}
    except json.JSONDecodeError as exc:
        raise SystemExit(f"--params-json is not valid JSON: {exc}") from None
    if not isinstance(params, dict):
        raise SystemExit("--params-json must be a JSON object")
    return {**params, **_pairs(pairs, "--param")}


def _pairs(pairs: list[str] | None, flag: str) -> dict[str, str]:
    out = {}
    for pair in pairs or []:
        if "=" not in pair:
            raise SystemExit(f"{flag} expects key=value, got {pair!r}")
        key, value = pair.split("=", 1)
        out[key.strip()] = value
    return out


def _ontology(args) -> Ontology:
    try:
        return Ontology.load(args.schema)
    except FileNotFoundError:
        raise SystemExit(f"ontology not found: {args.schema} (use --schema or $NANO_ONTO_SCHEMA)") from None
    except SchemaError as exc:
        raise SystemExit(str(exc)) from None


def _reader(args) -> Actor | None:
    """Who a read-only command acts as; ``None`` means the operator (sees everything)."""
    if bool(args.actor) != bool(args.role):
        raise SystemExit("pass both --actor and --role, or neither")
    return Actor(args.actor, args.role) if args.actor else None


def _print_result(res: ActionResult, as_json: bool, deliveries: list[dict[str, Any]] | None = None) -> None:
    if as_json:
        print(json.dumps({**res.to_dict(), "deliveries": deliveries or []}, ensure_ascii=False, indent=2))
        return
    tag = _STATUS_ICON.get(res.status, res.status.upper())
    print(f"[{tag}] {res.action}: {res.status}" + (" (dry run)" if res.dry_run else ""))
    for msg in res.messages:
        print(f"  - {msg}")
    if res.request_id and res.status == "pending_approval":
        print(f"  request: {res.request_id}  waiting for: {', '.join(res.required_approvals)}")
    for alias, pk in res.created.items():
        print(f"  created {alias}: {pk}")
    for item in res.updated:
        print(f"  updated {item}")
    for item in deliveries or []:
        print(f"  webhook {item['webhook']} ({item['id']}): {item['status']}"
              + (f" -- {item['error']}; will retry with `nano-onto deliver`" if item["error"] else ""))


def _dump(data: Any, as_json: bool) -> None:
    if as_json:
        print(json.dumps(data, ensure_ascii=False, indent=2, default=str))
    else:
        print(yaml.safe_dump(data, allow_unicode=True, sort_keys=False).rstrip())


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="nano-onto", description="Executable ontology for AI agents")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("validate", help="check an ontology file")
    _add_common(p, db=False)

    p = sub.add_parser("init", help="create the database and load seed data")
    _add_common(p)
    p.add_argument("--seed", help="seed JSON: {objects: {Type: [...]}, links: [...]}")

    p = sub.add_parser("describe", help="show types, links, roles and actions")
    _add_common(p, actor="optional")

    p = sub.add_parser("query", help="list objects of a type (as --actor/--role sees them, if given)")
    _add_common(p, actor="optional")
    p.add_argument("type")
    p.add_argument("--where", action="append", help="key=value (exact match)")
    p.add_argument("--limit", type=int, default=50)

    p = sub.add_parser("get", help="show one object and its links")
    _add_common(p, actor="optional")
    p.add_argument("type")
    p.add_argument("id")

    for name, helptext in (("run", "execute an action"), ("explain", "dry-run an action")):
        p = sub.add_parser(name, help=helptext)
        _add_common(p, actor="required")
        p.add_argument("action")
        p.add_argument("--param", "-p", action="append", help="key=value")
        p.add_argument("--params-json", help="parameters as a JSON object")

    p = sub.add_parser("pending", help="list pending approval requests")
    _add_common(p)
    p.add_argument("--role", help="only requests waiting for this role")

    p = sub.add_parser("request", help="show one approval request and its status")
    _add_common(p)
    p.add_argument("request_id")

    for name in ("approve", "reject"):
        p = sub.add_parser(name, help=f"{name} a pending request")
        _add_common(p, actor="required")
        p.add_argument("request_id")
        p.add_argument("--note", default="")

    p = sub.add_parser("audit", help="show the audit log (one line per attempt)")
    _add_common(p)
    p.add_argument("--limit", type=int, default=30)
    p.add_argument("--verbose", "-v", action="store_true", help="include parameters, messages and details")

    p = sub.add_parser("migrate", help="review and apply a changed ontology to the database")
    _add_common(p)
    p.add_argument("--dry-run", action="store_true", help="show the plan, change nothing")
    p.add_argument("--drop-removed", action="store_true", help="delete data of removed types and links")
    p.add_argument("--by", default=os.environ.get("NANO_ONTO_ACTOR", "admin"), help="who applies it (for the audit log)")

    p = sub.add_parser("history", help="ontology versions this database has followed")
    _add_common(p)

    p = sub.add_parser("deliver", help="send queued webhooks that are due")
    _add_common(p)
    p.add_argument("--watch", type=float, metavar="SECONDS", help="keep running, checking every SECONDS")

    p = sub.add_parser("outbox", help="list webhook deliveries")
    _add_common(p)
    p.add_argument("--status", choices=["pending", "delivered", "dead"])
    p.add_argument("--retry", metavar="ID", help="put a dead delivery back in the queue")

    p = sub.add_parser("serve-mcp", help="serve actions as MCP tools over stdio, as one fixed actor")
    _add_common(p, actor="required")

    p = sub.add_parser("serve-http", help="serve MCP over HTTP; each bearer token is one actor")
    _add_common(p)
    p.add_argument("--tokens", required=True, help="tokens file written by `nano-onto token`")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=8765)
    p.add_argument("--allow-origin", action="append", default=[], help="extra browser origin to accept")

    p = sub.add_parser("token", help="issue (or revoke) a bearer token for serve-http")
    p.add_argument("--tokens", required=True, help="tokens file (stores only hashes)")
    p.add_argument("--actor", required=True)
    p.add_argument("--role", help="role the token acts with")
    p.add_argument("--revoke", action="store_true", help="remove every token of --actor")

    p = sub.add_parser("export-skill", help="write a SKILL.md for Claude Skills / nanobot")
    _add_common(p)
    p.add_argument("--out", default="skill", help="output directory")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        return _main(args)
    except (ActionError, StoreError, RuntimeError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


def _main(args) -> int:
    if args.cmd == "token":
        from .http_server import issue_token, revoke_tokens

        if args.revoke:
            print(f"revoked {revoke_tokens(args.tokens, args.actor)} token(s) of {args.actor}")
        elif not args.role:
            raise SystemExit("--role is required to issue a token")
        else:
            token = issue_token(args.tokens, args.actor, args.role)
            print(f"token for {args.actor} ({args.role}) -- shown once, only its hash is stored:\n{token}")
        return 0

    onto = _ontology(args)
    if args.cmd == "validate":
        print(
            f"ok: {onto.name} v{onto.version} -- {len(onto.object_types)} object types, "
            f"{len(onto.link_types)} link types, {len(onto.action_types)} actions, "
            f"{sum(len(a.rules) for a in onto.action_types.values())} rules"
        )
        return 0

    store = open_store(args.db, onto)
    if args.cmd == "migrate":
        from .migrate import migrate

        plan = migrate(store, onto, actor=args.by, dry_run=args.dry_run, drop_removed=args.drop_removed)
        if args.json:
            _dump(plan.to_dict(), True)
        else:
            print(f"{plan.old or '(new database)'} -> {plan.new}")
            for title, lines in (("schema changes", plan.changes), ("data", plan.steps), ("PROBLEMS", plan.problems)):
                if lines:
                    print(f"{title}:")
                    print("\n".join(f"  {line}" for line in lines))
            if plan.problems:
                print("nothing was applied.")
            elif not plan.changes and plan.old:
                print("up to date.")
            else:
                print("applied." if plan.applied else "dry run: nothing was applied.")
        return 1 if plan.problems else 0

    engine = Engine(onto, store)

    if args.cmd == "init":
        counts = {}
        if args.seed:
            with open(args.seed, encoding="utf-8") as fh:
                counts = engine.load_seed(json.load(fh))
        print(f"initialised {args.db}: " + (", ".join(f"{k}={v}" for k, v in counts.items()) or "empty"))
    elif args.cmd == "describe":
        _dump(describe(engine, _reader(args)), args.json)
    elif args.cmd == "query":
        where = _pairs(args.where, "--where")
        _dump(engine.query(args.type, where or None, args.limit, _reader(args)), args.json)
    elif args.cmd == "get":
        obj = engine.get(args.type, args.id, _reader(args))
        if obj is None:
            print(f"{args.type} {args.id!r} not found", file=sys.stderr)
            return 1
        _dump(obj, args.json)
    elif args.cmd in ("run", "explain", "approve", "reject"):
        actor = Actor(args.actor, args.role)
        if args.cmd == "approve":
            res = engine.approve(args.request_id, actor, args.note)
        elif args.cmd == "reject":
            res = engine.reject(args.request_id, actor, args.note)
        else:
            params = _parse_params(args.param, args.params_json)
            res = engine.execute(args.action, params, actor, dry_run=args.cmd == "explain")
        deliveries = []
        if res.webhooks:
            from .webhooks import deliver

            deliveries = [d for d in deliver(engine) if d["id"] in res.webhooks]
        _print_result(res, args.json, deliveries)
        if args.cmd == "explain" and not args.json:
            for t in res.trace:
                mark = "x" if t["fired"] else " "
                print(f"  [{mark}] {t['rule']}: {t['when']}" + (f" -> {t['outcome']}" if t["outcome"] else ""))
        return 0 if res.status in ("executed", "pending_approval", "rejected") else 2
    elif args.cmd == "pending":
        _dump(engine.pending(args.role), args.json)
    elif args.cmd == "request":
        req = engine.request(args.request_id)
        if req is None:
            print(f"no approval request {args.request_id!r}", file=sys.stderr)
            return 1
        _dump(req, args.json)
    elif args.cmd == "audit":
        rows = engine.store.audit_log(args.limit)
        if args.json or args.verbose:
            _dump(rows, args.json)
        else:  # one line per attempt; --verbose / --json show parameters, messages and rule outcomes
            for row in rows:
                who = f"{row['actor']} ({row['role']})"
                ref = row["detail"].get("request_id") or row["detail"].get("delivery") or ""
                print(f"{row['seq']:>4}  {row['ts'][:19]}  {who:<22} {row['event']:<8} "
                      f"{row['action'] or '-':<26} {row['outcome']:<17}{ref}".rstrip())
    elif args.cmd == "history":
        _dump(engine.store.history(), args.json)
    elif args.cmd == "deliver":
        from .webhooks import deliver

        while True:
            report = deliver(engine)
            if report or not args.watch:
                _dump(report, args.json)
            if not args.watch:
                return 0 if all(r["status"] == "delivered" for r in report) else 2
            time.sleep(args.watch)
    elif args.cmd == "outbox":
        if args.retry:
            from .webhooks import retry

            print("queued again" if retry(engine, args.retry) else f"no dead delivery {args.retry!r}")
        else:
            _dump(engine.store.outbox(args.status), args.json)
    elif args.cmd in ("serve-mcp", "serve-http"):
        wake = None
        if onto.webhooks:
            from .webhooks import Worker

            wake = Worker(engine).wake
        if args.cmd == "serve-mcp":
            actor = Actor(args.actor, args.role)
            engine._check_role(actor)
            MCPServer(engine, actor, wake).serve()
        else:
            from .http_server import make_server

            server = make_server(engine, args.tokens, args.host, args.port, tuple(args.allow_origin), wake)
            print(f"nano-onto MCP ({onto.stamp}) on http://{args.host}:{server.server_address[1]}/mcp", file=sys.stderr)
            try:
                server.serve_forever()
            except KeyboardInterrupt:
                pass
    elif args.cmd == "export-skill":
        out = Path(args.out)
        out.mkdir(parents=True, exist_ok=True)
        (out / "SKILL.md").write_text(render_skill(engine.onto, args.schema, args.db), encoding="utf-8")
        print(f"wrote {out / 'SKILL.md'}")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
