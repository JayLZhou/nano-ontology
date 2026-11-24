# Walkthrough: the complete flow

This is the whole life of an ontology, start to finish: check it, load it, act through it, approve, read with permissions, audit, change a rule under review, and hand it to an agent three different ways.

Everything here is runnable. One script does all of it against a throwaway database:

```bash
python -m pip install -e .
bash examples/walkthrough.sh
```

The sections below follow that script step by step, with its real output. The example is a small procurement company ([`examples/procurement/ontology.yaml`](../examples/procurement/ontology.yaml)): three roles (`employee`, `dept_manager`, `cfo`), two object types (`Supplier`, `PurchaseOrder`), and three actions (`submit_purchase_order`, `cancel_purchase_order`, `blacklist_supplier`).

To type the commands yourself, point the CLI at a schema and a database once:

```bash
export NANO_ONTO_SCHEMA=examples/procurement/ontology.yaml NANO_ONTO_DB=shop.db
```

[中文版](./walkthrough_cn.md)

## Contents

1. [Check and load](#1-check-and-load)
2. [Ask before acting](#2-ask-before-acting)
3. [An allowed action](#3-an-allowed-action)
4. [A denied action](#4-a-denied-action)
5. [An action held for approval](#5-an-action-held-for-approval)
6. [Separation of duties](#6-separation-of-duties)
7. [Approval and execution](#7-approval-and-execution)
8. [Read permissions](#8-read-permissions)
9. [The audit log](#9-the-audit-log)
10. [Changing a rule](#10-changing-a-rule)
11. [An agent over MCP (stdio)](#11-an-agent-over-mcp-stdio)
12. [Many users over HTTP](#12-many-users-over-http)
13. [A Skill](#13-a-skill)
14. [Where to go next](#where-to-go-next)

## 1. Check and load

```console
$ nano-onto validate
ok: procurement v0.1.0 -- 2 object types, 1 link types, 3 actions, 7 rules
$ nano-onto init --seed examples/procurement/seed.json
initialised shop.db: Supplier=3, PurchaseOrder=2, links=2
```

`validate` is strict on purpose. A rule that mentions a property that does not exist, an effect that leaves a required property unset, an unknown role, or a misspelt key all fail here, before any agent can call anything. `init` creates the database, loads the seed objects and links, and stamps the database with the ontology it now follows (that stamp matters in [step 10](#10-changing-a-rule)).

## 2. Ask before acting

A dry run evaluates an action without changing anything, and shows the rule trace:

```console
$ nano-onto explain submit_purchase_order -p title=服务器 -p amount=60000 -p supplier=S-002 --actor alice --role employee
[WAIT] submit_purchase_order: pending_approval (dry run)
  - needs approval by dept_manager (部门经理): 单笔超过 1 万元须部门经理审批
  - needs approval by cfo (财务总监): 单笔超过 5 万元须财务总监审批
  [ ] no_blacklisted_supplier: params.supplier.blacklisted
  [ ] positive_amount: params.amount <= 0
  [x] dept_manager_over_10k: params.amount > 10000 -> needs approval by dept_manager
  [x] cfo_over_50k: params.amount > 50000 -> needs approval by cfo
  [ ] warn_rating_c: params.supplier.rating == 'C'
```

Every rule is listed with whether it fired. Agents get the same thing through the `onto_explain` tool. Dry runs are written to the audit log too, because they evaluate rules over data the caller may not be allowed to see.

## 3. An allowed action

```console
$ nano-onto run submit_purchase_order -p title=文具 -p amount=8000 -p supplier=S-001 --actor alice --role employee
[OK] submit_purchase_order: executed
  created po: PO-0003
```

No rule fired, so the effects ran: a `PurchaseOrder` was created and linked to its supplier. Role check, parameter coercion, rules, effects and the audit entry are one serialized transaction.

## 4. A denied action

```console
$ nano-onto run submit_purchase_order -p title=芯片 -p amount=4800 -p supplier=S-003 --actor alice --role employee
[DENIED] submit_purchase_order: denied
  - denied: 不得向黑名单供应商采购
  - warning: 该供应商评级为 C，请注意交付风险
```

`S-003` is blacklisted; the rule `params.supplier.blacklisted` followed the reference to the supplier object and denied the action. Nothing changed. The exit code is 2, so scripts notice.

## 5. An action held for approval

```console
$ nano-onto run submit_purchase_order -p title=服务器 -p amount=60000 -p supplier=S-002 --actor alice --role employee
[WAIT] submit_purchase_order: pending_approval
  - needs approval by dept_manager (部门经理): 单笔超过 1 万元须部门经理审批
  - needs approval by cfo (财务总监): 单笔超过 5 万元须财务总监审批
  request: REQ-298D1CC6  waiting for: cfo, dept_manager
```

This is the important one: **no order exists yet.** What was stored is an approval request with the parameters, the requester and the roles that must sign. An agent that reports "done" here is wrong, and its tool result says so (`status: pending_approval`).

## 6. Separation of duties

```console
$ nano-onto approve REQ-298D1CC6 --actor alice --role cfo
error: requesters cannot approve their own requests (separation of duties)
```

Alice cannot approve her own request, whatever role she presents. The attempt is recorded in the audit log as `refused`.

## 7. Approval and execution

```console
$ nano-onto approve REQ-298D1CC6 --actor bob --role dept_manager
[WAIT] submit_purchase_order: pending_approval
  - approval recorded; still waiting for: cfo
  request: REQ-298D1CC6  waiting for: cfo
$ nano-onto approve REQ-298D1CC6 --actor carol --role cfo
[OK] submit_purchase_order: executed
  created po: PO-0004
$ nano-onto request REQ-298D1CC6 --json
REQ-298D1CC6 -> executed approved by {'cfo': 'carol', 'dept_manager': 'bob'}
```

When the last approval arrives, the engine does not simply replay the request. It resolves the parameters and evaluates **every rule again against current data**, as the original requester. If the supplier was blacklisted in the meantime the request is closed as `denied`; if a stricter rule now applies, the new role is added to the list; if the action can no longer run at all, the request is closed as `failed` rather than left hanging. `nano-onto request <id>` (or the `onto_request_status` tool) is how a requester finds out what happened.

## 8. Read permissions

The procurement ontology says employees see only their own orders, and only the CFO sees a supplier's bank account:

```yaml
PurchaseOrder:
  visible_when: obj.requester == actor.id or actor.role != 'employee'   # row level
Supplier:
  properties:
    bank_account: {type: string, readable_by: [cfo]}                    # property level
```

```console
$ nano-onto query PurchaseOrder --actor alice --role employee --json
alice sees ['PO-0001', 'PO-0003', 'PO-0004']
$ nano-onto query PurchaseOrder --actor carol --role cfo --json
carol sees ['PO-0001', 'PO-0002', 'PO-0003', 'PO-0004']
$ nano-onto get Supplier S-001 --actor alice --role employee
type: Supplier
id: S-001
properties:
  bank_account: '***'
  blacklisted: false
  name: 联华办公
  rating: A
  supplier_id: S-001
links: {}
$ nano-onto run cancel_purchase_order -p po=PO-0002 --actor alice --role employee
[INVALID] cancel_purchase_order: invalid
  - po: PurchaseOrder 'PO-0002' not found
```

`PO-0002` belongs to Dave. Alice does not see it in queries, and her *actions* cannot reference it either: a hidden object answers exactly like a missing one, so actions cannot be used to probe for what exists. A masked property cannot be used as a query filter for the same reason.

## 9. The audit log

```console
$ nano-onto audit
   1  2026-11-24T20:25:40  alice (employee)       explain  submit_purchase_order      pending_approval
   2  2026-11-24T20:25:40  alice (employee)       execute  submit_purchase_order      executed
   3  2026-11-24T20:25:40  alice (employee)       execute  submit_purchase_order      denied
   4  2026-11-24T20:25:40  alice (employee)       execute  submit_purchase_order      pending_approval REQ-298D1CC6
   5  2026-11-24T20:25:40  alice (cfo)            approve  submit_purchase_order      refused          REQ-298D1CC6
   6  2026-11-24T20:25:40  bob (dept_manager)     approve  submit_purchase_order      recorded         REQ-298D1CC6
   7  2026-11-24T20:25:41  carol (cfo)            approve  submit_purchase_order      recorded         REQ-298D1CC6
   8  2026-11-24T20:25:41  carol (cfo)            approve  submit_purchase_order      executed         REQ-298D1CC6
   9  2026-11-24T20:25:41  alice (employee)       execute  cancel_purchase_order      invalid
```

One row per attempt, including the dry run (1), the denial (3), the refused self-approval (5) and the reference to an order Alice may not see (9). `nano-onto audit --verbose` (or `--json`) adds the parameters, the messages, what was created or updated, and the ontology version in force.

## 10. Changing a rule

Lower the manager-approval threshold from 10,000 to 5,000 in `ontology.yaml` and bump `version` to `0.2.0`. The database was stamped with the old ontology, so nothing runs until the change is reviewed:

```console
$ nano-onto run submit_purchase_order -p title=打印机 -p amount=7000 -p supplier=S-001 --actor alice --role employee
error: this database follows ontology procurement@0.1.0, but this process holds procurement@0.2.0 with different content. If the ontology file changed, review and apply it with `nano-onto migrate`; if the database was migrated, restart this process.
$ nano-onto migrate --dry-run
procurement@0.1.0 -> procurement@0.2.0
schema changes:
  ~ action_types.submit_purchase_order.rules.dept_manager_over_10k.when: "params.amount > 10000" -> "params.amount > 5000"
  ~ version: "0.1.0" -> "0.2.0"
dry run: nothing was applied.
$ nano-onto migrate --by carol
procurement@0.1.0 -> procurement@0.2.0
schema changes:
  ~ action_types.submit_purchase_order.rules.dept_manager_over_10k.when: "params.amount > 10000" -> "params.amount > 5000"
  ~ version: "0.1.0" -> "0.2.0"
applied.
$ nano-onto run submit_purchase_order -p title=打印机 -p amount=7000 -p supplier=S-001 --actor alice --role employee
[WAIT] submit_purchase_order: pending_approval
  - needs approval by dept_manager (部门经理): 单笔超过 1 万元须部门经理审批
  request: REQ-AC609D91  waiting for: dept_manager
$ nano-onto history --json
1 initialised procurement@0.1.0
2 migrated procurement@0.2.0
```

A migration is more than a stamp. When types or properties change it backfills defaults, follows `renamed_from`, re-types and drops stored values, and closes pending requests that no longer make sense; anything it cannot do safely is reported as a problem and nothing is applied. The check runs on every action and read, so a long-running server stops the moment another process migrates the database. See [Changing the ontology](./ontology-reference.md#changing-the-ontology).

## 11. An agent over MCP (stdio)

```bash
nano-onto serve-mcp --actor alice --role employee
```

This is a dependency-free MCP server on stdin/stdout. Each action is a tool whose description lists its rules; reads go through `onto_query` and `onto_get` with Alice's permissions. The script sends it three JSON-RPC lines by hand:

```console
server: nano-onto-procurement
alice calls blacklist_supplier -> denied ["role 'employee' may not run blacklist_supplier; allowed: cfo"]
```

The actor is fixed when the server starts. Nothing the model says in the conversation can change who it is acting as. Client configuration for Claude Desktop, Claude Code and Cursor is in [Connect an agent](./agents.md).

## 12. Many users over HTTP

```bash
TOKEN=$(nano-onto token --tokens tokens.yaml --actor carol --role cfo | tail -1)
nano-onto serve-http --tokens tokens.yaml --port 18765
```

```console
no token   -> HTTP 401
carol/cfo  -> executed
```

One process, many users: each request carries a bearer token and the token decides the actor. Only hashes of tokens are stored, `nano-onto token --revoke` takes effect without a restart, and the server binds to localhost unless told otherwise.

## 13. A Skill

```console
$ nano-onto export-skill --out skill
wrote skill/SKILL.md
---
name: procurement-ontology
description: "Read and change procurement business data (Supplier (供应商), PurchaseOrder (采购单)) only through ontology actions that enforce permissions, business rules and approvals. ..."
---
```

The generated `SKILL.md` teaches a Skills-capable agent to act only through `nano-onto run`, with every action, its parameters and its rules listed. A Skill passes `--actor/--role` itself, so use MCP when the agent must not be able to choose its identity.

## Where to go next

- Write your own ontology: [Ontology reference](./ontology-reference.md)
- Leave your data where it is: [Read-through](./ontology-reference.md#existing-tables-read-through), and `python examples/erp_readthrough/demo.py`
- Notify other systems after an action commits: [Webhooks](./ontology-reference.md#webhooks)
- Wire up a real client: [Connect an agent](./agents.md)
- Every command and flag: [CLI reference](./cli-reference.md)
- What exactly is guaranteed, and what is not: [Architecture](./architecture.md)
