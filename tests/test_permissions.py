import json

import pytest

from conftest import ALICE, BOB, CAROL, PROCUREMENT, make_engine
from nano_onto import ActionError, Actor
from nano_onto.cli import main
from nano_onto.engine import MASK
from nano_onto.mcp_server import MCPServer, describe

DAVE = Actor("dave", "employee")


def ids(rows):
    return [r["po_id"] for r in rows]


def test_rows_are_filtered_per_actor(engine):
    assert ids(engine.query("PurchaseOrder", actor=ALICE)) == ["PO-0001"]
    assert ids(engine.query("PurchaseOrder", actor=DAVE)) == ["PO-0002"]
    assert ids(engine.query("PurchaseOrder", actor=BOB)) == ["PO-0001", "PO-0002"]
    assert ids(engine.query("PurchaseOrder")) == ["PO-0001", "PO-0002"]  # trusted caller


def test_limit_applies_after_the_row_filter(engine):
    for _ in range(3):
        engine.execute("submit_purchase_order", {"title": "t", "amount": 5, "supplier": "S-001"}, DAVE)
    assert ids(engine.query("PurchaseOrder", limit=2, actor=DAVE)) == ["PO-0002", "PO-0003"]
    assert ids(engine.query("PurchaseOrder", limit=1, actor=ALICE)) == ["PO-0001"]


def test_hidden_rows_look_missing(engine):
    assert engine.get("PurchaseOrder", "PO-0002", ALICE) is None
    assert engine.get("PurchaseOrder", "PO-0002", DAVE)["links"] == {"supplier": ["S-002"]}
    assert engine.get("PurchaseOrder", "PO-9999", CAROL) is None


def test_properties_are_masked_and_cannot_be_probed(engine):
    assert engine.get("Supplier", "S-001", ALICE)["properties"]["bank_account"] == MASK
    assert engine.get("Supplier", "S-001", CAROL)["properties"]["bank_account"].startswith("6222")
    assert {r["bank_account"] for r in engine.query("Supplier", actor=BOB)} == {MASK}
    with pytest.raises(ActionError, match="may not filter"):  # otherwise equality filters leak the value
        engine.query("Supplier", {"bank_account": "6222 0210 0100 3344"}, actor=ALICE)
    assert len(engine.query("Supplier", {"bank_account": "6222 0210 0100 3344"}, actor=CAROL)) == 1


def test_filters_are_typed(engine):
    assert [r["supplier_id"] for r in engine.query("Supplier", {"blacklisted": "true"})] == ["S-003"]
    assert ids(engine.query("PurchaseOrder", {"amount": "8600"})) == ["PO-0001"]
    with pytest.raises(ActionError, match="no property"):
        engine.query("Supplier", {"colour": "red"})
    with pytest.raises(ActionError, match="unknown object type"):
        engine.query("Nope")


def test_type_level_permission_and_link_filtering():
    def restrict(data):
        data["object_types"]["Supplier"]["readable_by"] = ["dept_manager", "cfo"]

    eng = make_engine(restrict)
    with pytest.raises(ActionError, match="may not read Supplier"):
        eng.query("Supplier", actor=ALICE)
    with pytest.raises(ActionError, match="may not read Supplier"):
        eng.get("Supplier", "S-001", ALICE)
    assert eng.get("PurchaseOrder", "PO-0001", ALICE)["links"] == {"supplier": []}  # target is not readable
    assert eng.get("PurchaseOrder", "PO-0001", BOB)["links"] == {"supplier": ["S-001"]}
    assert "Supplier" not in describe(eng, ALICE)["object_types"]
    assert "Supplier" in describe(eng, BOB)["object_types"]
    # an action cannot reference what its actor may not read: the supplier looks missing to alice
    res = eng.execute("submit_purchase_order", {"title": "t", "amount": 1, "supplier": "S-003"}, ALICE)
    assert res.status == "invalid" and "not found" in res.messages[0]
    assert eng.execute("submit_purchase_order", {"title": "t", "amount": 1, "supplier": "S-003"}, BOB).status == "denied"


def test_unknown_role_cannot_read(engine):
    with pytest.raises(ActionError, match="unknown role"):
        engine.query("Supplier", actor=Actor("mallory", "admin"))


def test_broken_row_filter_fails_closed():
    eng = make_engine(lambda d: d["object_types"]["PurchaseOrder"].update(visible_when="obj.amount > obj.title"))
    assert eng.query("PurchaseOrder", actor=CAROL) == []


def test_describe_hides_counts_that_would_leak(engine):
    as_alice = describe(engine, ALICE)["object_types"]
    assert "count" not in as_alice["PurchaseOrder"] and as_alice["Supplier"]["count"] == 3
    assert as_alice["Supplier"]["properties"]["bank_account"]["hidden"] is True
    assert describe(engine)["object_types"]["PurchaseOrder"]["count"] == 2


def test_mcp_tools_read_as_the_fixed_actor(engine):
    def call(actor, name, **args):
        payload, is_error = MCPServer(engine, actor).call_tool(name, args)
        return payload, is_error

    assert ids(call(ALICE, "onto_query", type="PurchaseOrder")[0]) == ["PO-0001"]
    assert call(ALICE, "onto_get", type="PurchaseOrder", id="PO-0002") == ({"error": "not found"}, True)
    assert call(ALICE, "onto_get", type="Supplier", id="S-002")[0]["properties"]["bank_account"] == MASK
    assert call(ALICE, "onto_query", type="Supplier", where={"bank_account": "x"})[1] is True
    assert call(ALICE, "onto_query", type="Nope")[1] is True
    assert call(ALICE, "onto_query", type="Supplier", limit="many")[1] is True


def test_cli_reads_as_actor(tmp_path, capsys):
    common = ["--schema", str(PROCUREMENT / "ontology.yaml"), "--db", str(tmp_path / "p.db")]
    assert main(["init", *common, "--seed", str(PROCUREMENT / "seed.json")]) == 0
    capsys.readouterr()
    assert main(["query", "PurchaseOrder", *common, "--json", "--actor", "alice", "--role", "employee"]) == 0
    assert ids(json.loads(capsys.readouterr().out)) == ["PO-0001"]
    assert main(["query", "Supplier", *common, "--json", "--where", "blacklisted=true"]) == 0
    assert [r["supplier_id"] for r in json.loads(capsys.readouterr().out)] == ["S-003"]
    assert main(["get", "PurchaseOrder", "PO-0002", *common, "--actor", "alice", "--role", "employee"]) == 1
    assert main(["query", "Supplier", *common, "--where", "bank_account=1", "--actor", "a", "--role", "employee"]) == 2
