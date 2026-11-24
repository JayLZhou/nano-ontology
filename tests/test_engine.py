import copy
import json
from pathlib import Path

import pytest
import yaml

from nano_onto import ActionError, Actor, Engine, Ontology, SchemaError, Store

EXAMPLE = Path(__file__).resolve().parents[1] / "examples" / "procurement"
ALICE = Actor("alice", "employee")
BOB = Actor("bob", "dept_manager")
CAROL = Actor("carol", "cfo")


def raw_schema():
    return yaml.safe_load((EXAMPLE / "ontology.yaml").read_text(encoding="utf-8"))


@pytest.fixture
def engine():
    eng = Engine(Ontology.from_dict(raw_schema()), Store())
    eng.load_seed(json.loads((EXAMPLE / "seed.json").read_text(encoding="utf-8")))
    return eng


def submit(engine, amount, supplier="S-001", actor=ALICE):
    return engine.execute(
        "submit_purchase_order", {"title": "t", "amount": amount, "supplier": supplier}, actor
    )


def test_small_order_executes_and_links(engine):
    res = submit(engine, 8000)
    assert res.status == "executed"
    pk = res.created["po"]
    assert engine.store.get("PurchaseOrder", pk)["requester"] == "alice"
    assert engine.store.links_from("supplier", pk) == ["S-001"]
    # generated id skips seeded PO-0001 / PO-0002
    assert pk == "PO-0003"


def test_string_amount_is_coerced(engine):
    assert submit(engine, "9000").status == "executed"


def test_blacklisted_supplier_denied(engine):
    res = submit(engine, 100, supplier="S-003")
    assert res.status == "denied"
    assert any("黑名单" in m for m in res.messages)
    assert engine.store.count("PurchaseOrder") == 2


def test_unknown_supplier_invalid(engine):
    assert submit(engine, 100, supplier="S-999").status == "invalid"


def test_missing_and_unknown_params(engine):
    assert engine.execute("submit_purchase_order", {"title": "t"}, ALICE).status == "invalid"
    res = engine.execute("submit_purchase_order", {"title": "t", "amount": 1, "supplier": "S-001", "x": 1}, ALICE)
    assert res.status == "invalid"


def test_role_not_allowed(engine):
    res = engine.execute("blacklist_supplier", {"supplier": "S-001"}, ALICE)
    assert res.status == "denied"
    assert engine.store.get("Supplier", "S-001")["blacklisted"] is False


def test_approval_flow(engine):
    res = submit(engine, 60000)
    assert res.status == "pending_approval"
    assert res.required_approvals == ["cfo", "dept_manager"]
    assert engine.store.count("PurchaseOrder") == 2  # nothing created yet

    with pytest.raises(ActionError):  # wrong role
        engine.approve(res.request_id, Actor("eve", "employee"))
    with pytest.raises(ActionError):  # separation of duties
        engine.approve(res.request_id, Actor("alice", "cfo"))

    step = engine.approve(res.request_id, BOB)
    assert step.status == "pending_approval" and step.required_approvals == ["cfo"]
    assert [a["id"] for a in engine.pending("cfo")] == [res.request_id]

    done = engine.approve(res.request_id, CAROL)
    assert done.status == "executed"
    assert engine.store.count("PurchaseOrder") == 3
    assert engine.store.get_approval(res.request_id)["status"] == "executed"
    with pytest.raises(ActionError):
        engine.approve(res.request_id, CAROL)


def test_actor_with_required_role_needs_no_extra_approval(engine):
    res = submit(engine, 30000, actor=BOB)
    assert res.status == "executed"


def test_approval_rechecks_deny_rules(engine):
    res = submit(engine, 20000, supplier="S-002")
    engine.execute("blacklist_supplier", {"supplier": "S-002"}, CAROL)
    final = engine.approve(res.request_id, BOB)
    assert final.status == "denied"
    assert engine.store.get_approval(res.request_id)["status"] == "denied"


def test_reject(engine):
    res = submit(engine, 20000)
    out = engine.reject(res.request_id, BOB, "预算不足")
    assert out.status == "rejected"
    assert engine.pending() == []


def test_cancel_rules_follow_object_state(engine):
    hidden = engine.execute("cancel_purchase_order", {"po": "PO-0002"}, ALICE)  # alice cannot see dave's order
    assert hidden.status == "invalid" and "not found" in hidden.messages[0] and hidden.trace == []
    assert engine.execute("cancel_purchase_order", {"po": "PO-0002"}, BOB).status == "denied"  # bob can, but may not
    assert engine.execute("cancel_purchase_order", {"po": "PO-0001"}, ALICE).status == "denied"  # paid
    assert engine.execute("cancel_purchase_order", {"po": "PO-0002"}, CAROL).status == "executed"
    assert engine.store.get("PurchaseOrder", "PO-0002")["status"] == "cancelled"


def test_link_traversal_in_rules():
    data = raw_schema()
    data["action_types"]["cancel_purchase_order"]["rules"].append(
        {"id": "no_c_rated", "when": "params.po.supplier.rating == 'B'", "then": "deny", "message": "B"}
    )
    eng = Engine(Ontology.from_dict(data), Store())
    eng.load_seed(json.loads((EXAMPLE / "seed.json").read_text(encoding="utf-8")))
    res = eng.execute("cancel_purchase_order", {"po": "PO-0002"}, CAROL)
    assert res.status == "denied" and res.messages == ["denied: B"]


def test_dry_run_changes_nothing(engine):
    res = engine.explain("submit_purchase_order", {"title": "t", "amount": 60000, "supplier": "S-001"}, ALICE)
    assert res.status == "pending_approval" and res.request_id is None and res.dry_run
    assert engine.pending() == []
    assert [(r["event"], r["outcome"]) for r in engine.store.audit_log()] == [("explain", "pending_approval")]


def test_effects_roll_back_on_failure(engine):
    data = raw_schema()
    data["action_types"]["submit_purchase_order"]["effects"].append(
        {"update": "po", "set": {"status": "'not-a-valid-status'"}}
    )
    eng = Engine(Ontology.from_dict(data), Store())
    eng.load_seed(json.loads((EXAMPLE / "seed.json").read_text(encoding="utf-8")))
    res = eng.execute("submit_purchase_order", {"title": "t", "amount": 10, "supplier": "S-001"}, ALICE)
    assert res.status == "invalid"
    assert eng.store.count("PurchaseOrder") == 2


def test_audit_log_records_everything(engine):
    submit(engine, 10)
    submit(engine, 10, supplier="S-003")
    outcomes = [row["outcome"] for row in engine.store.audit_log()]
    assert outcomes == ["executed", "denied"]


@pytest.mark.parametrize(
    "mutate, message",
    [
        (lambda d: d["action_types"]["submit_purchase_order"]["rules"].append(
            {"id": "x", "when": "params.nope > 1", "then": "deny"}), "unknown parameter"),
        (lambda d: d["action_types"]["submit_purchase_order"]["rules"].append(
            {"id": "x", "when": "params.supplier.colour == 'red'", "then": "deny"}), "no property or link"),
        (lambda d: d["action_types"]["submit_purchase_order"]["rules"].append(
            {"id": "x", "when": "params.amount > 1", "then": {"require_approval": "ceo"}}), "unknown role"),
        (lambda d: d["action_types"]["submit_purchase_order"]["rules"].append(
            {"id": "x", "when": "__import__('os')", "then": "deny"}), "only these functions"),
        (lambda d: d["action_types"]["submit_purchase_order"]["effects"][0]["set"].update(
            {"colour": "'red'"}), "no property 'colour'"),
        (lambda d: d["link_types"]["supplier"].update({"to": "Vendor"}), "unknown object type"),
    ],
)
def test_schema_validation_catches_mistakes(mutate, message):
    data = copy.deepcopy(raw_schema())
    mutate(data)
    with pytest.raises(SchemaError) as err:
        Ontology.from_dict(data)
    assert message in str(err.value)


def test_x_keys_are_ignored():
    data = raw_schema()
    data["object_types"]["x-review"] = {"anything": True}
    Ontology.from_dict(data)
