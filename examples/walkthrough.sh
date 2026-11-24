#!/usr/bin/env bash
# The complete nano-onto flow, end to end, on a throwaway database.
#
#   pip install -e .
#   bash examples/walkthrough.sh
#
# Every step is explained in docs/walkthrough.md. Nothing outside a temp directory is touched.
set -euo pipefail
cd "$(dirname "$0")/.."

WORK=$(mktemp -d)
trap 'kill "${SERVER:-}" 2>/dev/null || true; rm -rf "$WORK"' EXIT
cp examples/procurement/ontology.yaml "$WORK/ontology.yaml"
export NANO_ONTO_SCHEMA="$WORK/ontology.yaml" NANO_ONTO_DB="$WORK/shop.db"

exec 3>&1  # commands are echoed here, so `run ... | filter` only filters the command's own output
step() { printf '\n\033[1m== %s\033[0m\n' "$*"; }
run()  { printf '\033[2m$ %s\033[0m\n' "$*" >&3; "$@"; }
fails() { printf '\033[2m$ %s\033[0m\n' "$*" >&3; if "$@"; then echo "expected this to be refused" >&2; exit 1; fi; }

ALICE="--actor alice --role employee"; BOB="--actor bob --role dept_manager"; CAROL="--actor carol --role cfo"

step "1. Check the ontology, create the database, load seed data"
run nano-onto validate
run nano-onto init --seed examples/procurement/seed.json

step "2. Ask before acting: a dry run shows which rules would fire"
run nano-onto explain submit_purchase_order -p title=Servers -p amount=60000 -p supplier=S-002 $ALICE

step "3. A small order just happens"
run nano-onto run submit_purchase_order -p title=Stationery -p amount=8000 -p supplier=S-001 $ALICE

step "4. A blacklisted supplier is refused, whoever asks"
fails nano-onto run submit_purchase_order -p title=Chips -p amount=4800 -p supplier=S-003 $ALICE

step "5. A large order is held for approval: nothing is created yet"
run nano-onto run submit_purchase_order -p title=Servers -p amount=60000 -p supplier=S-002 $ALICE
REQ=$(nano-onto pending --json | python3 -c 'import json, sys; print(json.load(sys.stdin)[0]["id"])')

step "6. Separation of duties: the requester cannot approve it, even with the right role"
fails nano-onto approve "$REQ" --actor alice --role cfo

step "7. Both approvers sign; the rules are re-checked; only then is the order created"
run nano-onto approve "$REQ" $BOB
run nano-onto approve "$REQ" $CAROL
run nano-onto request "$REQ" --json | python3 -c 'import json, sys; r = json.load(sys.stdin); print(r["id"], "->", r["status"], "approved by", r["approved_by"])'

step "8. The same question, two answers: read permissions"
run nano-onto query PurchaseOrder $ALICE --json | python3 -c 'import json, sys; print("alice sees", [p["po_id"] for p in json.load(sys.stdin)])'
run nano-onto query PurchaseOrder $CAROL --json | python3 -c 'import json, sys; print("carol sees", [p["po_id"] for p in json.load(sys.stdin)])'
run nano-onto get Supplier S-001 $ALICE
fails nano-onto run cancel_purchase_order -p po=PO-0002 $ALICE      # dave's order: to alice it does not exist

step "9. Everything that was attempted is on the record"
run nano-onto audit

step "10. Change a rule: the database refuses the new ontology until the change is reviewed"
sed -i.bak -e 's/^version: 0.1.0/version: 0.2.0/' -e 's/params.amount > 10000/params.amount > 5000/' "$WORK/ontology.yaml"
fails nano-onto run submit_purchase_order -p title=Printers -p amount=7000 -p supplier=S-001 $ALICE
run nano-onto migrate --dry-run
run nano-onto migrate --by carol
run nano-onto run submit_purchase_order -p title=Printers -p amount=7000 -p supplier=S-001 $ALICE
run nano-onto history --json | python3 -c 'import json, sys; [print(h["seq"], h["event"], h["ontology"]) for h in json.load(sys.stdin)]'

step "11. Hand it to an agent: MCP over stdio (the actor is fixed when the server starts)"
printf '%s\n' \
  '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-06-18","capabilities":{},"clientInfo":{"name":"walkthrough","version":"0"}}}' \
  '{"jsonrpc":"2.0","method":"notifications/initialized"}' \
  '{"jsonrpc":"2.0","id":2,"method":"tools/call","params":{"name":"blacklist_supplier","arguments":{"supplier":"S-002"}}}' \
  | nano-onto serve-mcp $ALICE \
  | python3 -c 'import json, sys; r = [json.loads(l) for l in sys.stdin]; print("server:", r[0]["result"]["serverInfo"]["name"]); print("alice calls blacklist_supplier ->", r[1]["result"]["structuredContent"]["status"], r[1]["result"]["structuredContent"]["messages"])'

step "12. ...or over HTTP, where each bearer token is one user"
TOKEN=$(nano-onto token --tokens "$WORK/tokens.yaml" --actor carol --role cfo | tail -1)
nano-onto serve-http --tokens "$WORK/tokens.yaml" --port 18765 2>/dev/null &
SERVER=$!
for _ in $(seq 1 50); do curl -s -o /dev/null http://127.0.0.1:18765/healthz && break; sleep 0.1; done
call() { curl -s -X POST http://127.0.0.1:18765/mcp -H 'Content-Type: application/json' "$@"; }
BODY='{"jsonrpc":"2.0","id":1,"method":"tools/call","params":{"name":"blacklist_supplier","arguments":{"supplier":"S-002"}}}'
echo "no token   -> HTTP $(call -o /dev/null -w '%{http_code}' -d "$BODY")"
echo "carol/cfo  -> $(call -H "Authorization: Bearer $TOKEN" -d "$BODY" | python3 -c 'import json, sys; print(json.load(sys.stdin)["result"]["structuredContent"]["status"])')"
kill "$SERVER"

step "13. ...or as a Skill that teaches an agent the CLI"
run nano-onto export-skill --out "$WORK/skill"
head -5 "$WORK/skill/SKILL.md"

step "14. And for people: the web console, on the same engine"
nano-onto console --demo alice:employee --demo carol:cfo --auditor cfo --port 18787 2>/dev/null &
SERVER=$!
for _ in $(seq 1 50); do curl -s -o /dev/null http://127.0.0.1:18787/healthz && break; sleep 0.1; done
see() { curl -s -H "X-Demo-User: $1" "http://127.0.0.1:18787/api/$2"; }
echo "alice's audit view -> $(see alice audit | python3 -c 'import json, sys; d = json.load(sys.stdin); print(d["scope"], "entries:", len(d["rows"]))')"
echo "carol's audit view -> $(see carol audit | python3 -c 'import json, sys; d = json.load(sys.stdin); print(d["scope"], "entries:", len(d["rows"]))')"
echo "alice sees bank account -> $(see alice 'object?type=Supplier&id=S-001' | python3 -c 'import json, sys; print(json.load(sys.stdin)["properties"]["bank_account"])')"
kill "$SERVER"

printf '\n\033[1mDone.\033[0m The whole flow ran against %s (now deleted).\n' "$NANO_ONTO_DB"
