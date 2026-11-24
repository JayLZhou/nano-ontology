<picture>
  <source media="(prefers-color-scheme: dark)" srcset="./images/readme-cover-dark.svg">
  <img alt="nano-onto: executable ontology for AI agents" src="./images/readme-cover-light.svg">
</picture>

<div align="center">
  <p>
    <a href="./README.md">English</a> |
    <a href="./README_CN.md">简体中文</a>
  </p>
  <p>
    <a href="https://github.com/JayLZhou/nano-ontology"><img src="https://img.shields.io/github/stars/JayLZhou/nano-ontology?style=flat&logo=github" alt="GitHub stars"></a>
    <a href="https://github.com/JayLZhou/nano-ontology/actions/workflows/ci.yml"><img src="https://github.com/JayLZhou/nano-ontology/actions/workflows/ci.yml/badge.svg?branch=main" alt="Test suite"></a>
    <a href="./pyproject.toml"><img src="https://img.shields.io/badge/python-%3E%3D3.10-blue" alt="Python 3.10 or newer"></a>
    <a href="./pyproject.toml"><img src="https://img.shields.io/badge/dependencies-PyYAML%20only-brightgreen" alt="One dependency: PyYAML"></a>
    <a href="./docs/agents.md"><img src="https://img.shields.io/badge/MCP-stdio%20%2B%20HTTP-8A2BE2" alt="MCP over stdio and HTTP"></a>
    <a href="./LICENSE"><img src="https://img.shields.io/badge/license-MIT-green" alt="MIT License"></a>
  </p>
  <p>
    <a href="./docs/walkthrough.md">Walkthrough</a> ·
    <a href="./docs/ontology-reference.md">Ontology reference</a> ·
    <a href="./docs/agents.md">Connect an agent</a> ·
    <a href="./benchmark/README.md">Benchmark</a> ·
    <a href="./CHANGELOG.md">Changelog</a>
  </p>
</div>

# nano-onto

🧭 **nano-onto** is a lightweight, open-source, Palantir-style ontology for AI agents, written in Python. You describe your business in one YAML file — what exists, what may happen, under which rules, with whose approval, and who may see what — and nano-onto enforces it outside the model, for any agent that speaks MCP.

## 💡 Why nano-onto

- **The action layer, not just the semantic layer:** Knowledge graphs tell an agent what exists. nano-onto also defines what it may *do* — typed actions with parameters, rules, approvals and effects — so an agent can change business data without being trusted to remember the policy.
- **Rules the model cannot talk its way past:** Rules are deterministic expressions checked by code. With the rules only in its prompt, a model in our benchmark broke one in 43% of episodes, and in every episode where the user pushed; behind nano-onto: 0%.
- **Approvals, permissions and audit built in:** Multi-role approval with separation of duties, row- and property-level read permissions, and an audit log of every attempt — including the refused ones, with the rule that refused them.
- **Works with the agents and databases you have:** One MCP server (stdio or HTTP) for Claude Desktop, Claude Code, Cursor, nanobot or your own agent. SQLite by default, PostgreSQL and DuckDB supported, or map the ontology onto your existing tables and leave the data where it is.
- **Small enough to read:** A ~2,500-line core with one dependency (PyYAML). Run `bash core_lines.sh` to count it yourself.

## Start Here

| What you want to do | Where to go |
|---|---|
| See the whole flow run in two minutes | [Quick Start](#-quick-start), then `bash examples/walkthrough.sh` |
| Understand each step of that flow | [Walkthrough](./docs/walkthrough.md) |
| Write an ontology for your own domain | [Ontology reference](./docs/ontology-reference.md) |
| Give it to Claude Desktop, Claude Code, Cursor or your own agent | [Connect an agent](./docs/agents.md) |
| Put it in front of a database that already exists | [Read-through](./docs/ontology-reference.md#existing-tables-read-through) |
| Look up a command | [CLI reference](./docs/cli-reference.md) |
| Know how it works and where its limits are | [Architecture](./docs/architecture.md) |
| Measure what it buys you | [Benchmark](./benchmark/README.md) |

## 📦 Install

Requires **Python 3.10 or newer**. The only dependency is PyYAML.

```bash
git clone https://github.com/JayLZhou/nano-ontology.git
cd nano-ontology
python -m pip install -e .
```

<details>
<summary>Optional extras: PostgreSQL, DuckDB, development tools</summary>

```bash
python -m pip install -e ".[postgres]"   # postgresql:// stores (psycopg)
python -m pip install -e ".[duckdb]"     # duckdb:// stores
python -m pip install -e ".[dev]"        # pytest, ruff, duckdb
```

</details>

## 🚀 Quick Start

```bash
python examples/procurement/demo.py
```

That runs a small procurement company through its rules in memory. To drive it yourself, the way an agent would:

**Your first three steps:**

1. Create a database from the example ontology and its seed data.

   ```bash
   export NANO_ONTO_SCHEMA=examples/procurement/ontology.yaml NANO_ONTO_DB=shop.db
   nano-onto init --seed examples/procurement/seed.json
   ```

2. Ask for something the rules do not simply allow — an employee ordering 60,000 worth of servers.

   ```bash
   nano-onto run submit_purchase_order -p title=服务器 -p amount=60000 -p supplier=S-002 --actor alice --role employee
   ```

   The order is **not** created. It is held as a request that the department manager and the CFO must both approve.

3. Give the same database to an agent. The actor is fixed when the server starts, so the model cannot pick a role.

   ```bash
   nano-onto serve-mcp --actor alice --role employee
   ```

Then run the complete flow — dry runs, approvals, read permissions, the audit log, a reviewed rule change, MCP over stdio and HTTP, a Skill export — in one go:

```bash
bash examples/walkthrough.sh
```

<details>
<summary>Connect Claude Desktop, Claude Code or Cursor</summary>

Claude Desktop (`claude_desktop_config.json`) and Cursor (`.cursor/mcp.json`):

```json
{ "mcpServers": { "procurement": {
    "command": "nano-onto",
    "args": ["serve-mcp", "--schema", "/abs/path/ontology.yaml", "--db", "/abs/path/shop.db",
             "--actor", "alice", "--role", "employee"] } } }
```

Claude Code:

```bash
claude mcp add procurement -- nano-onto serve-mcp --schema /abs/path/ontology.yaml --db /abs/path/shop.db --actor alice --role employee
```

Every action becomes a tool whose description lists its rules, next to `onto_describe`, `onto_query`, `onto_get`, `onto_explain`, `onto_request_status`, `onto_pending_approvals`, `onto_approve` and `onto_reject`. See [Connect an agent](./docs/agents.md).

</details>

<details>
<summary>Serve many users over HTTP, one token each</summary>

```bash
nano-onto token --tokens tokens.yaml --actor alice --role employee      # prints the token once; only its hash is stored
nano-onto serve-http --tokens tokens.yaml --port 8765
# POST http://127.0.0.1:8765/mcp      Authorization: Bearer nonto_...
```

The token, not the model, decides who is acting. Revoking a token takes effect immediately. See [Connect an agent](./docs/agents.md#mcp-over-http).

</details>

<details>
<summary>Use it from Python</summary>

```python
from nano_onto import Actor, Engine, Ontology, open_store

onto = Ontology.load("examples/procurement/ontology.yaml")
engine = Engine(onto, open_store("shop.db", onto))

res = engine.execute("submit_purchase_order", {"title": "服务器", "amount": 60000, "supplier": "S-002"},
                     Actor("alice", "employee"))
res.status        # executed | pending_approval | denied | invalid
res.trace         # which rules fired, with their source quotes
engine.query("PurchaseOrder", actor=Actor("alice", "employee"))   # reads, with permissions applied
```

</details>

If nano-onto is useful to you, a star on GitHub is the simplest way to support the project.

## 🎬 See It Work

Everything below is real output: recorded model episodes and terminal sessions, regenerated by [`scripts/readme_assets.py`](./scripts/readme_assets.py).

<p align="center">
  <picture>
    <source media="(prefers-color-scheme: dark)" srcset="images/same-request-dark.svg">
    <img src="images/same-request-light.svg" alt="The same request handled by the same model with database tools and with nano-onto tools" width="100%">
  </picture>
</p>

### Rules the model cannot talk its way past

The same 27 requests go to the same model twice: once with plain database tools and the policy in its prompt, once with nano-onto's tools. Prompt-only rules held for ordinary requests and collapsed under pressure ("the CFO already agreed", "I'll take the blame"); enforced rules did not move.

<p align="center">
  <picture>
    <source media="(prefers-color-scheme: dark)" srcset="images/benchmark-dark.svg">
    <img src="images/benchmark-light.svg" alt="Benchmark: rule violations 43% with prompt-only rules, 0% with nano-onto; task success 42% versus 96%" width="100%">
  </picture>
</p>

One small model on one small domain, 81 episodes per arm — read it as a direction, not a leaderboard.

→ [Benchmark: method, numbers, caveats](./benchmark/README.md)

### Approvals with separation of duties

A dry run shows which rules would fire and why. A request above the thresholds waits for every required role, the requester cannot approve it under any role, and the rules are checked again against current data when the last approval arrives.

<p align="center">
  <img src="images/terminal-approvals.svg" alt="Terminal: explain, run, a refused self-approval, then two approvals that execute the order" width="900">
</p>

→ [Walkthrough, steps 2-7](./docs/walkthrough.md#2-ask-before-acting)

### Everyone sees only what they may

Read permissions live in the ontology and apply to every read: hidden rows look missing, hidden properties come back as `***` and cannot be used as filters, and an action cannot reference an object its actor may not see.

<p align="center">
  <img src="images/terminal-permissions.svg" alt="Terminal: an employee sees only her own orders and a masked bank account; the CFO sees the account" width="900">
</p>

→ [Read permissions](./docs/ontology-reference.md#read-permissions)

### Every attempt is on the record

Executed, held, denied, refused, failed, even dry runs: each attempt is one audit row, written in the same transaction as the change it describes.

<p align="center">
  <img src="images/terminal-audit.svg" alt="Terminal: the audit log of the session above" width="900">
</p>

→ [What one action does](./docs/architecture.md#what-one-action-does)

### Change the rules under review

The database is stamped with the ontology it follows. Edit a rule and nothing runs until the change has been reviewed and applied — and a server that is already running stops instead of enforcing the old rules.

<p align="center">
  <img src="images/terminal-migrate.svg" alt="Terminal: a changed rule is refused, reviewed with migrate --dry-run, applied, and then enforced" width="900">
</p>

→ [Changing the ontology](./docs/ontology-reference.md#changing-the-ontology)

### Govern the database you already have

Map object types and links onto existing tables and actions read and write them in place, in the same transaction as the audit entry. The database's own constraints still apply, and webhooks tell your other systems what happened — only after it really did.

<p align="center">
  <img src="images/terminal-readthrough.svg" alt="Terminal: actions on an existing ERP database, a CHECK constraint rolling an action back, webhooks delivered" width="900">
</p>

→ [Read-through](./docs/ontology-reference.md#existing-tables-read-through) · [Webhooks](./docs/ontology-reference.md#webhooks)

## ✍️ An Action, in YAML

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

Rules are written in a small, safe expression language: parsed with `ast`, whitelisted, interpreted, never `eval`'d. The whole file is checked when it loads — a rule that mentions a property that does not exist, an effect that forgets a required property, or a misspelt key such as `allowed_role:` fails before any agent can call it. Full syntax in the [ontology reference](./docs/ontology-reference.md).

## Architecture

<p align="center">
  <picture>
    <source media="(prefers-color-scheme: dark)" srcset="images/architecture-dark.svg">
    <img src="images/architecture-light.svg" alt="nano-onto architecture: agents, surfaces, engine, store" width="100%">
  </picture>
</p>

One engine sits behind every surface — MCP over stdio, MCP over HTTP, the CLI and the Python API — and runs each action as a single serialized transaction: role gate, parameters, rules, approvals, effects, audit. Read [Architecture](./docs/architecture.md) for the semantics, the module map and the limits worth knowing.

## Docs

- **Start:** [Walkthrough](./docs/walkthrough.md) · [中文流程](./docs/walkthrough_cn.md) · [`examples/walkthrough.sh`](./examples/walkthrough.sh)
- **Model your domain:** [Ontology reference](./docs/ontology-reference.md) · [Read permissions](./docs/ontology-reference.md#read-permissions) · [Read-through](./docs/ontology-reference.md#existing-tables-read-through) · [Webhooks](./docs/ontology-reference.md#webhooks) · [Migrations](./docs/ontology-reference.md#changing-the-ontology)
- **Run it:** [Connect an agent](./docs/agents.md) · [CLI reference](./docs/cli-reference.md)
- **Understand it:** [Architecture](./docs/architecture.md) · [Benchmark](./benchmark/README.md) · [Changelog](./CHANGELOG.md)
- **Examples:** [`examples/procurement`](./examples/procurement) · [`examples/erp_readthrough`](./examples/erp_readthrough)

## Where Do Ontologies Come From?

Writing YAML by hand is fine for a demo. For real documents, the sister project **Ontology-Anything** reads policies, SOPs, contracts and spreadsheets, proposes object types, actions and rules *with verbatim source quotes*, flags conflicts between documents, and emits a nano-onto ontology after a human approves it. Keys starting with `x-` are ignored everywhere in the YAML, so such tools can attach review metadata.

## Roadmap

- [x] Row- and property-level read permissions
- [x] PostgreSQL and DuckDB stores; read-through to existing tables
- [x] Webhooks with a transactional outbox and retries
- [x] Ontology versioning and reviewed migrations
- [x] MCP over HTTP with per-user tokens
- [x] Benchmark harness and a first model result
- [ ] Benchmark numbers across more models
- [ ] Row filters pushed down to SQL; composite keys in read-through
- [ ] OAuth and server-sent events for the HTTP transport

## 🤝 Contribute

Issues with a bug, a reproduction, a use case or a suggestion are welcome. Before a pull request, run:

```bash
python -m pip install -e ".[dev]"
ruff check . && pytest && bash examples/walkthrough.sh
```

The suite runs every backend it can find: SQLite always, DuckDB if installed, PostgreSQL against `$NANO_ONTO_TEST_PG` (or a throwaway embedded server if the `pgserver` package is installed).

<p align="center">
  <em>Thanks for visiting nano-onto. MIT License.</em>
</p>
