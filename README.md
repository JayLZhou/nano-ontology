<div align="center">

# nano-onto

**The ultra-lightweight Palantir-style Ontology for AI agents.**
Objects, links, *actions*, roles, approvals, read permissions and audit -- in a ~2,500-line Python core you can read in an afternoon.

[中文](README_CN.md) · MIT · Python ≥ 3.10 · one dependency (PyYAML)

</div>

---

📏 Core size: run `bash core_lines.sh` to verify the line count yourself (core ≈ 2,500 lines; four optional modules add ≈ 700).

Objects and links say what exists. *Actions* say what may happen, under which rules, with whose approval -- and nano-onto enforces that outside the model.

Most open-source "ontology" and knowledge-graph projects stop at the **semantic layer**: entities, properties, relations. That is enough to *answer questions*. It is not enough to let an agent *change things* -- submit an order, cancel a contract, blacklist a supplier -- because the business rules that govern those changes live in PDFs and people's heads, and an LLM asked to "remember the rules" will eventually forget one.

nano-onto is the missing **action layer**:

| | Semantic layer (KG / GraphRAG) | **nano-onto** |
|---|---|---|
| What exists | ✅ objects, links | ✅ objects, links |
| What may happen | ❌ | ✅ typed **actions** with parameters and effects |
| Who may do it | ❌ | ✅ roles per action |
| Under which rules | prompt text | ✅ **deterministic rules**, checked by code, not by the model |
| Approvals | ❌ | ✅ multi-role approval, separation of duties |
| Who may see what | ❌ | ✅ row- and property-level read permissions, masking |
| Accountability | ❌ | ✅ audit log of every attempt, rule trace, source quote per rule |
| Where the data lives | a copy in a graph store | ✅ its own SQLite / PostgreSQL / DuckDB store, **or your existing tables, in place** |
| Side effects | ❌ | ✅ webhooks queued in the action's transaction, retried until delivered |
| Change control | ❌ | ✅ versioned ontology, reviewed migrations |
| Agent interface | retrieval | ✅ **MCP server** (stdio and HTTP), CLI, Claude/nanobot **Skill** |

## 30-second demo

```bash
pip install -e .
python examples/procurement/demo.py
```

```
== 3. Alice 买 6 万元服务器（需部门经理 + 财务总监）
   status: pending_approval
   - needs approval by dept_manager (部门经理): 单笔超过 1 万元须部门经理审批
   - needs approval by cfo (财务总监): 单笔超过 5 万元须财务总监审批
== 4. Alice 想自己批
   blocked: requesters cannot approve their own requests (separation of duties)
== 6. Carol（财务总监）批准 -> 真正执行
   status: executed
   created: {'po': 'PO-0004'}
== 9. 同一个查询，不同的人看到不同的结果（行级 + 字段级权限）
   alice (employee): 采购单 ['PO-0001', 'PO-0003', 'PO-0004']  S-001 收款账号 ***
   carol (cfo): 采购单 ['PO-0001', 'PO-0002', 'PO-0003', 'PO-0004']  S-001 收款账号 6222 0210 0100 3344
```

## An action, in YAML

```yaml
action_types:
  submit_purchase_order:
    allowed_roles: [employee, dept_manager, cfo]
    parameters:
      title:    {type: string}
      amount:   {type: number}
      supplier: {type: object_ref, object_type: Supplier}
    rules:
      - id: no_blacklisted_supplier
        when: params.supplier.blacklisted          # follows the object graph
        then: deny
        message: 不得向黑名单供应商采购
      - id: cfo_over_50k
        when: params.amount > 50000
        then: {require_approval: cfo}
        source: {doc: 采购管理制度.md, quote: "单笔采购金额超过50000元的，须经财务总监审批。"}
    effects:
      - create: PurchaseOrder
        as: po
        set: {title: params.title, amount: params.amount, requester: actor.id, status: "'approved'"}
      - link: supplier
        from: po
        to: params.supplier
```

Rules are written in a small, **safe** expression language (parsed with `ast`, whitelisted, interpreted -- never `eval`'d). Rules can follow links (`params.po.supplier.rating`), compare against the actor (`params.po.requester != actor.id`), and use `today()` for rules that start on a date.

The whole ontology is checked at load time, strictly: a rule that mentions a property that does not exist, an effect that forgets a required property, an unknown role, or a misspelt key such as `allowed_role:` (which would otherwise leave the action open to everyone) fails before any agent can call it.

## Who may see what

Read permissions are part of the ontology and apply to every read an agent makes:

```yaml
object_types:
  PurchaseOrder:
    visible_when: obj.requester == actor.id or actor.role != 'employee'   # row level
  Supplier:
    properties:
      bank_account: {type: string, readable_by: [cfo]}                    # property level: others see ***
  Salary:
    readable_by: [cfo]                                                    # type level
```

Hidden rows look missing -- to reads *and* to actions, which cannot reference an object their actor may not see. Hidden properties come back as `***` and cannot be used as filters (an equality filter would reveal the value), and links to hidden objects are left out. Rules still read every property -- the engine evaluates them, not the reader.

## Give it to an agent

**MCP over stdio** (Claude Desktop, Claude Code, Cursor, nanobot, any MCP client) -- zero extra dependencies:

```bash
nano-onto init --schema examples/procurement/ontology.yaml --db shop.db --seed examples/procurement/seed.json
nano-onto serve-mcp --schema examples/procurement/ontology.yaml --db shop.db --actor alice --role employee
```

```json
{ "mcpServers": { "procurement": {
    "command": "nano-onto",
    "args": ["serve-mcp", "--schema", "/abs/ontology.yaml", "--db", "/abs/shop.db", "--actor", "alice", "--role", "employee"] } } }
```

Every action becomes a tool whose description lists its rules; plus `onto_describe`, `onto_query`, `onto_get`, `onto_explain` (dry run), `onto_request_status`, `onto_pending_approvals`, `onto_approve`, `onto_reject`. **The actor is fixed when the server starts** -- the model cannot pick its own role to get past a rule.

**MCP over HTTP, one token per user** -- one process serves many users, and the bearer token (not the model) decides who is acting:

```bash
nano-onto token --tokens tokens.yaml --actor alice --role employee      # prints the token once; only its hash is stored
nano-onto serve-http --schema ontology.yaml --db shop.db --tokens tokens.yaml --port 8765
# POST http://127.0.0.1:8765/mcp   Authorization: Bearer nonto_...
```

This is the request/response half of MCP's Streamable HTTP transport (JSON replies, no server-sent stream). It binds to localhost and rejects other browser origins by default; put TLS in front of it before exposing it.

**Skill** (Claude Skills / nanobot): `nano-onto export-skill --out skills/procurement` writes a `SKILL.md` that teaches the agent to act only through `nano-onto run`. A Skill passes `--actor/--role` itself, so use MCP when the agent must not be able to choose its identity.

**CLI**:

```bash
nano-onto explain submit_purchase_order -p title=服务器 -p amount=60000 -p supplier=S-002 --actor alice --role employee
nano-onto run     submit_purchase_order -p title=服务器 -p amount=60000 -p supplier=S-002 --actor alice --role employee
nano-onto pending --role cfo
nano-onto approve REQ-1A2B3C4D --actor carol --role cfo
nano-onto request REQ-1A2B3C4D        # pending / executed / denied / rejected / failed / stale
nano-onto query PurchaseOrder --actor alice --role employee     # what alice may see
nano-onto audit
```

**Python**:

```python
from nano_onto import Engine, Ontology, Actor, open_store
onto = Ontology.load("ontology.yaml")
engine = Engine(onto, open_store("shop.db", onto))
res = engine.execute("submit_purchase_order", {"title": "服务器", "amount": 60000, "supplier": "S-002"},
                     Actor("alice", "employee"))
res.status            # executed | pending_approval | denied | invalid
res.trace             # which rules fired, with their source quotes
engine.query("PurchaseOrder", actor=Actor("alice", "employee"))   # reads, with permissions applied
```

## Use the database you already have

`--db` (or `open_store`) takes a SQLite file, `postgresql://user:pass@host/db` (`pip install 'nano-onto[postgres]'`) or `duckdb://file.duckdb` (`pip install 'nano-onto[duckdb]'`). nano-onto's own tables are all prefixed `onto_`.

Better: **do not copy your data at all.** Map object types and links onto the tables that already hold them, and actions read and write those tables directly, in the same transaction as the audit entry:

```yaml
object_types:
  Supplier:
    source: {table: suppliers, columns: {supplier_id: id, name: supplier_name}}   # property -> column
link_types:
  supplier:
    from: PurchaseOrder
    to: Supplier
    source: {column: supplier_id}                                   # foreign-key column on purchase_orders
  tags:
    from: PurchaseOrder
    to: Tag
    cardinality: many_to_many
    source: {table: po_tags, from_column: po_no, to_column: tag}    # join table
```

`python examples/erp_readthrough/demo.py` runs this against a pre-existing database: only the changed columns are written, unmapped columns are never touched, and the database's own constraints still apply (a failed `CHECK` rolls the whole action back and is audited as `invalid`).

## Side effects: webhooks

```yaml
webhooks:
  erp:
    url: ${ERP_HOOK_URL}                 # ${ENV_VAR}s keep endpoints and secrets out of the YAML
    secret_env: ERP_HOOK_SECRET          # optional HMAC-SHA256 signature (X-Nano-Onto-Signature)
    max_attempts: 6
action_types:
  submit_purchase_order:
    effects:
      # ... create / link ...
      - webhook: erp
        event: purchase_order.created
        payload: {po_id: po.po_id, amount: params.amount}
```

The delivery is written to an outbox table **in the action's transaction**, so it exists exactly when the action committed -- never for a denied, pending or rolled-back action. It is then POSTed; failures are retried with exponential backoff (30 s, 1 min, 2 min ... 1 h) and marked `dead` after `max_attempts`. The MCP servers deliver in the background; `nano-onto run` tries once and `nano-onto deliver [--watch 30]` drains the queue; `nano-onto outbox` lists it. Delivery is at-least-once: de-duplicate on the `X-Nano-Onto-Delivery` header.

## Changing the ontology

The database is stamped with the ontology it follows. When the file changes, the engine refuses to act until the change has been reviewed and applied:

```bash
nano-onto migrate --dry-run     # schema diff, and what happens to the stored data
nano-onto migrate --by carol    # apply it in one transaction; recorded in the audit log
nano-onto history               # every ontology version this database has followed
```

A migration backfills defaults, renames (`renamed_from: old_name` on a type, property or link), re-types and drops stored properties, and closes pending approval requests whose action or parameters no longer exist. Anything it cannot do safely -- a value that does not fit its new type, a required property with nothing to put in it, data of a removed type (unless `--drop-removed`) -- is reported as a problem and **nothing** is applied. Pending requests survive rule changes, because rules are re-checked when the last approval arrives.

## Where do ontologies come from?

Writing YAML by hand is fine for a demo. For real documents, use the sister project **[Ontology-Anything](../ontology-anything)**: it reads policies, SOPs, contracts and spreadsheets, proposes object types, actions and rules *with verbatim source quotes*, flags conflicts between documents, and emits an nano-onto ontology after a human approves it. Keys starting with `x-` are ignored everywhere in the YAML, so such tools can attach review metadata.

## Semantics in one paragraph

`execute` checks the role, validates and coerces parameters (resolving object references), evaluates every rule, and then: any `deny` → **denied**; any `require_approval` the actor does not already hold → **pending_approval** (an approval request is stored; nothing changes yet); otherwise the effects run → **executed**. All of that happens in **one serialized transaction** together with the audit entry: the state the rules saw is the state the effects change, and any failure rolls everything back and is audited as **invalid**. When the last approval arrives, the rules are re-checked against current data before the effects run; a fully approved request that can no longer run is closed as `failed`, never left hanging. Every attempt -- denials, refused approvals, failed effects -- goes to the audit log. A missing value is `None`, and an ordering comparison with `None` is false: write `x == None or x > 3` where a missing value should trip the rule.

## Benchmark

`benchmark/` measures what the action layer buys: the same 27 requests (allowed, needs-approval, forbidden, forbidden-under-pressure, and reads of hidden data) are given to an agent with **plain database tools and the rules in its prompt**, and to the same agent with **nano-onto's tools**. Expected outcomes are not written by hand -- a reference engine computes them.

```bash
python -m benchmark.bench --base-url http://localhost:8000/v1 --model <model>     # any OpenAI-compatible endpoint
python -m benchmark.bench --agent scripted:compliant                                # no model needed
```

First result (Qwen3-8B, 81 episodes per arm): with the rules only in its prompt the model broke a rule in **43%** of episodes -- and in **all 18** where the user pushed ("the CFO already agreed", "I'll take the blame"); with nano-onto, **0%**, while task success rose from 42% to 96%. One small model on one small domain; see [benchmark/README.md](benchmark/README.md) for the metrics, the full table and the caveats.

## Limits worth knowing

- PostgreSQL runs one action at a time per database (an advisory lock), SQLite one writer at a time; DuckDB allows a single process. Fine for agent workloads, not for high write concurrency.
- Row filters (`visible_when`) are evaluated in Python after fetching the type's rows; they are not pushed down to SQL.
- Read-through needs single-column primary keys and column names that are plain identifiers.
- The HTTP transport has static bearer tokens and no event stream -- no OAuth, no server-initiated messages.
- An action can only reference objects its actor may read (a hidden object answers "not found", like a missing one), but rules do read masked *properties*. An actor sees which rules fired on their own request, so a rule over a masked property reveals that one fact per attempt; dry runs are written to the audit log for that reason.

## Roadmap

- [x] Postgres / DuckDB stores; read-through to existing databases instead of copying
- [x] Row- and property-level permissions; masking in `onto_query`
- [x] Webhooks / side-effect actions (call an API after commit, with retries)
- [x] Ontology versioning and migrations
- [x] HTTP MCP transport and per-user auth
- [x] Benchmark harness: task success and rule-violation rate with vs. without nano-onto
- [ ] Benchmark numbers across more models (one published so far)
- [ ] Push row filters down to SQL; composite keys in read-through
- [ ] OAuth for the HTTP transport; server-sent events

## Tests

```bash
pip install -e ".[dev]" && pytest
```

The suite runs every backend it can find: SQLite always, DuckDB if installed, PostgreSQL against `$NANO_ONTO_TEST_PG` (or a throwaway embedded server if the `pgserver` package is installed).

MIT License.
