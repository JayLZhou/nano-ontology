import json
import subprocess
import sys
from pathlib import Path

from nano_onto import Ontology
from nano_onto.cli import main
from nano_onto.compile import action_input_schema, render_skill

EXAMPLE = Path(__file__).resolve().parents[1] / "examples" / "procurement"
SCHEMA = str(EXAMPLE / "ontology.yaml")
SEED = str(EXAMPLE / "seed.json")


def test_input_schema():
    onto = Ontology.load(SCHEMA)
    schema = action_input_schema(onto.action_types["submit_purchase_order"], onto)
    assert schema["required"] == ["title", "amount", "supplier"]
    assert schema["properties"]["amount"]["type"] == "number"
    assert "Supplier" in schema["properties"]["supplier"]["description"]


def test_skill_mentions_rules():
    text = render_skill(Ontology.load(SCHEMA))
    assert text.startswith("---\nname: procurement-ontology")
    assert "cfo_over_50k" not in text  # ids stay internal
    assert "须财务总监审批" in text


def test_cli_end_to_end(tmp_path, capsys):
    db = str(tmp_path / "t.db")
    common = ["--schema", SCHEMA, "--db", db]
    assert main(["validate", "--schema", SCHEMA]) == 0
    assert main(["init", *common, "--seed", SEED]) == 0
    code = main(["run", "submit_purchase_order", *common, "--actor", "alice", "--role", "employee",
                 "-p", "title=服务器", "-p", "amount=60000", "-p", "supplier=S-002", "--json"])
    assert code == 0
    out = capsys.readouterr().out
    req = json.loads(out[out.index("{"):])["request_id"]
    assert main(["approve", req, *common, "--actor", "bob", "--role", "dept_manager"]) == 0
    assert main(["approve", req, *common, "--actor", "carol", "--role", "cfo"]) == 0
    capsys.readouterr()
    assert main(["query", "PurchaseOrder", *common, "--json"]) == 0
    rows = json.loads(capsys.readouterr().out)
    assert len(rows) == 3
    assert main(["run", "blacklist_supplier", *common, "--actor", "alice", "--role", "employee",
                 "-p", "supplier=S-001"]) == 2
    assert main(["export-skill", *common, "--out", str(tmp_path / "skill")]) == 0
    assert (tmp_path / "skill" / "SKILL.md").exists()


def test_mcp_stdio_roundtrip(tmp_path):
    db = str(tmp_path / "m.db")
    assert main(["init", "--schema", SCHEMA, "--db", db, "--seed", SEED]) == 0
    messages = [
        {"jsonrpc": "2.0", "id": 1, "method": "initialize",
         "params": {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "t", "version": "0"}}},
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
        {"jsonrpc": "2.0", "id": 3, "method": "tools/call",
         "params": {"name": "submit_purchase_order",
                    "arguments": {"title": "服务器", "amount": 60000, "supplier": "S-002"}}},
        {"jsonrpc": "2.0", "id": 4, "method": "tools/call",
         "params": {"name": "onto_approve", "arguments": {"request_id": "nope"}}},
        {"jsonrpc": "2.0", "id": 5, "method": "bogus"},
    ]
    proc = subprocess.run(
        [sys.executable, "-m", "nano_onto.cli", "serve-mcp", "--schema", SCHEMA, "--db", db,
         "--actor", "alice", "--role", "employee"],
        input="\n".join(json.dumps(m) for m in messages) + "\n",
        capture_output=True, text=True, timeout=30,
    )
    replies = {r["id"]: r for r in map(json.loads, proc.stdout.splitlines())}
    assert set(replies) == {1, 2, 3, 4, 5}
    assert replies[1]["result"]["protocolVersion"] == "2025-06-18"
    names = {t["name"] for t in replies[2]["result"]["tools"]}
    assert {"submit_purchase_order", "cancel_purchase_order", "onto_query"} <= names
    call = replies[3]["result"]
    assert call["isError"] is False
    assert call["structuredContent"]["status"] == "pending_approval"
    assert replies[4]["result"]["isError"] is True
    assert replies[5]["error"]["code"] == -32601
