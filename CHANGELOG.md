# Changelog

## 0.2.0

Roadmap features:

- **Read permissions** -- `readable_by` on object types and properties, `visible_when` row filters;
  applied by `Engine.query` / `Engine.get`, every MCP read tool and the CLI (`--actor/--role`).
- **Stores** -- the same store code runs on SQLite, PostgreSQL (`postgresql://`) and DuckDB (`duckdb://`).
  **Read-through**: `source:` maps object types and links onto existing tables (`open_store(path, onto)`).
- **Webhooks** -- `webhook` effects go through a transactional outbox with retries (`nano-onto deliver`, `outbox`).
- **Versioning and migrations** -- databases are stamped with their ontology; `nano-onto migrate` / `history`.
- **HTTP MCP transport** with per-user bearer tokens (`nano-onto serve-http`, `nano-onto token`).
- **Benchmark harness** in `benchmark/`.

Fixes found while auditing 0.1.0:

- `amount=nan` (or `inf`) passed every comparison and so every rule; non-finite numbers are now rejected.
- A misspelt key (`allowed_role:`) was silently ignored, leaving the action open to every role; unknown
  keys are now load errors, as are non-mapping specs, effects that omit required properties, updates of a
  primary key, reserved aliases, duplicate rule ids, and enum/default values of the wrong type.
- A broken ontology could crash the loader with `KeyError` instead of reporting the problem.
- A fully approved request whose effects failed stayed `pending` forever with nobody able to decide it;
  it is now closed as `failed`.
- Refused approval attempts (self-approval, wrong role) were not audited.
- Rule evaluation and effects were not one transaction; an action is now a single serialized transaction
  that includes its audit entry, so concurrent processes cannot act on stale rule checks.
- `link` effects could point at objects that do not exist; `one_to_one` / `one_to_many` were not enforced
  on the target side.
- Actors with a role the ontology does not define could run actions that had no `allowed_roles`.
- `--where blacklisted=true` compared the string `"true"` with a boolean and matched nothing.
- Expression arithmetic allowed `'x' * 10**9`.

Fixes from an independent review of the 0.2.0 code before release:

- An explicit `null` or `""` for a parameter with a default resolved to `None`, so threshold rules using it
  did not fire; it now takes the default, exactly like an omitted parameter.
- Integers were parsed through `float`: ids above 2**53 addressed the neighbouring row, and spellings such as
  `"01"` / `"1.0"` made the same row look like two objects to rules and links. Integers are parsed exactly
  and every object has one canonical key.
- Actions could reference objects their actor may not read, so `explain` distinguished hidden from missing
  objects and exposed rule outcomes over them. Hidden objects now answer "not found"; dry runs are audited.
- A database error at the start of `approve` (e.g. a lock timeout) let any caller close the request and wipe
  its approvals; a transient error now changes nothing.
- A failed `COMMIT` left the transaction and its write lock open.
- `allowed_roles` was not re-checked when a pending request was finally approved; `migrate` now closes
  requests whose requester may no longer run the action or that wait for a role that no longer exists.
- A running engine kept enforcing its old ontology after another process migrated the database; the stamp
  is now checked on every action and read.
- Revoked HTTP tokens kept working until restart; the tokens file is now re-read when it changes.
- Webhooks followed redirects (re-sending credentials to the new host and reporting success), stored
  secrets from error messages in the outbox and audit log, and two concurrent sweeps could send one event
  twice. Redirects are failures, secrets are scrubbed, deliveries are claimed before sending.
- `x-` keys were stripped from webhook headers and payloads (`x-api-key` was never sent).
- `migrate` reported "up to date" for a reorder-only change while the engine still refused the database;
  `renamed_from` naming a type that still exists silently moved its data.
- `export-skill` wrote a database URL with its password into SKILL.md.
- Smaller: overflow in rule arithmetic produced NaN; requests' existence leaked to uninvolved actors through
  approve/reject errors; generated ids did not fit integer keys; malformed `Origin` headers or deeply nested
  JSON killed an HTTP handler thread; dates accepted trailing garbage; engine calls inside a caller's
  transaction could not be rolled back and are now refused.

Changed:

- nano-onto's tables are now prefixed `onto_` (they can live next to existing tables). A database created
  by 0.1.0 is not picked up; re-run `nano-onto init`.
- Reads go through `Engine.query` / `Engine.get`; `mcp_server.object_with_links` is gone.
- `allowed_roles: []` now means "nobody" rather than "everybody".
- Dry runs (`explain`) are written to the audit log with the event `explain`.
- An action whose object parameter is hidden from the actor is `invalid` ("not found"), not `denied`.
