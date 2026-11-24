# CLI reference

```
nano-onto <command> [--schema FILE] [--db DATABASE] [--json] ...
```

`--schema` defaults to `$NANO_ONTO_SCHEMA`, then `ontology.yaml`. `--db` defaults to `$NANO_ONTO_DB`, then `nano_onto.db`; it takes a SQLite file, `postgresql://user:pass@host/db` or `duckdb://file.duckdb`. `--actor` and `--role` default to `$NANO_ONTO_ACTOR` and `$NANO_ONTO_ROLE`. `--json` prints machine-readable output.

## Commands

| Command | What it does |
|---|---|
| `validate` | Check an ontology file. Needs no database. |
| `init [--seed FILE]` | Create the database, stamp it with the ontology, optionally load seed data (`{"objects": {Type: [...]}, "links": [{type, from, to}]}`). |
| `describe [--actor A --role R]` | Types, properties, links, roles and actions. |
| `query TYPE [--where k=v ...] [--limit N] [--actor A --role R]` | List objects with exact-match filters. |
| `get TYPE ID [--actor A --role R]` | One object and its links. |
| `explain ACTION -p k=v ... --actor A --role R` | Dry-run an action and print the rule trace. |
| `run ACTION -p k=v ... --actor A --role R` | Execute an action. `--params-json '{...}'` is an alternative to `-p`. |
| `pending [--role R]` | Approval requests still waiting (for a role). |
| `request ID` | One approval request and its status. |
| `approve ID --actor A --role R [--note TEXT]` | Record an approval; executes the action when it is the last one. |
| `reject ID --actor A --role R [--note TEXT]` | Close a request as rejected. |
| `audit [--limit N] [--verbose]` | The audit log, one line per attempt; `--verbose` / `--json` add parameters and messages. |
| `migrate [--dry-run] [--drop-removed] [--by NAME]` | Review and apply a changed ontology. |
| `history` | Every ontology version this database has followed. |
| `deliver [--watch SECONDS]` | Send queued webhooks that are due. |
| `outbox [--status pending\|delivered\|dead] [--retry ID]` | List webhook deliveries; re-queue a dead one. |
| `serve-mcp --actor A --role R` | MCP server on stdin/stdout, as one fixed actor. |
| `serve-http --tokens FILE [--host H] [--port P] [--allow-origin O]` | MCP server over HTTP; each bearer token is one actor. |
| `console (--tokens FILE \| --demo USER:ROLE ...) [--auditor ROLE] [--host H] [--port P]` | Web console for people. With `--tokens` each bearer token is one user; `--demo` users need no sign-in and only work on localhost. `--auditor` roles may read the whole audit log. |
| `token --tokens FILE --actor A [--role R] [--revoke]` | Issue a token (printed once) or revoke an actor's tokens. Used by `serve-http` and `console`. |
| `export-skill [--out DIR]` | Write a `SKILL.md` for Skills-capable agents. |

## Reading as someone

`describe`, `query` and `get` show everything when run without `--actor/--role`: that is the operator's view. With both flags they show what that actor may see -- hidden rows are left out and hidden properties print as `***`.

## Exit codes

| Code | Meaning |
|---|---|
| 0 | The command worked. For `run` / `approve`: the action was `executed` or is `pending_approval`; for `reject`: rejected. |
| 1 | Not found (`get`, `request`), an invalid ontology, or a migration with problems. |
| 2 | The action was `denied` or `invalid`, an approval was refused, the database follows a different ontology, or a database error occurred. |

## Environment

| Variable | Used for |
|---|---|
| `NANO_ONTO_SCHEMA`, `NANO_ONTO_DB` | Defaults for `--schema` and `--db` |
| `NANO_ONTO_ACTOR`, `NANO_ONTO_ROLE` | Defaults for `--actor` and `--role` |
| `NANO_ONTO_TODAY` | Overrides `today()` in expressions (tests, replays) |
| `${ANY_NAME}` in a webhook `url`, `headers` or `secret_env` | Resolved when the webhook is delivered |
