# Connect an agent

nano-onto reaches an agent in four ways. They share one engine, so the rules, approvals, read permissions and audit log are identical; they differ in **who decides the actor**.

| Surface | Who decides the actor | Use it when |
|---|---|---|
| [MCP over stdio](#mcp-over-stdio) | Whoever starts the server | One user, a local client (Claude Desktop, Claude Code, Cursor, nanobot) |
| [MCP over HTTP](#mcp-over-http) | The bearer token on each request | Many users, one server |
| [Skill](#skill) | The agent, by passing `--actor/--role` | Trusted agents in environments without MCP |
| [Python](#python) | Your code | You are building the agent |

In the first two the model cannot choose its identity, which is the point: an agent that can pick its own role can pick `cfo`.

People use the same engine through the [web console](./console.md): approvers decide requests there, and anyone can see what an agent did on their behalf.

## The tools an agent sees

Each action in the ontology is one tool. Its description lists the action's allowed roles and every rule with its message, and its input schema is generated from the parameters. Next to them:

| Tool | What it does |
|---|---|
| `onto_describe` | Object types, properties, links, roles and actions, as this actor may see them |
| `onto_query` | List objects of a type, with exact-match filters; read permissions applied |
| `onto_get` | One object with its outgoing links |
| `onto_explain` | Dry-run an action: the outcome and the rule trace, nothing changed |
| `onto_request_status` | Has a held action happened yet? (`pending` / `executed` / `denied` / `rejected` / `failed` / `stale`) |
| `onto_pending_approvals` | Requests waiting for this actor's role |
| `onto_approve`, `onto_reject` | Decide a request (never your own) |

Every action result carries a `status`: `executed`, `pending_approval`, `denied` or `invalid`. The tool descriptions tell the model to report it truthfully; `pending_approval` in particular means the change has **not** happened.

## MCP over stdio

```bash
nano-onto serve-mcp --schema /abs/path/ontology.yaml --db /abs/path/shop.db --actor alice --role employee
```

A dependency-free MCP server (JSON-RPC 2.0 over stdin/stdout, protocol versions 2024-11-05 through 2025-11-25). It is tested against the official MCP Python SDK client.

**Claude Desktop** -- `claude_desktop_config.json`:

```json
{
  "mcpServers": {
    "procurement": {
      "command": "nano-onto",
      "args": ["serve-mcp", "--schema", "/abs/path/ontology.yaml", "--db", "/abs/path/shop.db",
               "--actor", "alice", "--role", "employee"]
    }
  }
}
```

**Cursor** -- the same object in `.cursor/mcp.json`.

**Claude Code**:

```bash
claude mcp add procurement -- nano-onto serve-mcp --schema /abs/path/ontology.yaml --db /abs/path/shop.db --actor alice --role employee
```

**nanobot and other MCP clients** -- register the same command and arguments wherever the client lists its MCP servers.

Use absolute paths: clients start the server from their own working directory. If `nano-onto` is installed in a virtual environment, use the full path to its executable as `command`.

## MCP over HTTP

One process serves many users; each request carries a bearer token and the token decides who is acting.

```bash
nano-onto token --tokens tokens.yaml --actor alice --role employee      # prints the token once
nano-onto token --tokens tokens.yaml --actor carol --role cfo
nano-onto serve-http --schema ontology.yaml --db shop.db --tokens tokens.yaml --port 8765
```

```bash
curl -s http://127.0.0.1:8765/mcp \
  -H "Authorization: Bearer nonto_..." -H "Content-Type: application/json" \
  -d '{"jsonrpc":"2.0","id":1,"method":"tools/call","params":{"name":"onto_query","arguments":{"type":"PurchaseOrder"}}}'
```

**Claude Code**:

```bash
claude mcp add --transport http procurement http://127.0.0.1:8765/mcp --header "Authorization: Bearer nonto_..."
```

With the MCP Python SDK:

```python
from mcp import ClientSession
from mcp.client.streamable_http import create_mcp_http_client, streamable_http_client

async with create_mcp_http_client(headers={"Authorization": f"Bearer {token}"}) as http:
    async with streamable_http_client("http://127.0.0.1:8765/mcp", http_client=http) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            result = await session.call_tool("submit_purchase_order", {"title": "servers", "amount": 60000, "supplier": "S-002"})
```

What to know:

- This is the request/response half of MCP's Streamable HTTP transport: each `POST /mcp` carries one JSON-RPC message and gets one JSON reply (notifications get `202`). There is no server-sent event stream; `GET /mcp` answers 405, as the specification allows.
- `tokens.yaml` stores only SHA-256 hashes. `nano-onto token --tokens tokens.yaml --actor alice --revoke` removes a user's tokens, and the running server notices without a restart.
- The server binds to `127.0.0.1` and rejects browser requests from other origins (`--allow-origin` adds one). Put TLS and your own network controls in front of it before exposing it.
- `GET /healthz` reports the ontology the server holds.
- If the ontology has webhooks, both servers deliver them in the background after each commit.

## Skill

```bash
nano-onto export-skill --schema ontology.yaml --db shop.db --out skills/procurement
```

writes `skills/procurement/SKILL.md`: the object types, every action with its parameters and rules, and instructions to change data only through `nano-onto run`, to report `pending_approval` as not done, and never to pick a role to get past a rule.

A Skill is instructions, not enforcement of identity: the agent passes `--actor` and `--role` itself (or you set `NANO_ONTO_ACTOR` / `NANO_ONTO_ROLE` in its environment). The rules are still enforced by the CLI; what a Skill cannot prevent is the agent claiming a different role. Use MCP when that matters. A database URL that contains credentials is never written into the Skill; it refers to `$NANO_ONTO_DB` instead.

## Python

```python
from nano_onto import Actor, Engine, Ontology, open_store
from nano_onto.mcp_server import MCPServer

onto = Ontology.load("ontology.yaml")
engine = Engine(onto, open_store("shop.db", onto))
alice = Actor("alice", "employee")

# call the engine directly ...
result = engine.execute("submit_purchase_order", {"title": "servers", "amount": 60000, "supplier": "S-002"}, alice)
rows = engine.query("PurchaseOrder", actor=alice)

# ... or hand your own agent loop the same tools an MCP client would get
server = MCPServer(engine, alice)
tools = server.list_tools()                       # name, description, inputSchema for each tool
payload, is_error = server.call_tool("onto_explain", {"action": "submit_purchase_order",
                                                      "params": {"title": "servers", "amount": 60000, "supplier": "S-002"}})
```

`benchmark/bench.py` is a complete example of the second style: an agent loop over any OpenAI-compatible chat endpoint using `MCPServer.list_tools()` and `call_tool()`.

One rule for embedders: an action is its own transaction, so do not call the engine inside your own `store.transaction()` block.
