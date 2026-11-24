# Ontology reference

An ontology is one YAML file. This page lists everything that can go in it. The two complete examples are [`examples/procurement/ontology.yaml`](../examples/procurement/ontology.yaml) (roles, approvals, read permissions) and [`examples/erp_readthrough/ontology.yaml`](../examples/erp_readthrough/ontology.yaml) (existing tables, webhooks).

The file is checked strictly when it loads (`nano-onto validate`): unknown keys, unknown roles, references to properties or links that do not exist, effects that leave a required property unset, and values of the wrong type are all errors. The only keys that are ignored are those starting with `x-`, which you can use anywhere for your own annotations.

- [Top level](#top-level)
- [Roles](#roles)
- [Object types](#object-types)
- [Link types](#link-types)
- [Action types](#action-types): [parameters](#parameters) · [rules](#rules) · [effects](#effects)
- [Expressions](#expressions)
- [Read permissions](#read-permissions)
- [Existing tables (read-through)](#existing-tables-read-through)
- [Webhooks](#webhooks)
- [Changing the ontology](#changing-the-ontology)

## Top level

```yaml
name: procurement        # used in the MCP server name, the audit log and the version stamp
version: 0.1.0           # bump it when you change the file
description: ...         # optional
roles: {...}
object_types: {...}
link_types: {...}
action_types: {...}
webhooks: {...}          # optional
```

## Roles

```yaml
roles:
  employee:     {label: 员工}
  dept_manager: {label: 部门经理, description: ...}
  cfo:          {label: 财务总监}
```

If roles are declared, an actor whose role is not in the list is refused everywhere. Roles are flat: there is no inheritance, so list every role that may run an action.

## Object types

```yaml
object_types:
  PurchaseOrder:
    label: 采购单
    description: ...
    primary_key: po_id          # defaults to the first property
    id_prefix: PO-              # generated ids look like PO-0001; integer keys get the next free number
    properties:
      po_id:     {type: string, required: true}
      title:     {type: string, required: true, label: 标题}
      amount:    {type: number, required: true}
      status:    {type: string, enum: [approved, paid, cancelled], default: approved}
      requester: string         # shorthand for {type: string}
```

| Property key | Meaning |
|---|---|
| `type` | `string`, `number`, `integer`, `boolean` or `date` (ISO `YYYY-MM-DD`). Default `string`. |
| `required` | A value must be present when an object is created; it cannot be set to null later. |
| `enum` | The allowed values. |
| `default` | Used when an object is created without a value. |
| `label`, `description` | Shown to agents in `onto_describe` and exported Skills. |
| `readable_by` | Roles that may read this property; see [Read permissions](#read-permissions). |
| `renamed_from` | The property's previous name; see [Changing the ontology](#changing-the-ontology). |

Object-type keys beyond those shown: `readable_by` and `visible_when` ([Read permissions](#read-permissions)), `source` ([read-through](#existing-tables-read-through)) and `renamed_from`.

Values are coerced on the way in: `"8000"` becomes `8000`, `"true"` / `"是"` become `True`. Numbers must be finite; integers are parsed exactly.

## Link types

```yaml
link_types:
  supplier:
    from: PurchaseOrder
    to: Supplier
    cardinality: many_to_one     # default; also one_to_one, one_to_many, many_to_many
    label: 供应商
```

A link is followed in expressions by its name, from the `from` side: `params.po.supplier.rating`. Cardinality is enforced when links are written: a new `many_to_one` link replaces the previous one from the same object, and `one_to_one` / `one_to_many` also free the target. A link cannot share a name with a property of its `from` type.

## Action types

```yaml
action_types:
  submit_purchase_order:
    label: 提交采购单
    description: 创建一张采购单。金额较大时会先进入审批。
    allowed_roles: [employee, dept_manager, cfo]     # omit to allow every role; [] allows nobody
    parameters: {...}
    rules: [...]
    effects: [...]
```

Action names become MCP tool names, so they must match `[A-Za-z_][A-Za-z0-9_-]*` and may not start with `onto_`.

### Parameters

```yaml
parameters:
  title:    {type: string, description: 采购内容}
  amount:   {type: number}
  supplier: {type: object_ref, object_type: Supplier}
  note:     {type: string, required: false}
  quantity: {type: integer, default: 1}
  po:       PurchaseOrder            # shorthand for an object_ref
```

Parameters are required unless they have a `default` or say `required: false`. An explicit `null` or empty string means "not given" and takes the default. Unknown parameters are refused.

An `object_ref` is passed as the object's id and arrives in expressions as the object itself. The object must exist **and be visible to the actor**; otherwise the action is `invalid` with "not found".

### Rules

```yaml
rules:
  - id: no_blacklisted_supplier
    when: params.supplier.blacklisted
    then: deny
    message: 不得向黑名单供应商采购
  - id: cfo_over_50k
    when: params.amount > 50000
    then: {require_approval: cfo}
    message: 单笔超过 5 万元须财务总监审批
    source: {doc: 采购管理制度.md, quote: "单笔采购金额超过50000元的，须经财务总监审批。"}
  - id: warn_rating_c
    when: params.supplier.rating == 'C'
    then: warn
    message: 该供应商评级为 C，请注意交付风险
```

Every rule is evaluated; `when` is an [expression](#expressions) over `params` and `actor`.

| `then` | Effect when the rule fires |
|---|---|
| `deny` | The action is **denied**. |
| `{require_approval: <role>}` | The action is held until someone with that role approves, unless the actor already holds the role. |
| `warn` | The message is returned; the action proceeds. |

`message` is what the agent and the user see. `source` is free-form provenance (a document and a verbatim quote, typically) returned in the rule trace so a refusal can cite its origin.

Outcome of an action: any `deny` → `denied`; otherwise any unmet `require_approval` → `pending_approval`; otherwise the effects run → `executed`. Bad parameters, failed effects and rule-evaluation errors → `invalid`, with everything rolled back.

### Effects

Effects run in order, in one transaction. String values are expressions; anything else is a literal (so a literal string needs inner quotes: `status: "'approved'"`).

```yaml
effects:
  - create: PurchaseOrder        # the id is generated unless `set` provides the primary key
    as: po                       # name the new object for later effects
    set: {title: params.title, amount: params.amount, requester: actor.id, status: "'approved'"}
  - link: supplier               # both ends must exist
    from: po
    to: params.supplier
  - update: params.po            # an expression that resolves to an object
    set: {status: "'cancelled'"}
  - unlink: supplier
    from: params.po
    to: params.po.supplier
  - webhook: erp                 # see Webhooks
    event: purchase_order.created
    payload: {po_id: po.po_id, amount: params.amount}
```

A `create` must set every required property that has no default; an `update` cannot change the primary key. Both are checked at load time.

## Expressions

Rules, `visible_when` filters and effect values are written in a small expression language that looks like Python. It is parsed with `ast`, checked against a whitelist and interpreted; it is never passed to `eval`, and attribute access only ever reaches ontology data.

| | |
|---|---|
| Names | `params.<name>`, `actor.id`, `actor.role`, effect aliases (`po`), and `obj` in `visible_when` |
| Members | properties and to-one links: `params.po.supplier.rating`; `.id` is the primary key |
| Operators | `and or not`, `== != < <= > >=`, `in`, `not in`, `+ - * / // %`, `x if cond else y` |
| Literals | numbers, strings, `True False None`, lists |
| Functions | `today()` (ISO date; override with `$NANO_ONTO_TODAY`), `len abs min max lower upper` |

Things worth knowing:

- **Missing values are `None`.** Arithmetic with `None` gives `None`, and an ordering comparison with `None` is false, so `params.po.supplier.rating > 3` does not fire when there is no supplier. Where a missing value should trip the rule, say so: `params.po.supplier == None or params.po.supplier.rating > 3`.
- Dates are ISO strings, so they compare correctly: `today() >= '2026-01-01'`.
- Two references to the same object are equal: `params.keep == params.drop`.
- A to-many link gives a list: use `len(params.order.items)` or `x in ...`.
- Every name and member is checked when the ontology loads, including `x['member']` forms.

## Read permissions

```yaml
object_types:
  Salary:
    readable_by: [cfo]                                                   # type level
  PurchaseOrder:
    visible_when: obj.requester == actor.id or actor.role != 'employee'  # row level
  Supplier:
    properties:
      bank_account: {type: string, readable_by: [cfo]}                   # property level
```

They apply to every read an agent makes (`onto_query`, `onto_get`, `onto_describe`, and the CLI when `--actor/--role` is given):

- A type the role may not read cannot be queried, and is left out of `onto_describe`.
- A row hidden by `visible_when` looks missing. `visible_when` is an [expression](#expressions) over `obj` and `actor`; if it fails to evaluate, the row is hidden.
- A hidden property comes back as `***` and cannot be used as a filter (an equality filter would reveal the value).
- Links to objects the actor may not see are left out.
- **Actions follow the same rule for their parameters:** an `object_ref` to a hidden object answers "not found", exactly like a missing one.

Rules themselves read every property; the engine evaluates them, not the reader. An actor sees which rules fired on their own request, so a rule over a masked property reveals that one fact per attempt. Dry runs are audited for that reason.

From Python, `engine.query(..., actor=None)` and `engine.get(..., actor=None)` are the trusted, unrestricted view.

## Existing tables (read-through)

By default objects live in nano-onto's own tables. With a `source`, an object type lives in a table you already have, and a link is a foreign-key column or a join table. Nothing is copied: actions read and write those tables directly, in the same transaction as the audit entry.

```yaml
object_types:
  Supplier:
    primary_key: supplier_id
    source:
      table: suppliers
      columns: {supplier_id: id, name: supplier_name}    # property -> column; same name if omitted
    properties:
      supplier_id:  {type: integer, required: true}
      name:         {type: string, required: true}
      credit_limit: {type: number}
      active:       {type: boolean, default: true}

link_types:
  supplier:
    from: PurchaseOrder
    to: Supplier
    source: {column: supplier_id}                         # foreign-key column on PurchaseOrder's table
  tags:
    from: PurchaseOrder
    to: Tag
    cardinality: many_to_many
    source: {table: po_tags, from_column: po_no, to_column: tag}    # join table
```

Open the store with the ontology so it knows the mapping -- the CLI does this for you:

```python
from nano_onto import Engine, Ontology, open_store
onto = Ontology.load("ontology.yaml")
engine = Engine(onto, open_store("postgresql://user:pass@host/erp", onto))
```

- `--db` / `open_store` take a SQLite file, `postgresql://...` (`pip install 'nano-onto[postgres]'`) or `duckdb://file.duckdb` (`pip install 'nano-onto[duckdb]'`).
- Only mapped columns are read; an update writes only the columns that changed; unmapped columns are never touched.
- Column values are coerced to the declared property types (`1` → `True`, a `DATE` → `"2026-03-01"`, `NUMERIC` → number). A value that does not fit is a clear error naming the column.
- The database's own constraints still apply. A failed `CHECK`, `NOT NULL` or foreign key rolls the whole action back and is audited as `invalid`.
- Missing tables or columns fail when the store opens, not in the middle of an action.
- Mapped and unmapped types mix freely. nano-onto's own bookkeeping (`onto_objects`, `onto_links`, `onto_approvals`, `onto_audit`, `onto_outbox`, ...) is created in the same database, all prefixed `onto_`.

Limits: single-column primary keys; table and column names must be plain identifiers (not reserved words); a `NOT NULL` foreign-key column cannot be filled by a separate `link` effect after `create`.

Run `python examples/erp_readthrough/demo.py` to see it against a pre-existing database.

## Webhooks

```yaml
webhooks:
  erp:
    url: ${ERP_HOOK_URL}                      # or a literal https://... URL
    headers: {Authorization: "Bearer ${ERP_TOKEN}"}
    secret_env: ERP_HOOK_SECRET               # optional: HMAC-SHA256 signature of the body
    max_attempts: 6                           # default 6
    timeout: 10                               # seconds, default 10

action_types:
  submit_purchase_order:
    effects:
      # ... create / link ...
      - webhook: erp
        event: purchase_order.created
        payload: {po_id: po.po_id, amount: params.amount}
```

`${VAR}` is replaced from the environment at delivery time, so endpoints and secrets stay out of the YAML.

The delivery is written to an outbox table **in the action's transaction**: it exists exactly when the action committed, never for a denied, pending or rolled-back action. It is then sent as:

```
POST <url>
Content-Type: application/json
X-Nano-Onto-Event: purchase_order.created
X-Nano-Onto-Delivery: EVT-3AB5EA5DACA9                 # de-duplicate on this
X-Nano-Onto-Signature: sha256=<hmac of the body>      # if secret_env is set

{"id": "EVT-...", "event": "...", "action": "submit_purchase_order",
 "actor": {"id": "alice", "role": "employee"}, "ontology": "procurement@0.1.0",
 "payload": {"po_id": "PO-0003", "amount": 8000}}
```

A non-2xx answer (redirects included: they are not followed) or a network error is retried with exponential backoff -- 30 s, 1 min, 2 min, ... capped at 1 h -- and marked `dead` after `max_attempts`. The MCP servers deliver in the background; `nano-onto run` tries once; `nano-onto deliver [--watch 30]` drains the queue; `nano-onto outbox [--status dead] [--retry ID]` inspects it. Delivery is at-least-once. Every attempt is in the audit log, with secrets scrubbed from error messages.

## Changing the ontology

A database is stamped with the ontology it follows (name, version, a content fingerprint and a full copy). When the file changes, every command except `validate` and `migrate` refuses to run -- and so does a server that is already running, the moment another process migrates the database.

```bash
nano-onto migrate --dry-run     # the schema diff, and what will happen to stored data
nano-onto migrate --by carol    # apply it in one transaction; recorded in history and the audit log
nano-onto history               # every ontology this database has followed
```

What a migration does to stored data:

| Change | What happens |
|---|---|
| Rule, message, label or role changes | Recorded; no data changes. Pending requests survive, because rules are re-checked at approval. |
| Property added | Backfilled with its `default`. Required with no default → **problem**. |
| Property removed | Dropped from stored objects. |
| Property type or enum changed | Every stored value is re-coerced; a value that does not fit → **problem**. |
| Property, type or link renamed | Declare `renamed_from: <old name>` on the new one and the data follows. |
| Type or link removed | **Problem** if data exists, unless `--drop-removed` deletes it. |
| Cardinality tightened | **Problem** if existing links violate it. |
| Pending approval requests | Closed as `stale` if their action was removed, their parameters no longer fit, the requester's role may no longer run it, or they wait for a role that no longer exists. |

If there is any problem, **nothing** is applied. Types mapped onto existing tables are never rewritten -- that data is not nano-onto's to migrate. `x-` annotations do not count as a change.
